"""The semantic index, as a set of questions an agent can ask.

Each tool answers one question and returns JSON.  Four rules shape all of them,
and they exist because the reader on the other end pays for every token and
cannot see the repository:

* Nothing returns a source file.  `get_source_context` is the only tool that
  returns text, and what it returns is a region, with line numbers in it.
* Every symbol in a result is named by a ``file:line``, and that spelling
  resolves exactly when it is passed back as an argument - so an agent can
  follow one answer to the next question without ever handling a USR.
* A list that has been cut says so, and carries its true length.  A list of
  five callers with no note is how an answer becomes a lie.
* Nothing is guessed.  A name that denotes three overloads comes back as three
  candidates, and a dependency that holds only under run-time dispatch is
  labelled `possible` rather than reported as fact.

The tools are declared as data so that the same definitions serve the MCP
server, the command line and the tests, and so that the answer to "what can
this thing tell me" is one list that cannot drift from what it does.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import changes as changes_mod
from . import git as git_mod
from .discovery import language_of
from .query import (CALLABLE_KINDS, CALL_EDGES, DEPENDENCY_EDGES,
                    REFERENCE_EDGES, Query)

# Kinds that read as a class to someone asking for one.
TYPE_KINDS = ("class", "struct", "union", "enum")

# How much of a list is worth sending before it stops being an answer.  These
# are the defaults; a caller that wants more can raise them.
DEFAULT_LIST = 25
MAX_LIST = 500


class ToolError(Exception):
    """A question that cannot be answered, as opposed to one with no answer.

    The difference matters to an agent: "nothing matches that name" is a fact
    about the code and belongs in the result, while "the index has not been
    built" is a fact about the situation and belongs in the error - it says
    what to do next rather than inviting another look at the same nothing.
    """

    def __init__(self, message: str, payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.payload = payload if payload is not None else {"error": message}


@dataclass
class Tool:
    name: str
    title: str
    description: str
    schema: Dict[str, Any]
    handler: Callable[[Query, Dict[str, Any]], Dict[str, Any]]


# ---------------------------------------------------------------------------
# Argument reading
#
# An agent composing a call from a description will sometimes pass a number as
# a string, or leave out something optional.  Rejecting a call over that costs
# a turn and teaches nothing, so the readers coerce, clamp, and give a clear
# message only when the argument is genuinely missing or unusable.
# ---------------------------------------------------------------------------

def _text(args: Dict[str, Any], key: str, required: bool = False) -> str:
    value = args.get(key)
    if value is None:
        if required:
            raise ToolError(f"{key!r} is required")
        return ""
    if not isinstance(value, str):
        raise ToolError(f"{key!r} must be a string, not {type(value).__name__}")
    value = value.strip()
    if required and not value:
        raise ToolError(f"{key!r} must not be empty")
    return value


def _int(args: Dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = args.get(key)
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{key!r} must be a whole number, not {value!r}")
    return max(low, min(high, number))


def _flag(args: Dict[str, Any], key: str, default: bool = False) -> bool:
    value = args.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _cut(out: Dict[str, Any], key: str, entries: Sequence[Any], limit: int,
         count_key: Optional[str] = None,
         count: Optional[Callable[[], int]] = None) -> None:
    """Store a list, and the number it was cut from when it was cut.

    Two ways to call this, and getting them the wrong way round is silent.

    A caller holding a *complete* list passes it and nothing else: the length is
    the answer, and `len(entries) > limit` is a true statement about the world.

    A caller holding a *page* must fetch one more than it intends to show, and
    pass `count` for the exact total.  Fetching `limit` and comparing against
    `limit` can never detect a cut, because a query with `LIMIT n` cannot
    return n+1 rows - so the answer is silently presented as complete when it
    is not.  `count` is asked for only when the page was in fact full, so an
    ordinary answer does not pay for a number it will not report.
    """
    out[key] = list(entries[:limit])
    if len(entries) > limit:
        out[count_key or f"{key}_count"] = count() if count else len(entries)


def _schema(properties: Dict[str, Any], required: Sequence[str] = ()
            ) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_SYMBOL = {"type": "string", "description":
           "A symbol name (`Foo::resize`), a qualified name with a parameter "
           "list to pick an overload (`Foo::resize(size_t)`), or a "
           "`file.cpp:142` location taken from an earlier result."}
_PATH = {"type": "string", "description":
         "A file, absolute or relative to the project root. One query names a "
         "symbol that is declared in several files at once; the same file is "
         "also accepted as `pool.cpp` when the name is unambiguous."}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_LIST,
          "description": "How many entries to return."}
_FILE = {"type": "string", "description":
         "A file, absolute or relative to the project root."}


def _one(query: Query, args: Dict[str, Any], key: str = "symbol") -> str:
    """The USR a reference names, or the reason there is not one.

    The failure carries the candidates, so an agent that asked for one of three
    overloads is told which three rather than being left to guess at a spelling
    the index might accept.
    """
    reference = _text(args, key, required=True)
    usr, failure = query.resolve_one(reference, _text(args, "path") or None)
    if usr is None:
        payload = dict(failure or {})
        raise ToolError(payload.get("error", f"no symbol matching {reference!r}"),
                        payload)
    return usr


def _target(query: Query, usr: str) -> Dict[str, Any]:
    """The summary fields every list-shaped answer starts with."""
    row = query.symbol(usr, detail=False)
    out: Dict[str, Any] = {"symbol": row["symbol"]}
    if row.get("location"):
        out["location"] = row["location"]
    if row.get("defined_at"):
        out["defined_at"] = row["defined_at"]
    return out


def _as_callable(query: Query, usr: str) -> Dict[str, Any]:
    """The symbol's detail, refusing one that cannot be called."""
    detail = query.symbol(usr)
    if detail["kind"] not in CALLABLE_KINDS:
        raise ToolError(
            f"{detail['symbol']} is a {detail['kind']}, not a function",
            {"error": f"{detail['symbol']} is a {detail['kind']}, not a function",
             "symbol": detail["symbol"], "kind": detail["kind"],
             "hint": "get_class describes a type; get_symbol describes anything"},
        )
    return detail


