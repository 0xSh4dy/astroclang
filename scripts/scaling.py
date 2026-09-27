#!/usr/bin/env python3
"""How indexing scales with worker count, and where it stops scaling.

`evaluate.py` reports one wall time and one peak RSS for a whole run.  Both are
artefacts of a choice it does not vary: how many translation units are analysed
at once.  This measures that choice directly.

The reason it deserves its own script is that the first number this project
recorded was wrong, and wrong in a way that looked fine.  A serial run followed
by a parallel run reported 3.10x, because the serial run read every file from a
cold page cache and the parallel run found them all in memory.  The fix is not
a better formula but a better procedure: alternate the order and repeat it, so
that neither configuration systematically gets the warm cache.

    scripts/scaling.py --root /path/to/project --jobs 1 2 4 8

Two things are measured per configuration, and they disagree about what the
limit is:

* wall time, which keeps improving with more workers until the cores run out
* peak extractor memory *for the run as a whole* - the sum across concurrent
  processes, sampled while they run

`evaluate.py` reports `getrusage(RUSAGE_CHILDREN).ru_maxrss`, which on Linux is
the largest single child rather than the sum.  One unity build needing 1.8 GB
and eight of them needing 14.4 GB are the same number under that measure and
very different facts about the machine.  This is the measurement that tells
them apart, and it is the one that decides how many workers are safe.

The script refuses to start a configuration it does not believe fits in
available memory.  That refusal is the point rather than a nuisance: on the
machine this was developed on, the binding constraint is RAM, not cores, and a
sweep that ignores it does not measure the tool - it measures the swap file,
having first made the rest of the desktop unusable.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from cpp_code_graph import indexer  # noqa: E402
from cpp_code_graph.store import Store, default_db_path  # noqa: E402

SAMPLE_INTERVAL = 0.25       # seconds between memory samples
MB = 1024 * 1024

# Refuse to run a configuration whose estimated peak exceeds this share of what
# the kernel says is available.  Not 100%: the estimate is a guess, other
# processes grow while this runs, and overshooting into swap costs more than
# the measurement is worth.
MEMORY_HEADROOM = 0.75

# What one translation unit's front end may need.  The measured worst case on
# the evaluation subject is 1.84 GB for a unity build; most files need far
# less.  Assuming the worst for every worker over-reserves on ordinary trees,
# which is the safe direction - the guard exists to prevent swap, and a guard
# that lets swap through is worse than no guard.
PER_WORKER_PEAK_BYTES = 1900 * MB


def mem_available() -> int:
    """The kernel's own estimate of what can be allocated without swapping."""
    with open("/proc/meminfo") as handle:
        for line in handle:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    raise RuntimeError("/proc/meminfo has no MemAvailable")


