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
import random
import resource
import shutil
import statistics
import sys
import time
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from astroclang import indexer  # noqa: E402
from astroclang.query import Query  # noqa: E402
from astroclang.store import Store, default_db_path  # noqa: E402
from astroclang import tools  # noqa: E402

# What an agent asks, in the order it tends to ask it: find the thing, look at
# it, then walk outward.  Each entry is a tool call the MCP server would serve.
# A `None` argument means "aim this at the symbol under test".
QUERIES = [
    ("get_symbol", None),
    ("get_callers", None),
    ("get_callees", None),
    ("get_impact_analysis", None),
    ("get_source_context", None),
]

# Queries with no subject: they cost the same whatever is asked about, so they
# are timed once rather than once per sampled symbol.
FIXED_QUERIES = [
    ("find_symbol", {"reference": "begin"}),
    ("search_symbols", {"query": "format", "limit": 20}),
    ("get_index_status", {}),
]

REPEATS = 20

# How many ordinary symbols the per-symbol queries are timed against.  Large
# enough that the median means something, small enough that a run over a big
# project stays minutes rather than hours.
SAMPLE_SIZE = 200
SAMPLE_SEED = 1


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


def pick_busiest(store: Store) -> str:
    """The most-called function in the project, which is the worst case.

    Deliberately the worst case: a hub with thousands of callers is where an
    index that scales badly would show it, and an agent that asks about
    `std::string::string` should get an answer rather than a timeout.  It is
    not, however, what most questions look like - hence the sample below.
    """
    row = store.connection().execute(
        "SELECT s.usr FROM symbol s JOIN file f ON f.id = s.file_id"
        " WHERE s.stub = 0 AND f.in_project = 1"
        "   AND s.kind IN ('function', 'method', 'constructor', 'destructor')"
        " ORDER BY (SELECT COUNT(*) FROM raw_edge WHERE dst = s.usr) DESC"
        " LIMIT 1"
    ).fetchone()
    return row["usr"] if row else ""


def sample_symbols(store: Store, size: int) -> List[str]:
    """A spread of ordinary functions, which is what most questions are about.

    Fixed seed: two runs over the same project must time the same symbols, or
    the comparison between them measures the draw rather than the code.
    """
    rows = store.connection().execute(
        "SELECT s.usr FROM symbol s JOIN file f ON f.id = s.file_id"
        " WHERE s.stub = 0 AND f.in_project = 1"
        "   AND s.kind IN ('function', 'method', 'constructor', 'destructor')"
        "   AND EXISTS (SELECT 1 FROM raw_edge e"
        "               WHERE e.dst = s.usr AND e.kind = 'calls')"
    ).fetchall()
    usrs = [r["usr"] for r in rows]
    if len(usrs) <= size:
        return usrs
    return random.Random(SAMPLE_SEED).sample(usrs, size)


def arguments_for(name: str, usr: str) -> dict:
    args = {"symbol": usr}
    if name == "get_source_context":
        args["context_lines"] = 20
    elif name in ("get_callers", "get_callees", "get_impact_analysis"):
        args["limit"] = 10
    return args


def timed(fn, repeats: int) -> dict:
    samples = []
    result = None
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