# ---------------------------------------------------------------------------
# Looking a symbol up
# ---------------------------------------------------------------------------

def _find_symbol(query: Query, args: Dict[str, Any]) -> Dict[str, Any]:
    reference = _text(args, "reference", required=True)
    limit = _int(args, "limit", 10, 1, MAX_LIST)
    path = _text(args, "path")
    # One more than will be shown, so a full page can be told from a page that
    # exactly fitted.  `match_count` is how many symbols match, which is not
    # the same question as how many are listed.
    matches = query.ambiguity(reference, path, limit + 1)
    out: Dict[str, Any] = {"reference": reference, "matches": matches[:limit]}
    if len(matches) > limit:
        out["match_count"] = query.count_ambiguity(reference, path)
    else:
        out["match_count"] = len(matches)
    if not matches:
        out["note"] = (
            "nothing in the index matches that spelling; search_symbols "
            "matches on substrings, and a file added since the index was built "
            "is not in it"
        )
    elif len(matches) > 1:
        out["note"] = (
            "several symbols match; narrow the question with `path`, or pass "
            "one of these `location` values to name exactly one"
        )
        if len(matches) > limit:
            out["more"] = True
    return out


def _get_symbol(query: Query, args: Dict[str, Any]) -> Dict[str, Any]:
    usr = _one(query, args)
    out = query.symbol(usr)
    out["usr"] = usr
    # Counts rather than lists: an agent deciding whether to dig further needs
    # to know that there are forty callers, not to receive forty of them.
    out["callers"] = query.degree(usr, direction="in")
    out["callees"] = query.degree(usr, direction="out")
    if out["kind"] not in CALLABLE_KINDS:
        out["references"] = query.degree(usr, ("references",), direction="in")
    ancestors = [a["symbol"] for a in query.ancestors(usr)]
    if ancestors:
        out["within"] = ancestors
    return out


def _get_function(query: Query, args: Dict[str, Any]) -> Dict[str, Any]:
    usr = _one(query, args)
    detail = _as_callable(query, usr)
    detail["usr"] = usr
    detail["callers"] = query.degree(usr, direction="in")
    detail["callees"] = query.degree(usr, direction="out")
    ancestors = query.ancestors(usr)
    if ancestors:
        # The chain, innermost first: the class for a member, the namespace
        # after it.  Read from the parent links, so it is right even when the
        # definition is in another file from the declaration.
        detail["within"] = [a["symbol"] for a in ancestors]
        enclosing = ancestors[0]
        if enclosing["kind"] in TYPE_KINDS:
            detail["member_of"] = enclosing["symbol"]
    if detail["kind"] in ("constructor", "destructor"):
        detail.pop("type", None)
    return detail


