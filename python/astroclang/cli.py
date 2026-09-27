"""The command line: build the index, serve it, and ask it things.

The query subcommands are declared in a table and run through the same
`tools.call` the MCP server uses, so the two front ends cannot drift into
answering the same question differently.  What the CLI adds is argument
parsing, a readable rendering of the common answers, and exit statuses a shell
can branch on.

Three exit statuses, and the distinction matters to a script:

  0  the question was answered, even if the answer was "nothing"
  1  the question could not be asked - no index, an unknown file, an ambiguous
     name, a revision git cannot resolve
  2  the command line itself was wrong
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import textwrap
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import git as git_mod, http_server, indexer, tools
from .mcp_server import open_index, serve_stdio
from .query import Query
from .store import DEFAULT_INDEX_DIR, Store, default_db_path, read_meta

PROGRAM = "astroclang"

OK = 0
FAILED = 1
USAGE = 2


# -- rendering ---------------------------------------------------------------

def _emit(payload: Any, as_json: bool,
          human: Optional[Callable[[Dict[str, Any]], str]] = None) -> None:
    if as_json or human is None:
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    sys.stdout.write(human(payload))
    sys.stdout.write("\n")


def _where(entry: Dict[str, Any]) -> str:
    """`symbol  file:line` - the handle an answer is carried by.

    Two shapes carry a location: a symbol, which has `symbol` and `location`,
    and a file-shaped entry, which has `file` and `line`.  Both print as the
    `file:line` spelling that every tool here accepts back as an argument.
    """
    if entry.get("symbol"):
        return f"{entry['symbol']}  {entry.get('location', '')}".rstrip()
    if entry.get("file"):
        line = entry.get("line")
        return f"{entry['file']}:{line}" if line else str(entry["file"])
    return str(entry)


def _wrap(note: str, indent: str = "  ") -> str:
    return textwrap.fill(note, width=88, initial_indent=indent,
                         subsequent_indent=indent)


def _render_find(payload: Dict[str, Any]) -> str:
    matches = payload.get("matches", [])
    if not matches:
        return "nothing matches." + _note(payload)
    lines = [_where(m) for m in matches]
    if payload.get("match_count", len(matches)) > len(matches):
        lines.append(f"... {payload['match_count']} in all")
    return "\n".join(lines)


def _note(payload: Dict[str, Any], indent: str = "  ") -> str:
    return _wrap(payload["note"], indent) if payload.get("note") else ""


def _listing(key: str, count_key: str):
    """A renderer for the answer shape `_neighbours` produces."""
    def render(payload: Dict[str, Any]) -> str:
        entries = payload.get(key, [])
        lines = [_where(payload)] if payload.get("symbol") else []
        if not entries:
            lines.append("nothing.")
            if payload.get("note"):
                lines.append(_note(payload))
            return "\n".join(lines)
        for entry in entries:
            site = entry.get("call_site") or entry.get("referenced_at") or ""
            lines.append(f"  {_where(entry)}" + (f"  (at {site})" if site else ""))
        total = payload.get(count_key, len(entries))
        if total > len(entries):
            lines.append(f"  ... {total} in all")
        return "\n".join(lines)
    return render


def _render_symbol(payload: Dict[str, Any]) -> str:
    lines = [f"{payload.get('symbol', '')}  {payload.get('signature', '')}"
             f"   [{payload.get('kind', '')}]  {payload.get('location', '')}"]
    if payload.get("type"):
        lines.append(f"  type       {payload['type']}")
    if payload.get("member_of"):
        lines.append(f"  member_of  {payload['member_of']}")
    if payload.get("within"):
        # Innermost first, each already fully qualified: joining them with
        # `::` would spell the same namespace twice.
        lines.append(f"  within     {', '.join(payload['within'])}")
    for key in ("callers", "callees", "references"):
        if isinstance(payload.get(key), int):
            lines.append(f"  {key:<10} {payload[key]}")
    if payload.get("defined_at"):
        lines.append(f"  defined_at {payload['defined_at']}")
    return "\n".join(lines)


def _render_members(payload: Dict[str, Any]) -> str:
    lines = [f"{payload.get('symbol', '')}  {payload.get('location', '')}"]
    for label in ("bases", "derived", "ancestors", "descendants", "overrides",
                  "overridden", "overrides_indirectly"):
        entries = payload.get(label)
        if entries is None:
            continue
        names = ", ".join(e.get("symbol", "") for e in entries)
        lines.append(f"  {label:<20} {names or '-'}")
    for label in ("methods", "fields", "nested_types"):
        entries = payload.get(label) or []
        names = ", ".join(e.get("symbol", "") for e in entries)
        lines.append(f"  {label:<20} {names or '-'}")
    return "\n".join(lines)


def _render_impact(payload: Dict[str, Any]) -> str:
    lines = [_where(payload)] if payload.get("symbol") else []
    for label in ("direct", "indirect", "possible"):
        entries = payload.get(label) or []
        lines.append(f"{label} ({len(entries)}):")
        for entry in entries:
            reason = entry.get("reason", "")
            lines.append(f"  {_where(entry)}" + (f"  - {reason}" if reason else ""))
    if payload.get("note"):
        lines.append(_wrap(payload["note"], ""))
    return "\n".join(line for line in lines if line)


def _render_status(payload: Dict[str, Any]) -> str:
    lines = [f"{'index':<18} {payload.get('database', '')}",
             f"{'root':<18} {payload.get('root', '')}",
             f"{'accuracy':<18} {payload.get('accuracy', '')}"]
    for key in ("files", "project_files", "translation_units", "symbols",
                "symbols_in_project", "edges", "includes", "diagnostics"):
        if key in payload:
            lines.append(f"{key:<18} {payload[key]}")
    if payload.get("built_at"):
        lines.append(f"{'built_at':<18} {payload['built_at']}")
    if payload.get("index_matches_working_tree") is False:
        lines.append("WARNING            the index does not describe the current "
                     "revision; re-index before trusting an answer")
    for warning in payload.get("warnings", []):
        lines.append(f"WARNING            {warning}")
    if payload.get("note"):
        lines.append(f"note               {payload['note']}")
    return "\n".join(lines)


def _render_changed(payload: Dict[str, Any]) -> str:
    lines = [f"{payload.get('revision', '')}  {payload.get('subject', '')}",
             f"{len(payload.get('files', []))} file(s) changed"]
    for entry in payload.get("changed_symbols", []):
        lines.append(f"  {_where(entry)}")
    for path in payload.get("unknown_files", []):
        lines.append(f"  {path}  (not in the index)")
    if payload.get("affected"):
        lines.append("")
        lines.append(_render_impact(payload["affected"]))
    return "\n".join(lines)


def _render_context(payload: Dict[str, Any]) -> str:
    return (f"{payload.get('symbol', '')}  {payload.get('location', '')}\n"
            f"{payload.get('source', '')}")


# -- the commands ------------------------------------------------------------

class Command:
    """One query subcommand: the tool it runs and how its words map to it."""

    def __init__(self, tool: str, summary: str,
                 positional: Sequence[Tuple[str, str]] = (),
                 render: Optional[Callable[[Dict[str, Any]], str]] = None):
        self.tool = tool
        self.summary = summary
        self.positional = list(positional)  # (tool argument name, help text)
        self.render = render


_TARGET = ("symbol", "name, or a file:line from a previous answer")
_PATH = ("path", "path as the index recorded it")

COMMANDS: Dict[str, Command] = {
    "find": Command("find_symbol", "symbols whose name matches, with locations",
                    [("reference", "name or substring to look for")], _render_find),
    "search": Command("search_symbols", "symbols matching a query",
                      [("query", "text to search for")], _render_find),
    "symbol": Command("get_symbol", "everything known about one symbol",
                      [_TARGET], _render_symbol),
    "function": Command("get_function", "one function, or refuse if it is not one",
                        [_TARGET], _render_symbol),
    "class": Command("get_class", "a class or struct, with its members and bases",
                     [_TARGET], _render_members),
    "inheritance": Command("get_inheritance", "bases, derived classes and overrides",
                           [_TARGET], _render_members),
    "callers": Command("get_callers", "who calls this",
                       [_TARGET], _listing("callers", "caller_count")),
    "callees": Command("get_callees", "what this calls",
                       [_TARGET], _listing("callees", "callee_count")),
    "refs": Command("get_references", "who refers to this without calling it",
                    [_TARGET], _listing("references", "reference_count")),
    "deps": Command("get_symbol_dependencies",
                    "what this symbol depends on, grouped by relationship",
                    [_TARGET]),
    "impact": Command("get_impact_analysis",
                      "what may be affected by changing this, by degree",
                      [_TARGET], _render_impact),
    "context": Command("get_source_context", "a small region of source around a symbol",
                       [_TARGET], _render_context),
    "file": Command("get_file", "a file, its language and its translation units",
                    [_PATH]),
    "symbols": Command("get_file_symbols", "every symbol declared in a file",
                       [_PATH], _listing("symbols", "symbol_count")),
    "includes": Command("get_includes", "what a file includes",
                        [_PATH], _listing("includes", "include_count")),
    "file-deps": Command("get_file_dependencies", "what a file depends on, transitively",
                         [_PATH]),
    "changed": Command("get_changed_symbols",
                       "the symbols a revision changed, and their impact",
                       [("revision", "revision or range; defaults to HEAD")],
                       _render_changed),
    "status": Command("get_index_status", "what is indexed and how far to trust it",
                      [], _render_status),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="A semantic index of a C/C++ project, and the questions it "
                    "answers.",
    )
    parser.add_argument("--db", metavar="PATH", default=None,
                        help=f"index file (default: {DEFAULT_INDEX_DIR}/"
                             f"index.db under the project root)")
    parser.add_argument("--json", action="store_true",
                        help="print the answer as JSON instead of a summary")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    index = sub.add_parser("index", help="build or update the index")
    index.add_argument("root", nargs="?", default=".",
                       help="project root (default: the current directory)")
    index.add_argument("--compdb", metavar="PATH", default=None,
                       help="compilation database to use instead of searching")
    index.add_argument("--only", action="append", metavar="PATH", default=None,
                       help="index only this translation unit (repeatable)")
    index.add_argument("--limit", type=int, default=None, metavar="N",
                       help="index at most this many translation units")
    index.add_argument("--jobs", type=int, default=0, metavar="N",
                       help="translation units to analyse at once")
    index.add_argument("--extractor", metavar="PATH", default=None,
                       help="path to the astroclang-index extractor")
    index.add_argument("--timeout", type=float, default=300.0, metavar="SECONDS",
                       help="per-translation-unit timeout")
    index.add_argument("--no-incremental", action="store_true",
                       help="re-analyse every translation unit")
    index.add_argument("--quiet", action="store_true",
                       help="print nothing but the summary line")

    mcp = sub.add_parser("mcp", help="serve the index over MCP on stdin/stdout")
    mcp.add_argument("root", nargs="?", default=".",
                     help="project root (default: the current directory)")
    mcp.add_argument("--list-tools", action="store_true",
                     help="print the tool surface and exit, without serving")
    mcp.add_argument("--http", action="store_true",
                     help="listen on HTTP instead of reading stdin")
    mcp.add_argument("--host", default=http_server.DEFAULT_HOST, metavar="ADDR",
                     help=f"address to bind with --http (default: "
                          f"{http_server.DEFAULT_HOST}, this machine only)")
    mcp.add_argument("--port", type=int, default=http_server.DEFAULT_PORT,
                     metavar="N",
                     help=f"port to bind with --http (default: "
                          f"{http_server.DEFAULT_PORT}; 0 picks a free one)")
    mcp.add_argument("--allow-origin", action="append", default=None,
                     metavar="ORIGIN",
                     help="also accept this Origin, for a client that is not "
                          "on this machine (repeatable)")

    for name, command in COMMANDS.items():
        doc = sub.add_parser(name, help=command.summary)
        for arg_name, help_text in command.positional:
            doc.add_argument(arg_name, help=help_text)
        # `--path` narrows a name to one file; it is only offered where `path`
        # is not already a positional, or argparse would let the option quietly
        # overwrite the argument it was given.
        if all(arg != "path" for arg, _ in command.positional):
            doc.add_argument("--path", default=None,
                             help="restrict to symbols declared in this file")
        doc.add_argument("--limit", type=int, default=None,
                         help="how many entries to return")
    return parser


def _db_path(args) -> Path:
    if args.db:
        return Path(args.db)
    root = getattr(args, "root", None)
    if root is not None:
        return default_db_path(Path(root).resolve())
    return _find_index(Path.cwd().resolve())


def _find_index(start: Path) -> Path:
    """The nearest index at or above `start`.

    Walking up rather than demanding the root is what makes the tool usable
    from a subdirectory, which is where a person - or an agent handed a file
    path - usually is.  When there is none anywhere, the answer is the path
    where one would go, so that the error can name it.
    """
    for directory in [start, *start.parents]:
        candidate = default_db_path(directory)
        if candidate.is_file():
            return candidate
    return default_db_path(start)


def _project_root(db: Path, fallback: Path) -> Path:
    """The root the index was built against, not where we happen to be standing.

    Answering from a subdirectory would resolve `src/foo.cpp` against the wrong
    directory, and a `git diff` would be taken in the wrong repository.
    """
    return Path(read_meta(db, "root") or fallback)


# -- commands ----------------------------------------------------------------

def cmd_index(args) -> int:
    root = Path(args.root).resolve()
    db = _db_path(args)
    store = Store(db, project_root=root)
    try:
        report = indexer.index_project(
            root, store,
            extractor=Path(args.extractor) if args.extractor else None,
            compdb=Path(args.compdb) if args.compdb else None,
            jobs=args.jobs, only=args.only, limit=args.limit,
            incremental=not args.no_incremental, timeout=args.timeout,
            progress=None if args.quiet else _progress,
        )
        if report.indexed:
            _record_build(store, root)
        _report_indexing(report, db, args.quiet)
        # A run where nothing could be indexed is a failure worth exiting on;
        # a run where some units failed is a partial index, which the summary
        # above has already reported.
        return FAILED if report.indexed == 0 and report.failed else OK
    finally:
        store.close()


def _progress(message: str) -> None:
    sys.stderr.write(message + "\n")


def _record_build(store: Store, root: Path) -> None:
    """Stamp the index with what it was built from and when.

    `git_revision` is what lets a later question notice that the answers
    describe a tree that has since moved, which is worse than no answer.
    """
    store.set_meta("indexed_at",
                   datetime.datetime.now(datetime.timezone.utc)
                   .isoformat(timespec="seconds"))
    store.set_meta("root", str(root))
    try:
        head = git_mod.run_git(root, ["rev-parse", "--short", "HEAD"],
                               check=False).strip()
    except git_mod.GitError:
        head = ""
    store.set_meta("git_revision", head)


def _report_indexing(report, db: Path, quiet: bool) -> None:
    # The plan's notes have already gone to stderr through the progress
    # callback; repeating them here would say the same thing twice.
    if not quiet:
        for failure in report.failures:
            _progress(f"failed  {failure.path}: {failure.detail}")
    _progress("  ".join([f"indexed {report.indexed}",
                         f"unchanged {report.unchanged}",
                         f"failed {report.failed}",
                         f"{report.seconds:.1f}s",
                         str(db)]))
    if report.degraded:
        _progress(f"warning: {report.degraded} translation unit(s) were analysed "
                  f"from a fallback configuration, so some declarations may be "
                  f"missing")


def _announce_mcp(db: Path, root: Path, server) -> None:
    """Say on stderr what is about to be served, before the protocol starts.

    stdout carries protocol frames and nothing else, so without this a person
    who runs `astroclang mcp` by hand sees a terminal that never prints
    anything at all and cannot tell a working server from a hang.  The same
    lines land in a client's log capture, which is where they are wanted when
    the server is spawned rather than typed.

    Only what is being served, not how: the transport adds its own line, since
    only it knows the port it ended up on.
    """
    if server.missing:
        _progress(f"{PROGRAM} mcp: {server.missing}")
        _progress(f"{PROGRAM} mcp: serving anyway, so a client is told why its "
                  f"calls fail instead of finding a process that died")
    else:
        _progress(f"{PROGRAM} mcp: {len(tools.describe())} tools, index {db}")
        _progress(f"{PROGRAM} mcp: root {root}")


def cmd_mcp(args) -> int:
    if args.list_tools:
        json.dump(tools.describe(), sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return OK
    db = _db_path(args)
    root = _project_root(db, Path(args.root or ".").resolve())
    # `_progress` is also the server's log sink, so an exception raised while
    # answering a request leaves a trace on stderr instead of vanishing into
    # an error reply the client may never show anyone.  The same sink carries
    # HTTP request lines, which is why the file they are written to is the one
    # a person is already watching.
    server = open_index(db, root=root, log=_progress,
                        # Only HTTP answers on more than one thread; the stdio
                        # transport keeps the store to itself.
                        cross_thread=args.http)
    _announce_mcp(db, root, server)
    if args.http:
        return http_server.serve_http(
            server, host=args.host, port=args.port, log=_progress,
            allow_origins=args.allow_origin or ())
    _progress(f"{PROGRAM} mcp: JSON-RPC on stdin/stdout, diagnostics here on "
              f"stderr; waiting for a client")
    return serve_stdio(server)


def cmd_query(args) -> int:
    command = COMMANDS[args.command]
    arguments: Dict[str, Any] = {}
    for arg_name, _ in command.positional:
        value = getattr(args, arg_name, None)
        if value is not None:
            arguments[arg_name] = value
    if getattr(args, "path", None) is not None:
        arguments["path"] = args.path
    if args.limit is not None:
        arguments["limit"] = args.limit

    db = _db_path(args)
    if not db.is_file():
        sys.stderr.write(f"{PROGRAM}: there is no index at {db}\n"
                         f"build one with `{PROGRAM} index "
                         f"{db.parent.parent}`\n")
        return FAILED

    store = Store(db, project_root=_project_root(db, Path.cwd()))
    try:
        payload = tools.call(Query(store), command.tool, arguments)
    except tools.ToolError as exc:
        json.dump(exc.payload, sys.stderr, indent=2, ensure_ascii=False)
        sys.stderr.write("\n")
        return FAILED
    finally:
        store.close()
    _emit(payload, args.json, command.render)
    return OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(sys.stderr)
        return USAGE
    if args.command == "index":
        return cmd_index(args)
    if args.command == "mcp":
        return cmd_mcp(args)
    return cmd_query(args)


def run() -> None:
    """Entry point for the console script."""
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except BrokenPipeError:
        # A query piped into `head` is an ordinary way to use this.
        raise SystemExit(OK)


if __name__ == "__main__":
    run()