def extractor_rss() -> int:
    """Resident bytes held by every live extractor process, summed.

    This is the number `ru_maxrss` cannot give: the concurrent total.  Read
    from /proc rather than from the indexer, because the point is to observe
    what the machine is actually carrying rather than what the code believes
    it started.
    """
    total = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().strip()
            if comm != "cg-index":
                continue
            for line in (entry / "status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1]) * 1024
                    break
        except (OSError, ValueError):
            # A process that exited between listing /proc and reading it is
            # the normal case under load, not an error.
            continue
    return total


class Sampler:
    """Watches memory while a run proceeds, from a thread of its own.

    The indexer is synchronous and single-threaded from the caller's point of
    view; sampling has to happen alongside it or the peak is missed entirely.
    """

    def __init__(self) -> None:
        self.peak_extractor = 0
        self.lowest_available = mem_available()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(SAMPLE_INTERVAL):
            self.peak_extractor = max(self.peak_extractor, extractor_rss())
            self.lowest_available = min(self.lowest_available, mem_available())

    def __enter__(self) -> "Sampler":
        self.lowest_available = mem_available()
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        # One last reading: a run that ends between two samples would otherwise
        # report the peak of the interval before it.
        self.peak_extractor = max(self.peak_extractor, extractor_rss())
        self.lowest_available = min(self.lowest_available, mem_available())


def one_run(root: Path, compdb: Optional[Path], jobs: int) -> Dict[str, object]:
    """Index the tree from scratch at one worker count, and observe it."""
    db = default_db_path(root)
    if db.parent.exists():
        shutil.rmtree(db.parent)

    store = Store(db, project_root=root)
    try:
        with Sampler() as sampler:
            started = time.monotonic()
            report = indexer.index_project(root, store, compdb=compdb, jobs=jobs)
            seconds = time.monotonic() - started
        return {
            "jobs": jobs,
            "seconds": round(seconds, 2),
            "translation_units": report.total,
            "failed": report.failed,
            "peak_extractor_mb": round(sampler.peak_extractor / MB, 1),
            "lowest_available_mb": round(sampler.lowest_available / MB, 1),
        }
    finally:
        store.close()


def safe_to_run(jobs: int, per_worker: int = PER_WORKER_PEAK_BYTES) -> bool:
    """Whether a configuration of this width fits in what is currently free."""
    free = mem_available()
    needed = jobs * per_worker
    return needed <= free * MEMORY_HEADROOM


def sweep(root: Path, compdb: Optional[Path], widths: List[int],
          repeat: int,
          per_worker: int = PER_WORKER_PEAK_BYTES) -> Dict[str, object]:
    root = root.resolve()
    runs: List[Dict[str, object]] = []
    refused: List[Dict[str, object]] = []

    for pass_number in range(repeat):
        # Alternating the order is the whole correction.  Ascending every time
        # would hand the small configurations the cold cache and the large ones
        # the warm cache, reproducing the artefact this script exists to avoid.
        order = widths if pass_number % 2 == 0 else list(reversed(widths))
        for jobs in order:
            if not safe_to_run(jobs, per_worker):
                refused.append({
                    "jobs": jobs,
                    "pass": pass_number + 1,
                    "available_mb": round(mem_available() / MB, 1),
                    "needed_mb": round(jobs * per_worker / MB, 1),
                })
                print(f"  refused jobs={jobs}: would need ~"
                      f"{jobs * per_worker / MB:.0f} MB at "
                      f"{per_worker / MB:.0f} MB/worker, "
                      f"{mem_available() / MB:.0f} MB available", flush=True)
                continue
            print(f"  pass {pass_number + 1}: jobs={jobs} ...", flush=True)
            result = one_run(root, compdb, jobs)
            result["pass"] = pass_number + 1
            runs.append(result)
            print(f"    {result['seconds']}s, peak extractor "
                  f"{result['peak_extractor_mb']} MB, "
                  f"lowest available {result['lowest_available_mb']} MB",
                  flush=True)

    return {"root": str(root), "runs": runs, "refused": refused}


def table(result: Dict[str, object]) -> str:
    """A markdown table, best time per worker count across the passes."""
    runs = result["runs"]
    if not runs:
        return "_No configuration was run._"

    by_width: Dict[int, List[Dict[str, object]]] = {}
    for run in runs:
        by_width.setdefault(run["jobs"], []).append(run)

    baseline = min((min(r["seconds"] for r in rs), w)
                   for w, rs in by_width.items())[0]

    lines = [
        "| Workers | Wall time | Speedup | Peak extractor RSS | Lowest free RAM | Passes |",
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for width in sorted(by_width):
        rs = by_width[width]
        best = min(r["seconds"] for r in rs)
        peak = max(r["peak_extractor_mb"] for r in rs)
        low = min(r["lowest_available_mb"] for r in rs)
        lines.append(f"| {width} | {best:.1f} s | {baseline / best:.2f}× | "
                     f"{peak:.0f} MB | {low:.0f} MB | {len(rs)} |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--compdb", type=Path, default=None)
    parser.add_argument("--jobs", type=int, nargs="+", default=[1, 2, 4, 8],
                        help="worker counts to measure (default: 1 2 4 8)")
    parser.add_argument("--repeat", type=int, default=2,
                        help="passes over the widths; the order alternates "
                             "(default: 2)")
    parser.add_argument("--per-worker-mb", type=int, default=1900,
                        help="memory to assume one worker may need when "
                             "deciding whether a width is safe; the default is "
                             "the measured unity-build worst case, which is "
                             "conservative for trees without one")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = sweep(args.root, args.compdb, sorted(args.jobs), args.repeat,
                   args.per_worker_mb * MB)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print()
        print(table(result))
        if result["refused"]:
            print(f"\n{len(result['refused'])} configuration(s) refused for "
                  f"want of memory; see the JSON for what was free.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