def _get_class(query: Query, args: Dict[str, Any]) -> Dict[str, Any]:
    usr = _one(query, args)
    detail = query.symbol(usr)
    if detail["kind"] not in TYPE_KINDS + ("namespace",):
        raise ToolError(
            f"{detail['symbol']} is a {detail['kind']}, not a type",
            {"error": f"{detail['symbol']} is a {detail['kind']}, not a type",
             "symbol": detail["symbol"], "kind": detail["kind"],
             "hint": "get_symbol describes any symbol"},
        )
    limit = _int(args, "limit", DEFAULT_LIST, 1, MAX_LIST)
    out = _target(query, usr)
    out["usr"] = usr
    out["kind"] = detail["kind"]
    if detail.get("properties"):
        out["properties"] = detail["properties"]
    if detail.get("type"):
        out["type"] = detail["type"]

    tree = query.inheritance(usr)
    # Bases and derived classes are the question this tool exists to answer, so
    # an empty list is stated rather than omitted.  "This class has no bases" is
    # an answer; a missing key leaves the reader unable to tell it from a tool
    # that did not look.
    for key in ("bases", "derived"):
        _cut(out, key, tree.get(key, []), limit)
    for key in ("ancestors", "descendants", "overrides", "overrides_indirectly",
                "overridden"):
        if tree.get(key):
            _cut(out, key, tree[key], limit)

    members = query.members(usr)
    methods = [m for m in members if m["kind"] in CALLABLE_KINDS]
    fields = [m for m in members if m["kind"] in ("field", "enumerator",
                                                  "variable")]
    nested = [m for m in members if m["kind"] in TYPE_KINDS]
    if methods:
        _cut(out, "methods", methods, limit, "method_count")
    if fields:
        _cut(out, "fields", fields, limit, "field_count")
    if nested:
        _cut(out, "nested_types", nested, limit, "nested_type_count")
    return out


def _search_symbols(query: Query, args: Dict[str, Any]) -> Dict[str, Any]:
    text = _text(args, "query", required=True)
    kind = _text(args, "kind") or None
    limit = _int(args, "limit", 20, 1, MAX_LIST)
    include_system = _flag(args, "include_system")
    # One more than will be shown, so a full page can be told from a page that
    # exactly fitted.  `match_count` is then the number that matched rather
    # than the number returned, which is the question a caller is asking when
    # it reads a count at all.
    matches = query.search(text, kind=kind, limit=limit + 1,
                           include_system=include_system)
    out: Dict[str, Any] = {"query": text, "matches": matches[:limit]}
    if len(matches) > limit:
        out["match_count"] = query.count_search(text, kind, include_system)
        out["more"] = True
    else:
        out["match_count"] = len(matches)
    if not matches:
        out["note"] = "no name contains that text"
    return out


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------

def _neighbours(query: Query, args: Dict[str, Any], direction: str,
                key: str) -> Dict[str, Any]:
    usr = _one(query, args)
    limit = _int(args, "limit", DEFAULT_LIST, 1, MAX_LIST)
    include_system = _flag(args, "include_system")
    # One more than asked for, so that a full page can be told from a page that
    # exactly fitted; the exact total is then one indexed query.
    entries = (query.callers(usr, limit=limit + 1, include_system=include_system)
               if direction == "in" else
               query.callees(usr, limit=limit + 1, include_system=include_system))
    out = _target(query, usr)
    out["kind"] = query.symbol(usr, detail=False)["kind"]
    _cut(out, key, entries, limit,
         "caller_count" if direction == "in" else "callee_count",
         count=lambda: query.degree(usr, CALL_EDGES, direction, include_system))
    if not entries:
        out["note"] = _empty_neighbour_note(out["kind"], direction)
    return out


# A macro is the one kind where an empty answer means less than it looks like.
# Macros are indexed as definitions, with a name and a location, and nothing
# records their uses - so "no callers" and "no references" are true statements
# about a table that was never populated for them.  An agent reading that as
# "this macro is unused" would delete live code, so the answer says which it is.
_MACRO_NOTE = (
    "this is a macro: the index records where it is defined and not where it "
    "is used, so an empty list here does not mean it is unused"
)


def _empty_neighbour_note(kind: str, direction: str) -> str:
    if kind == "macro":
        return _MACRO_NOTE
    return ("nothing in the index calls this" if direction == "in"
            else "this calls nothing the index records")


def _get_callers(query: Query, args: Dict[str, Any]) -> Dict[str, Any]:
    return _neighbours(query, args, "in", "callers")


def _get_callees(query: Query, args: Dict[str, Any]) -> Dict[str, Any]:
    return _neighbours(query, args, "out", "callees")