def _distribution(samples: List[float], payloads: List[int]) -> dict:
    samples = sorted(samples)
    payloads = sorted(payloads)
    if not samples:
        return {"samples": 0}
    return {
        "samples": len(samples),
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(samples[int(len(samples) * 0.95) - 1], 3),
        "max_ms": round(samples[-1], 3),
        "median_payload_bytes": payloads[len(payloads) // 2],
    }


def measure_queries(query: Query, label: str) -> dict:
    """Time every query over a sample of symbols, and the hub separately.

    Timing one symbol and reporting it as "the" latency is a way of being
    wrong twice: aim it at the busiest symbol and the tool looks slow, aim it
    at a quiet one and the hub's cost is hidden.  Both are real, so both are
    reported, and it is the sample that says what using the tool is like.
    """
    busiest = pick_busiest(query.store)
    sample = sample_symbols(query.store, SAMPLE_SIZE)
    if label == "hub":
        sample = [busiest] if busiest else []

    out = {}
    for name, _ in QUERIES:
        samples, payloads = [], []
        for usr in sample:
            started = time.perf_counter()
            try:
                result = tools.call(query, name, arguments_for(name, usr))
            except tools.ToolError:
                continue
            samples.append((time.perf_counter() - started) * 1000.0)
            payloads.append(len(json.dumps(result, separators=(",", ":"))))
        out[name] = _distribution(samples, payloads)

    if label == "sample":
        for name, args in FIXED_QUERIES:
            try:
                out[name] = timed(lambda n=name, a=args: tools.call(query, n, a),
                                  REPEATS)
            except tools.ToolError as exc:
                out[name] = {"error": str(exc)}
    return {"subject": busiest, "queried": out}


# Where an edge's destination came from, most useful first.  A consumer asks
# "can this index tell me what that call actually calls", and the answer is yes
# for the first bucket, partly for the second, and no for the rest.
#
# The order is the order of the CASE, so the tests come first that would
# otherwise be swallowed: a stub has a file like any other, and a symbol with
# no file at all has neither a stub flag nor a project flag to test.
BUCKETS = [
    ("defined_in_project", "s.usr IS NOT NULL AND s.stub = 0"
                           " AND f.id IS NOT NULL AND f.in_project = 1"),
    ("defined_outside_project", "s.usr IS NOT NULL AND s.stub = 0"
                                " AND f.id IS NOT NULL"),
    ("stub_declaration", "s.usr IS NOT NULL AND s.stub = 1"),
    ("synthetic_identity", "s.usr IS NOT NULL"),
    ("no_symbol_row", "1"),
]


def measure_resolution(store: Store) -> dict:
    """How much of the graph the index can actually explain.

    Counting edges whose destination has no symbol row measures nothing: the
    extractor mints a stub for any declaration it merely refers to, which is
    what makes `callers(std::vector<int>::resize)` answerable without indexing
    libstdc++, so that count is always zero and the ratio always 100%.

    What does say something is which of those a destination is.  An edge into a
    project-defined symbol can be followed to a body; an edge into a stub names
    a declaration the index never saw the body of, and a question about that
    function stops at its signature.  The split is measured per edge kind,
    because call edges and containment edges have very different answers and
    averaging them would hide that.
    """
    c = store.connection()
    total = c.execute("SELECT COUNT(*) FROM raw_edge").fetchone()[0]

    def counts_where(extra: str = "") -> Dict[str, int]:
        q = ("SELECT CASE"
             + "".join(f" WHEN {cond} THEN '{name}'" for name, cond in BUCKETS)
             + " END AS bucket, COUNT(*) AS n"
             " FROM raw_edge e LEFT JOIN symbol s ON s.usr = e.dst"
             " LEFT JOIN file f ON f.id = s.file_id")
        if extra:
            q += " WHERE " + extra
        q += " GROUP BY bucket"
        return {row[0]: row[1] for row in c.execute(q)}

    by_kind = {row[0]: row[1] for row in c.execute(
        "SELECT kind, COUNT(*) FROM raw_edge GROUP BY kind ORDER BY 2 DESC")}
    kinds = {row[0]: row[1] for row in c.execute(
        "SELECT kind, COUNT(*) FROM symbol GROUP BY kind ORDER BY 2 DESC")}
    calls = counts_where("e.kind IN ('calls', 'calls_indirect')")
    resolved = calls.get("defined_in_project", 0)
    call_total = sum(calls.values())
    return {
        "edges": total,
        "edges_by_destination": counts_where(),
        "calls_by_destination": calls,
        "call_edges": call_total,
        "call_edges_into_project_code": resolved,
        # The one number worth quoting for "how much did semantic analysis
        # actually resolve": of the calls written in this project, the share
        # whose target body the index holds.
        "call_resolution_rate": round(resolved / call_total, 4) if call_total else None,
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
        result["queries"] = measure_queries(query, "sample")
        result["worst_case"] = measure_queries(query, "hub")
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
        f"   calls resolve into project code: "
        f"{r['call_edges_into_project_code']}/{r['call_edges']} "
        f"({r['call_resolution_rate']:.1%})",
    ]
    for name, n in sorted(r["calls_by_destination"].items(), key=lambda kv: -kv[1]):
        lines.append(f"     {name:<24} {n:>8}  {n / r['call_edges']:6.1%}")
    for name, n in sorted(r["edges_by_destination"].items(), key=lambda kv: -kv[1]):
        lines.append(f"   all edges {name:<24} {n:>8}  {n / r['edges']:6.1%}")
    if "incremental" in result:
        n = result["incremental"]
        lines.append(f"   one file changed: {n['seconds']}s vs {i['seconds']}s cold "
                     f"({n['speedup']}x), {n['re_indexed']} re-indexed / "
                     f"{n['unchanged']} unchanged")
    lines.append(f"   merge {result['merge_seconds']}s")

    def query_lines(queries: dict, indent: str) -> List[str]:
        out = []
        for name, q in queries.items():
            if "error" in q:
                out.append(f"{indent}{name:<22} n/a: {q['error'][:60]}")
            elif q.get("samples"):
                out.append(f"{indent}{name:<22} {q['median_ms']:>7.2f} ms median, "
                           f"{q['p95_ms']:>7.2f} ms p95, {q['max_ms']:>7.2f} ms max, "
                           f"{q['median_payload_bytes']:>6} B payload")
            else:  # a fixed query, timed rather than distributed
                out.append(f"{indent}{name:<22} {q['median_ms']:>7.2f} ms median, "
                           f"{q['p95_ms']:>7.2f} ms p95, "
                           f"{q['payload_bytes']:>6} B payload")
        return out

    n = result["queries"][next(iter(result["queries"]))].get("samples", 0)
    lines.append(f"   queries over {n} sampled symbols")
    lines += query_lines(result["queries"], "     ")
    subject = result["worst_case"]["subject"]
    lines.append(f"   same queries against the most-called symbol: {subject[:60]}")
    lines += query_lines(result["worst_case"]["queried"], "     ")
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
