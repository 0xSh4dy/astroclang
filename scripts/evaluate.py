#!/usr/bin/env python3
"""Measure the index over a real project.

Not a test: it asserts nothing, it reports.  What it reports is what the
specification asks a C/C++ index to be judged on - how long a full index takes,
how much memory the extractor needs, how large the database gets, how long the
queries an agent actually asks take to answer, how often a resolved reference
turns out not to resolve to anything, and what a one-file change costs.

    scripts/evaluate.py --root /path/to/project --label fmt

Writes nothing outside the index directory unless --json is given, in which
case the numbers go to stdout as JSON so a run can be compared with a later
one.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from cpp_code_graph import indexer  # noqa: E402
from cpp_code_graph.query import Query  # noqa: E402
from cpp_code_graph.store import Store, default_db_path  # noqa: E402
from cpp_code_graph import tools  # noqa: E402

# What an agent asks, in the order it tends to ask it: find the thing, look at
# it, then walk outward.  Each entry is a tool call the MCP server would serve.
QUERIES = [
    ("find_symbol", {"reference": "begin"}),
    ("search_symbols", {"query": "format", "limit": 20}),
    ("get_symbol", None),          # filled in with a symbol from the index
    ("get_callers", None),
    ("get_callees", None),
    ("get_impact_analysis", None),
    ("get_source_context", None),
    ("get_index_status", {}),
]

REPEATS = 20


def peak_child_rss() -> int:
    """Peak RSS of every extractor process this run has waited on, in bytes."""
    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024


def directory_size(path: Path) -> int:
    total = 0
    for entry in path.parent.glob(path.name + "*"):  # the index plus its WAL
        try:
            total += entry.stat().st_size
        except OSError:
            pass
    return total


def pick_subjects(store: Store) -> dict:
    """One symbol of each interesting shape, to aim the queries at."""
    rows = store.connection().execute(
        "SELECT usr, qualified, kind, signature FROM symbol"
        " WHERE stub = 0 AND file_id IN (SELECT id FROM file WHERE in_project = 1)"
        " ORDER BY (SELECT COUNT(*) FROM raw_edge WHERE dst = symbol.usr) DESC"
    ).fetchall()
    subject = {}
    for row in rows:
        if row["kind"] in ("function", "method", "constructor", "destructor") \
                and "symbol" not in subject:
            subject["symbol"] = row["usr"]
        if row["kind"] in ("class", "struct") and "class" not in subject:
            subject["class"] = row["usr"]
    subject.setdefault("symbol", rows[0]["usr"] if rows else "")
    return subject


def timed(fn, repeats: int) -> dict:
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - started) * 1000.0)
    samples.sort()
    return {
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(samples[int(len(samples) * 0.95) - 1], 3),
        "max_ms": round(samples[-1], 3),
        "payload_bytes": len(json.dumps(result, separators=(",", ":"))),
    }


def measure_queries(query: Query) -> dict:
    subject = pick_subjects(query.store)
    usr = subject["symbol"]
    out = {}
    for name, args in QUERIES:
        if args is None:
            args = {"symbol": usr} if name != "get_source_context" \
                else {"symbol": usr, "context_lines": 20}
            if name == "get_impact_analysis":
                args = {"symbol": usr, "limit": 10}
            if name == "get_callees":
                args = {"symbol": usr, "limit": 10}
            if name == "get_callers":
                args = {"symbol": usr, "limit": 10}
        try:
            out[name] = timed(lambda n=name, a=args: tools.call(query, n, a),
                              REPEATS)
        except tools.ToolError as exc:
            out[name] = {"error": str(exc)}
    return out


def measure_resolution(store: Store) -> dict:
    """How much of the graph resolved, and to what.

    A call edge whose destination is not a symbol the index holds is a
    reference the extractor saw but could not place - usually a declaration in
    a header that was never parsed.  That rate is the honest measure of how
    much to trust the graph, so it is measured rather than assumed.
    """
    c = store.connection()
    total = c.execute("SELECT COUNT(*) FROM raw_edge").fetchone()[0]
    dangling = c.execute(
        "SELECT COUNT(*) FROM raw_edge e"
        " WHERE NOT EXISTS (SELECT 1 FROM symbol s WHERE s.usr = e.dst)"
    ).fetchone()[0]
    in_project = c.execute(
        "SELECT COUNT(*) FROM raw_edge e JOIN symbol s ON s.usr = e.dst"
        " JOIN file f ON f.id = s.file_id WHERE f.in_project = 1"
    ).fetchone()[0]
    by_kind = {row[0]: row[1] for row in c.execute(
        "SELECT kind, COUNT(*) FROM raw_edge GROUP BY kind ORDER BY 2 DESC")}
    kinds = {row[0]: row[1] for row in c.execute(
        "SELECT kind, COUNT(*) FROM symbol GROUP BY kind ORDER BY 2 DESC")}
    return {
        "edges": total,
        "edges_resolved_in_project": in_project,
        "edges_unresolved": dangling,
        "resolution_rate": round((total - dangling) / total, 4) if total else None,
        "edges_by_kind": by_kind,
        "symbols_by_kind": kinds,
    }


def run(root: Path, compdb: Path | None, label: str, jobs: int) -> dict:
    root = root.resolve()
    db = default_db_path(root)
    if db.parent.exists():
        shutil.rmtree(db.parent)

    result: dict = {"label": label, "root": str(root)}

    store = Store(db, project_root=root)
    try:
        started = time.monotonic()
        report = indexer.index_project(root, store, compdb=compdb, jobs=jobs)
        cold = time.monotonic() - started

        result["index"] = {
            "translation_units": report.total,
            "indexed": report.indexed,
            "failed": report.failed,
            "degraded": report.degraded,
            "seconds": round(cold, 2),
            "seconds_per_tu": round(cold / report.total, 3) if report.total else None,
            "peak_child_rss_mb": round(peak_child_rss() / 1024 / 1024, 1),
            "notes": report.notes,
        }
        stats = store.stats()
        result["database"] = {
            "bytes": directory_size(db),
            "bytes_per_symbol": round(directory_size(db) / stats["symbols"], 1)
            if stats["symbols"] else None,
            **stats,
        }
        result["resolution"] = measure_resolution(store)

        query = Query(store)
        result["queries"] = measure_queries(query)
        result["meta"] = {k: store.get_meta(k) for k in ("schema_version",)}

        # Incremental: touch one source file's contents and re-index.
        source = next((f for f in indexer.plan_indexing(root, compdb).files
                       if f.suffix in (".cc", ".cpp", ".cxx", ".c")), None)
        if source is not None:
            original = source.read_text()
            started = time.monotonic()
            try:
                source.write_text(original + "\n// evaluate.py\n")
                again = indexer.index_project(root, store, compdb=compdb, jobs=jobs)
                incremental = time.monotonic() - started
                result["incremental"] = {
                    "file": str(source.relative_to(root)),
                    "seconds": round(incremental, 2),
                    "re_indexed": again.indexed,
                    "unchanged": again.unchanged,
                    "speedup": round(cold / incremental, 1) if incremental else None,
                }
            finally:
                source.write_text(original)

        # A rebuild of the merged symbol table, which is the part that scales
        # with the whole project rather than with what changed.
        started = time.monotonic()
        store.rebuild_symbols()
        result["merge_seconds"] = round(time.monotonic() - started, 2)
    finally:
        store.close()
    return result


def describe(result: dict) -> str:
    i, d, r = result["index"], result["database"], result["resolution"]
    lines = [
        f"== {result['label']}  {result['root']}",
        f"   {i['translation_units']} translation units in {i['seconds']}s "
        f"({i['seconds_per_tu']}s each), {i['failed']} failed, "
        f"{i['degraded']} degraded",
        f"   {i['peak_child_rss_mb']} MB peak extractor RSS",
        f"   {d['symbols']} symbols, {d['edges']} edges, "
        f"{d['bytes'] / 1024 / 1024:.1f} MB index "
        f"({d['bytes_per_symbol']} bytes/symbol)",
        f"   resolution: {r['edges_resolved_in_project']}/{r['edges']} in project, "
        f"{r['edges_unresolved']} unresolved "
        f"({r['resolution_rate']:.1%} resolved)",
    ]
    if "incremental" in result:
        n = result["incremental"]
        lines.append(f"   one file changed: {n['seconds']}s vs {i['seconds']}s cold "
                     f"({n['speedup']}x), {n['re_indexed']} re-indexed / "
                     f"{n['unchanged']} unchanged")
    lines.append(f"   merge {result['merge_seconds']}s")
    for name, q in result["queries"].items():
        if "error" in q:
            lines.append(f"   {name:<22} n/a: {q['error'][:60]}")
        else:
            lines.append(f"   {name:<22} {q['median_ms']:>7.2f} ms median, "
                         f"{q['p95_ms']:>7.2f} ms p95, "
                         f"{q['payload_bytes']:>6} B payload")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--compdb", type=Path, default=None)
    parser.add_argument("--label", default="")
    parser.add_argument("--jobs", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run(args.root, args.compdb, args.label or args.root.name, args.jobs)
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
    else:
        print(describe(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