def _get_references(query: Query, args: Dict[str, Any]) -> Dict[str, Any]:
    usr = _one(query, args)
    limit = _int(args, "limit", DEFAULT_LIST, 1, MAX_LIST)
    include_system = _flag(args, "include_system")
    entries = query.references_to(usr, limit=limit + 1,
                                  include_system=include_system)
    out = _target(query, usr)
    out["kind"] = query.symbol(usr, detail=False)["kind"]
    _cut(out, "references", entries, limit, "reference_count",
         count=lambda: query.degree(usr, REFERENCE_EDGES, "in", include_system))
    out["note"] = (
        _MACRO_NOTE if out["kind"] == "macro" else
        "uses of the symbol that are not calls: assignments, addresses taken, "
        "and calls made through a function pointer, whose target is only "
        "known at run time")
    return out


def _get_inheritance(query: Query, args: Dict[str, Any]) -> Dict[str, Any]:
    usr = _one(query, args)
    limit = _int(args, "limit", DEFAULT_LIST, 1, MAX_LIST)
    tree = query.inheritance(usr, transitive=_flag(args, "transitive",
                                                   default=True))
    out = _target(query, usr)
    out["kind"] = query.symbol(usr, detail=False)["kind"]
    for key, entries in tree.items():
        _cut(out, key, entries, limit)
    return out


def _get_symbol_dependencies(query: Query, args: Dict[str, Any]
                             ) -> Dict[str, Any]:
    usr = _one(query, args)
    limit = _int(args, "limit", DEFAULT_LIST, 1, MAX_LIST)
    out = _target(query, usr)
    out["kind"] = query.symbol(usr, detail=False)["kind"]
    # Asked for one kind at a time.  The single grouped query this replaces
    # applied its limit across every group at once, so a symbol with sixty
    # parameter types and no calls would spend the whole budget on the types
    # and report nothing else - and either way the answer could not say that
    # anything had been left out, because the cut was invisible from here.
    grouped = 0
    for kind in DEPENDENCY_EDGES:
        entries = query.outgoing(usr, (kind,), limit=limit + 1)
        if not entries:
            continue
        grouped += 1
        _cut(out, kind, entries, limit,
             count=lambda k=kind: query.degree(usr, (k,), "out"))
    # Summing the lists would under-report by exactly the amount that was cut,
    # and it would do so silently in the one answer where the reader most needs
    # the real number.  A group that was cut carries its exact total under
    # `<kind>_count`, so prefer that wherever it exists.
    out["dependency_count"] = sum(
        out.get(f"{k}_count", len(v)) for k, v in out.items()
        if k in DEPENDENCY_EDGES and isinstance(v, list))
    if not grouped:
        out["note"] = "the index records no outgoing relationships"
    return out


def _get_impact_analysis(query: Query, args: Dict[str, Any]) -> Dict[str, Any]:
    usr = _one(query, args)
    depth = _int(args, "depth", 3, 1, 8)
    limit = _int(args, "limit", 40, 1, MAX_LIST)
    report = query.impact(usr, depth=depth, limit=limit)
    if not report:
        raise ToolError("the index has no record of that symbol")
    return report


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def _unknown_file(path: str) -> ToolError:
    """A file the index has never seen.

    Worth an error rather than an empty answer: an empty file summary is
    indistinguishable from a file that holds nothing, and the two call for
    completely different next steps.
    """
    return ToolError(
        f"the index has not seen {path}",
        {"error": f"the index has not seen {path}", "file": path,
         "hint": "it may be outside the project, generated, or newer than the "
                 "index"},
    )


def _get_file(query: Query, args: Dict[str, Any]) -> Dict[str, Any]:
    path = _text(args, "path", required=True)
    limit = _int(args, "limit", 30, 1, MAX_LIST)
    info = query.file(path)
    if info is None:
        raise _unknown_file(path)
    tus = info.pop("translation_units", [])
    info["translation_unit_count"] = len(tus)
    if tus:
        _cut(info, "translation_units", tus, limit)
        # A header has no language of its own: `.h` is C or C++ depending on
        # who includes it, so it is read off the translation units that do.
        languages = sorted({language_of(t) for t in tus} - {""})
        if languages:
            info["language"] = languages[0] if len(languages) == 1 else languages
    if "language" not in info:
        own = language_of(path)
        if own:
            info["language"] = own
    for key in ("includes", "included_by"):
        entries = info.get(key) or []
        if len(entries) > limit:
            info[f"{key}_count"] = len(entries)
            info[key] = entries[:limit]
    return info


