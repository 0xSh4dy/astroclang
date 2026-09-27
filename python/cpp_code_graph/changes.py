"""What a diff changed, in terms of symbols rather than lines.

A diff says which lines moved.  A reviewer needs to know which *functions*
moved, because a line is only meaningful inside the thing that contains it,
and because "this change is inside Foo::resize" is the fact that leads to
every next question.

So each changed range is attributed to the innermost symbol that contains it,
with the enclosing chain kept alongside - a change inside a method is also a
change to its class.  Lines that fall inside nothing the index knows about are
counted rather than dropped: a diff over a file the index has never seen, or
over a region between declarations, would otherwise appear to have changed
nothing at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .discovery import HEADER_EXTENSIONS, SOURCE_EXTENSIONS
from .git import Diff, collapse_ranges
from .query import Query

INDEXABLE_SUFFIXES = SOURCE_EXTENSIONS + HEADER_EXTENSIONS

# A range is attributed to the innermost container, but the chain around it is
# worth reporting: a change inside a method is a change to its class too.
MAX_ENCLOSING = 4
DEFAULT_SYMBOL_LIMIT = 100


def changed_symbols(query: Query, diff: Diff,
                    limit: int = DEFAULT_SYMBOL_LIMIT) -> Dict[str, Any]:
    """The symbols a diff touches, innermost container first."""
    out: Dict[str, Any] = {
        "revision": diff.revision,
        "subject": diff.subject,
        "files": [f.describe() for f in diff.files],
    }

    attributed: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    unattributed: Dict[str, int] = {}
    unknown_files: List[str] = []
    untouched: List[str] = []

    for path, ranges in diff.for_index():
        rel = query.rel(path)
        if not query.has_file(path):
            # Only a source or header file is something the index is expected
            # to hold.  Reporting a changed README as "not indexed" would
            # suggest a gap where there is none.
            if Path(path).suffix.lower() in INDEXABLE_SUFFIXES:
                unknown_files.append(rel)
            continue
        hit = False
        for start, end in ranges:
            containers = query.containers_in_range(path, start, end)
            if not containers:
                # Not inside a function or a class: a declaration, an include,
                # a macro.  The symbol sitting on those lines is still worth
                # naming even though nothing contains it.
                containers = query.symbols_in_range(path, start, end)
            primary = containers[0] if containers else None
            if primary is None:
                unattributed[rel] = unattributed.get(rel, 0) + end - start + 1
                continue
            hit = True
            key = primary["symbol"] + primary.get("signature", "")
            entry = attributed.get(key)
            if entry is None:
                entry = dict(primary)
                entry["lines"] = []
                # Resolved by location rather than by name: a changed symbol
                # is very often an overload, and `geo::scale` alone names
                # three of them.
                entry["_usr"], _ = query.resolve_one(
                    primary.get("location") or primary["symbol"])
                attributed[key] = entry
                order.append(key)
            entry["lines"].append((start, end))
        if not hit:
            untouched.append(rel)

    symbols = []
    for key in order[:limit]:
        entry = dict(attributed[key])
        usr = entry.pop("_usr", None)
        entry["lines"] = collapse_ranges(entry["lines"])
        if usr:
            # The lexical chain, from the parent links rather than from source
            # ranges - a function's namespace is rarely on the same lines as
            # its body.
            within = [a["symbol"] for a in query.ancestors(usr)][:MAX_ENCLOSING]
            if within:
                entry["within"] = within
        symbols.append(entry)

    out["changed_symbols"] = symbols
    out["symbol_count"] = len(order)
    if len(order) > limit:
        out["truncated"] = True
    if unknown_files:
        # A file the index has never seen.  Saying so is the point: an empty
        # answer here would read as "nothing changed".
        out["not_indexed"] = unknown_files
    if untouched:
        out["changed_files_without_symbols"] = untouched
    if unattributed:
        out["lines_outside_any_symbol"] = unattributed
    return out


def impact_of_changes(query: Query, diff: Diff, depth: int = 3,
                      limit: int = 40) -> Dict[str, Any]:
    """Who is affected by what the diff changed, in the three degrees.

    The changed symbols themselves are removed from the result: they are the
    subject of the question, not part of its answer.
    """
    base = changed_symbols(query, diff)
    changed = base.get("changed_symbols", [])
    if not changed:
        return {**base, "affected": {}}

    touched = {s["symbol"] for s in changed}
    buckets: Dict[str, Dict[str, Dict[str, Any]]] = {
        "direct": {}, "indirect": {}, "possible": {}}
    notes: List[str] = []

    unresolved: List[str] = []
    for entry in changed:
        # By location, not by name.  A changed symbol is very often an
        # overload, and `geo::scale` alone names three of them - resolving by
        # name would either fail or, worse, succeed against the wrong one.
        usr, failure = query.resolve_one(
            entry.get("location") or entry["symbol"])
        if usr is None:
            unresolved.append(entry["symbol"])
            continue
        report = query.impact(usr, depth=depth, limit=limit)
        notes.append(f"{entry['symbol']}: {report.get('note', '')}".strip())
        for bucket in buckets:
            for item in report.get(bucket, []):
                if item["symbol"] in touched:
                    continue
                item = dict(item)
                item.setdefault("because_of", entry["symbol"])
                buckets[bucket].setdefault(item["symbol"], item)

    affected = {k: list(v.values()) for k, v in buckets.items() if v}
    out = {
        "revision": base["revision"],
        "subject": base["subject"],
        "changed_symbols": changed,
        "affected": affected,
        "affected_count": sum(len(v) for v in affected.values()),
    }
    if unresolved:
        # Silently dropping these would understate the change, which is the
        # one thing an impact analysis must not do.
        out["could_not_resolve"] = sorted(set(unresolved))
    return out


def summarise(diff: Diff, symbols: Optional[Dict[str, Any]] = None) -> str:
    """One line for a human, used by the command line and by logs."""
    files = len(diff.files)
    if symbols is None:
        return f"{diff.revision}: {files} file(s) changed"
    return (f"{diff.revision}: {files} file(s), "
            f"{symbols.get('symbol_count', 0)} symbol(s) changed")