def _get_file_symbols(query: Query, args: Dict[str, Any]) -> Dict[str, Any]:
    path = _text(args, "path", required=True)
    limit = _int(args, "limit", 100, 1, MAX_LIST)
    kind = _text(args, "kind") or None
    if not query.has_file(path):
        raise _unknown_file(path)
    entries = query.file_symbols(path, kind=kind, limit=limit + 1)
    out: Dict[str, Any] = {"file": query.rel(path) if "/" in path else path}
    _cut(out, "symbols", entries, limit, "symbol_count",
         count=lambda: query.count_file_symbols(path, kind))
    if kind:
        out["kind"] = kind
    if not entries:
        out["note"] = ("nothing is declared or defined here"
                       if not kind else f"nothing of kind {kind!r} here")
    return out


def _get_includes(query: Query, args: Dict[str, Any]) -> Dict[str, Any]:
    path = _text(args, "path", required=True)
    if not query.has_file(path):
        raise _unknown_file(path)
    limit = _int(args, "limit", 50, 1, MAX_LIST)
    # The lists come back whole and are cut here, so a cut one reports the
    # number it was cut from.  Cutting inside the query would leave nothing
    # downstream able to tell a page from the whole list.
    out = query.includes(path, transitive=_flag(args, "transitive"))
    for key in ("includes", "included_by", "includes_transitively",
                "included_by_transitively"):
        entries = out.get(key)
        if entries:
            _cut(out, key, entries, limit)
    return out


def _get_file_dependencies(query: Query, args: Dict[str, Any]
                           ) -> Dict[str, Any]:
    path = _text(args, "path", required=True)
    if not query.has_file(path):
        raise _unknown_file(path)
    limit = _int(args, "limit", 50, 1, MAX_LIST)
    out = query.file_dependencies(path)
    for key in ("includes_transitively", "included_by_transitively"):
        entries = out.get(key)
        if entries:
            _cut(out, key, entries, limit)
    out["note"] = ("what this file needs, transitively: every header it "
                   "reaches, and every file that would be recompiled if it "
                   "changed")
    return out


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------

def _get_source_context(query: Query, args: Dict[str, Any]) -> Dict[str, Any]:
    reference = _text(args, "symbol", required=True)
    out = query.source_context(
        reference,
        context_lines=_int(args, "context_lines", 20, 0, 200),
        path=_text(args, "path") or None,
    )
    if "error" in out and "source" not in out:
        # Reported as a failure because the caller asked for source and there
        # is none; the candidates, when there are any, come with it.
        raise ToolError(out["error"], out)
    return out


# ---------------------------------------------------------------------------
# Change
# ---------------------------------------------------------------------------

def _get_changed_symbols(query: Query, args: Dict[str, Any]) -> Dict[str, Any]:
    revision = _text(args, "revision") or "HEAD"
    staged = _flag(args, "staged")
    depth = _int(args, "depth", 3, 1, 8)
    limit = _int(args, "limit", 50, 1, MAX_LIST)
    try:
        d = git_mod.diff(query.root, revision, staged=staged)
    except git_mod.GitError as exc:
        raise ToolError(
            f"cannot read a diff at {revision}: {exc}",
            {"error": f"cannot read a diff at {revision}: {exc}",
             "revision": revision},
        )
    if _flag(args, "include_impact"):
        out = changes_mod.impact_of_changes(query, d, depth=depth, limit=limit)
    else:
        out = changes_mod.changed_symbols(query, d, limit=limit)
    if not d.files:
        out["note"] = f"{d.revision} differs from {revision} in no file"
    return out


# ---------------------------------------------------------------------------
# The index itself
# ---------------------------------------------------------------------------

# What each configuration source means for how much to trust the answer.  A
# caller has to be able to tell a semantic index from a guess, or it will read
# a missing edge as a fact about the code instead of a fact about the build.
_CONFIG_WORDS = {
    "compile_commands.json": "exact",
    "auto-detected": "approximate",
    "fallback": "degraded",
}
_CONFIG_NOTES = {
    "auto-detected": ("compiler arguments were inferred rather than read from "
                      "a compilation database, so some declarations may be "
                      "missing"),
    "fallback": ("no compilation database was used: include paths and language "
                 "standard are guesses, so headers and the declarations behind "
                 "them may be missing"),
}


def _get_index_status(query: Query, args: Dict[str, Any]) -> Dict[str, Any]:
    store = query.store
    out: Dict[str, Any] = {
        "root": str(query.root) if query.root else "",
        "database": str(store.path),
        "schema_version": store.get_meta("schema_version") or "",
    }
    try:
        out["database_bytes"] = os.path.getsize(store.path)
    except OSError:
        pass

    stats = store.stats()
    out.update(stats)
    built_at = store.get_meta("indexed_at")
    if built_at:
        out["built_at"] = built_at

    tus = store.tu_records()
    sources: Dict[str, int] = {}
    for row in tus:
        sources[row["config_source"] or "unknown"] = \
            sources.get(row["config_source"] or "unknown", 0) + 1
    if sources:
        out["compiler_arguments"] = sources
        out["accuracy"] = min((_CONFIG_WORDS.get(s, "unknown") for s in sources),
                              key=lambda w: ("exact", "approximate", "degraded",
                                             "unknown").index(w))
    else:
        out["accuracy"] = "empty"
        out["note"] = ("no translation units have been indexed; run "
                       "`cpp-code-graph index`")
    for source, word in _CONFIG_NOTES.items():
        if sources.get(source):
            out.setdefault("warnings", []).append(word)

    # Whether the index still describes the tree it was built from.  Answered
    # only when the index recorded a revision, because a guess here is worse
    # than silence: an agent that trusts a stale index reasons confidently
    # about code that has moved.
    recorded = store.get_meta("git_revision")
    if recorded:
        out["indexed_revision"] = recorded
        try:
            head = git_mod.run_git(query.root, ["rev-parse", "--short", "HEAD"],
                                   check=False).strip()
        except git_mod.GitError:
            head = ""
        if head:
            out["current_revision"] = head
            out["index_matches_working_tree"] = head == recorded
    if stats.get("degraded_tus"):
        out.setdefault("warnings", []).append(
            f"{stats['degraded_tus']} translation unit(s) were analysed from a "
            "fallback configuration"
        )
    if stats.get("failed_tus"):
        out.setdefault("warnings", []).append(
            f"{stats['failed_tus']} translation unit(s) reported errors, so "
            "constructs behind them may be missing"
        )
    return out


def _get_diagnostics(query: Query, args: Dict[str, Any]) -> Dict[str, Any]:
    path = _text(args, "path")
    limit = _int(args, "limit", 25, 1, MAX_LIST)
    # `count` is how many the index holds and `diagnostics` is how many are
    # shown; the two differing is the answer, not a discrepancy.  Reporting
    # the length of the page as the count said "three warnings" on a build
    # that had three hundred.
    entries = query.diagnostics(path or None, limit=limit)
    out: Dict[str, Any] = {
        "count": query.count_diagnostics(path or None),
        "diagnostics": entries,
    }
    out["note"] = ("errors and warnings the compiler raised while indexing; "
                   "constructs guarded by a failed declaration may be absent "
                   "from the graph")
    return out


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}

TOOLS: Tuple[Tool, ...] = (
    Tool(
        name="get_index_status",
        title="Index status",
        description=(
            "What the index covers and how much to trust it. Start here when "
            "an answer looks empty or incomplete: it reports the counts, the "
            "compiler arguments the analysis used, whether the index still "
            "describes the working tree, and any translation unit that failed "
            "to compile."
        ),
        schema=_schema({}),
        handler=_get_index_status,
    ),
    Tool(
        name="find_symbol",
        title="Find a symbol",
        description=(
            "Resolve a name to the symbols it could mean, with a `file:line` "
            "for each. Use this first when you have a name but not a location: "
            "several symbols often share one, and this says which is which "
            "instead of picking one. Namespaces are prefixes (`Foo::resize`), "
            "and a parameter list picks an overload."
        ),
        schema=_schema({
            "reference": dict(_SYMBOL, description=(
                "The name to resolve, with or without qualification and "
                "parameters.")),
            "path": _PATH,
            "limit": _LIMIT,
        }, ["reference"]),
        handler=_find_symbol,
    ),
    Tool(
        name="get_symbol",
        title="Describe a symbol",
        description=(
            "Everything the index knows about one symbol: kind, signature, "
            "return type for a function, flags such as virtual or static, "
            "where it is declared and defined, what encloses it, and how many "
            "callers, callees and references it has. Pass a `location` from an "
            "earlier result to name one exactly."
        ),
        schema=_schema({"symbol": _SYMBOL, "path": _PATH}, ["symbol"]),
        handler=_get_symbol,
    ),
    Tool(
        name="get_function",
        title="Describe a function or method",
        description=(
            "A callable in detail: signature, return type, definition site, "
            "the class or namespace it belongs to, and how many places call it "
            "and it calls. Use get_callers or get_callees for the lists "
            "themselves."
        ),
        schema=_schema({"symbol": _SYMBOL, "path": _PATH}, ["symbol"]),
        handler=_get_function,
    ),
    Tool(
        name="get_class",
        title="Describe a class, struct or enum",
        description=(
            "A type in detail: bases and derived classes, ancestors and "
            "descendants, overrides, and its members split into methods, "
            "fields and nested types. This is how to answer \"what implements "
            "this interface\" and \"what is in this class\" without reading "
            "the header."
        ),
        schema=_schema({"symbol": _SYMBOL, "path": _PATH, "limit": _LIMIT},
                       ["symbol"]),
        handler=_get_class,
    ),
    Tool(
        name="search_symbols",
        title="Search symbol names",
        description=(
            "Substring search over every name in the index, optionally "
            "narrowed to one kind (`class`, `function`, `method`, `field`, "
            "`variable`, `typedef`, `namespace`). Use it when you do not know "
            "the exact spelling, or to survey a subsystem: search for a prefix "
            "to see everything under it."
        ),
        schema=_schema({
            "query": {"type": "string",
                      "description": "Text to look for inside a name."},
            "kind": {"type": "string", "description": "Restrict to one kind."},
            "limit": _LIMIT,
            "include_system": {"type": "boolean", "description":
                               "Include symbols from system headers."},
        }, ["query"]),
        handler=_search_symbols,
    ),
    Tool(
        name="get_callers",
        title="Who calls this",
        description=(
            "The functions that call this one, with the call site for each. "
            "Resolved semantically, so an overload or a method on another class "
            "with the same name is not confused with this one."
        ),
        schema=_schema({
            "symbol": _SYMBOL, "path": _PATH, "limit": _LIMIT,
            "include_system": {"type": "boolean", "description":
                               "Include callers from system headers."},
        }, ["symbol"]),
        handler=_get_callers,
    ),
    Tool(
        name="get_callees",
        title="What this calls",
        description=(
            "The functions this one calls, with the call site for each. A call "
            "whose target cannot be resolved statically - through a function "
            "pointer, or a call the index could not tie to a declaration - is "
            "marked so rather than dropped, because a missing call reads as a "
            "call that does not exist."
        ),
        schema=_schema({
            "symbol": _SYMBOL, "path": _PATH, "limit": _LIMIT,
            "include_system": {"type": "boolean", "description":
                               "Include callees in system headers."},
        }, ["symbol"]),
        handler=_get_callees,
    ),
    Tool(
        name="get_references",
        title="Where this is used",
        description=(
            "Non-call uses of a symbol: reads, writes, addresses taken, and "
            "calls made through a function pointer. Together with get_callers "
            "this is the full set of places the index knows about."
        ),
        schema=_schema({
            "symbol": _SYMBOL, "path": _PATH, "limit": _LIMIT,
            "include_system": {"type": "boolean",
                               "description": "Include system headers."},
        }, ["symbol"]),
        handler=_get_references,
    ),
    Tool(
        name="get_inheritance",
        title="Inheritance around a type",
        description=(
            "Bases and derived classes, transitively when asked, plus which "
            "methods override which. Answers \"what derives from Base\" and "
            "\"where is this interface implemented\"."
        ),
        schema=_schema({
            "symbol": _SYMBOL, "path": _PATH, "limit": _LIMIT,
            "transitive": {"type": "boolean", "description":
                           "Include ancestors and descendants beyond the "
                           "direct ones (default true)."},
        }, ["symbol"]),
        handler=_get_inheritance,
    ),
    Tool(
        name="get_symbol_dependencies",
        title="What a symbol depends on",
        description=(
            "The outgoing relationships of one symbol, grouped by kind: the "
            "types of its parameters and return value, the types of its "
            "fields, the classes it inherits from, and what it calls. This is "
            "the \"what would I have to look at to change this\" query."
        ),
        schema=_schema({"symbol": _SYMBOL, "path": _PATH, "limit": _LIMIT},
                       ["symbol"]),
        handler=_get_symbol_dependencies,
    ),
    Tool(
        name="get_file",
        title="Describe a file",
        description=(
            "A file: its language, whether it belongs to the project, what it "
            "includes and is included by, and the translation units it is "
            "compiled into - which is what a change to a header propagates "
            "through."
        ),
        schema=_schema({"path": _FILE, "limit": _LIMIT}, ["path"]),
        handler=_get_file,
    ),
    Tool(
        name="get_file_symbols",
        title="What a file declares or defines",
        description=(
            "Every symbol declared or defined in a file, with both locations "
            "when they differ. Use it to see the contents of a header without "
            "reading it."
        ),
        schema=_schema({
            "path": _FILE,
            "kind": {"type": "string", "description": "Restrict to one kind."},
            "limit": _LIMIT,
        }, ["path"]),
        handler=_get_file_symbols,
    ),
    Tool(
        name="get_includes",
        title="Includes of a file",
        description=(
            "What a file includes and what includes it, directly or "
            "transitively. The direct form is the source-level view; the "
            "transitive form is the dependency closure."
        ),
        schema=_schema({
            "path": _FILE,
            "transitive": {"type": "boolean", "description":
                           "Follow includes through the files they include."},
            "limit": _LIMIT,
        }, ["path"]),
        handler=_get_includes,
    ),
    Tool(
        name="get_file_dependencies",
        title="Dependency closure of a file",
        description=(
            "Every header a file reaches, and every file whose recompilation "
            "depends on it. This is the query for \"what does allocator.h pull "
            "in\" and \"what would rebuild if I changed it\"."
        ),
        schema=_schema({"path": _FILE, "limit": _LIMIT}, ["path"]),
        handler=_get_file_dependencies,
    ),
    Tool(
        name="get_source_context",
        title="Read around a symbol",
        description=(
            "A small region of source around a symbol - its definition where "
            "there is one, its declaration otherwise, padded by the requested "
            "number of lines and returned with line numbers. This is the only "
            "tool that returns source, and it returns a region rather than a "
            "file on purpose."
        ),
        schema=_schema({
            "symbol": _SYMBOL, "path": _PATH,
            "context_lines": {"type": "integer", "minimum": 0, "maximum": 200,
                              "description":
                              "Lines of context on each side (default 20)."},
        }, ["symbol"]),
        handler=_get_source_context,
    ),
    Tool(
        name="get_impact_analysis",
        title="What a change could affect",
        description=(
            "What is affected if this symbol changes, in three degrees that "
            "are not equally certain: `direct` is a dependency the index "
            "records, `indirect` is reached through the callers of those, and "
            "`possible` holds only if run-time dispatch reaches it - a virtual "
            "call, a function pointer, or a template instantiation. Each entry "
            "says why."
        ),
        schema=_schema({
            "symbol": _SYMBOL, "path": _PATH,
            "depth": {"type": "integer", "minimum": 1, "maximum": 8,
                      "description":
                      "How many call hops to follow (default 3)."},
            "limit": _LIMIT,
        }, ["symbol"]),
        handler=_get_impact_analysis,
    ),
    Tool(
        name="get_changed_symbols",
        title="What a diff changed",
        description=(
            "The symbols a commit, a range or the working tree changed, "
            "innermost first, each with the lines and the scope that contains "
            "it. With `include_impact`, it also says what those changes could "
            "affect. A file the index has not seen is named rather than "
            "skipped: an empty answer would read as \"nothing changed\"."
        ),
        schema=_schema({
            "revision": {"type": "string", "description":
                         "A commit, a range such as `main...HEAD`, or "
                         "`worktree` for everything uncommitted (default "
                         "HEAD)."},
            "staged": {"type": "boolean", "description":
                       "Restrict the working tree comparison to staged "
                       "changes."},
            "include_impact": {"type": "boolean", "description":
                               "Also report what the changed symbols affect."},
            "depth": {"type": "integer", "minimum": 1, "maximum": 8},
            "limit": _LIMIT,
        }),
        handler=_get_changed_symbols,
    ),
    Tool(
        name="get_diagnostics",
        title="Compiler errors and warnings",
        description=(
            "Where the analysis could not see the whole translation unit. A "
            "file the compiler rejected contributes the declarations it got "
            "through and nothing behind the error, so an absent symbol may "
            "mean a broken build rather than absent code."
        ),
        schema=_schema({
            "path": _FILE, "limit": _LIMIT,
        }),
        handler=_get_diagnostics,
    ),
)

BY_NAME: Dict[str, Tool] = {t.name: t for t in TOOLS}


def describe() -> List[Dict[str, Any]]:
    """The tool list as MCP wants it."""
    return [
        {
            "name": t.name,
            "title": t.title,
            "description": t.description,
            "inputSchema": t.schema,
            "annotations": READ_ONLY,
        }
        for t in TOOLS
    ]


def call(query: Query, name: str, arguments: Optional[Dict[str, Any]] = None
         ) -> Dict[str, Any]:
    """Run one tool.  Raises ToolError when the question cannot be answered."""
    tool = BY_NAME.get(name)
    if tool is None:
        raise ToolError(f"no such tool: {name}",
                        {"error": f"no such tool: {name}",
                         "tools": sorted(BY_NAME)})
    args = arguments or {}
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    return tool.handler(query, args)
