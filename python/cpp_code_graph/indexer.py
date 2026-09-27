"""Running the extractor over a project and storing what it reports.

Failure handling is the substance of this module.  A translation unit can fail
in three distinct ways, and they mean different things:

  * the extractor exits non-zero - it could not be started, or had no usable
    compiler arguments, so nothing is known about that file;
  * the extractor exits zero but emits a truncated stream - it crashed part way
    through, so what it reported is a prefix, not an answer;
  * the extractor completes and reports diagnostics - the facts are real but
    the translation unit did not compile cleanly, so constructs behind the
    error may be missing.

Only the third produces a usable index, and even then the diagnostics are kept
so a consumer can judge how much to trust it.  The first two leave the previous
index entry for that file untouched rather than replacing it with a partial
one.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .discovery import Plan, plan_indexing
from .facts import FactStreamError, TranslationUnit, read_facts_file
from .store import Store


class ExtractorNotFound(RuntimeError):
    pass


def find_extractor(explicit: Optional[str] = None) -> Path:
    """Locate the cg-index binary.

    The environment variable is checked before PATH so that a build tree can
    be used without installing, which is how the tests and the evaluation runs
    drive it.
    """
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        raise ExtractorNotFound(f"no extractor at {explicit}")

    env = os.environ.get("CG_INDEX")
    if env and Path(env).is_file():
        return Path(env)

    found = shutil.which("cg-index")
    if found:
        return Path(found)

    # A build tree next to the package, for a checkout that has been built but
    # not installed.
    here = Path(__file__).resolve()
    for base in (here.parents[2], Path.cwd()):
        for rel in ("build/cpp/cg-index", "build/cg-index", "cpp/cg-index"):
            candidate = base / rel
            if candidate.is_file():
                return candidate

    raise ExtractorNotFound(
        "cg-index not found. Build it (cmake --build build) and set CG_INDEX, "
        "or put it on PATH."
    )


@dataclass
class TuResult:
    path: Path
    status: str  # "indexed" | "failed" | "unchanged"
    detail: str = ""
    symbols: int = 0
    edges: int = 0
    includes: int = 0
    errors: int = 0
    degraded: bool = False
    seconds: float = 0.0


@dataclass
class IndexReport:
    total: int = 0
    indexed: int = 0
    failed: int = 0
    unchanged: int = 0
    degraded: int = 0
    seconds: float = 0.0
    notes: List[str] = field(default_factory=list)
    failures: List[TuResult] = field(default_factory=list)
    results: List[TuResult] = field(default_factory=list)


def _stamp(path: Path, compdb: Optional[Path]) -> str:
    """Identity of the inputs that determine a translation unit's facts.

    Deliberately covers only the file's own bytes and the compilation database.
    A header it includes is *not* part of this: header changes are handled by
    asking which translation units reach the header, which is a question the
    index can answer and this cannot.
    """
    h = hashlib.sha256()
    try:
        h.update(path.read_bytes())
    except OSError:
        h.update(b"<unreadable>")
    h.update(b"\0")
    if compdb is not None:
        try:
            h.update(str(compdb.stat().st_mtime_ns).encode())
        except OSError:
            pass
    return h.hexdigest()


def _run_one(extractor: Path, path: Path, arguments: Sequence[str],
             timeout: float) -> Tuple[Optional[TranslationUnit], str, float]:
    import time

    started = time.monotonic()
    try:
        proc = subprocess.run(
            [str(extractor), *arguments, str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"extractor timed out after {timeout:.0f}s", timeout
    except OSError as exc:
        return None, f"could not run extractor: {exc}", 0.0

    elapsed = time.monotonic() - started

    if proc.returncode != 0 and not proc.stdout.strip():
        detail = (proc.stderr or "").strip().splitlines()
        return None, detail[-1] if detail else f"exit status {proc.returncode}", elapsed

    # A non-zero exit with output on stdout is Clang reporting a bad parse; the
    # facts are still complete for whatever did parse.
    from .facts import read_facts

    try:
        tu = read_facts(iter(proc.stdout.splitlines()), source=str(path))
    except FactStreamError as exc:
        return None, str(exc), elapsed

    return tu, "", elapsed


def index_project(root: Path,
                  store: Store,
                  extractor: Optional[Path] = None,
                  compdb: Optional[Path] = None,
                  jobs: int = 0,
                  only: Optional[Sequence[str]] = None,
                  limit: Optional[int] = None,
                  incremental: bool = True,
                  timeout: float = 300.0,
                  options: Sequence[str] = (),
                  progress: Optional[Callable[[str], None]] = None
                  ) -> IndexReport:
    exe = find_extractor(str(extractor) if extractor else None)
    plan = plan_indexing(Path(root), compdb, only, limit)

    report = IndexReport(total=len(plan.files), notes=list(plan.notes))
    if progress:
        for note in plan.notes:
            progress(note)

    import time

    started = time.monotonic()

    pending: List[Tuple[Path, str]] = []
    for path in plan.files:
        stamp = _stamp(path, plan.database.path)
        if incremental and store.tu_stamp(store.file_id(str(path))) == stamp:
            report.unchanged += 1
            report.results.append(TuResult(path=path, status="unchanged"))
            continue
        pending.append((path, stamp))

    base_args = [*plan.compdb_args, "--project-root", str(plan.root), *options]

    def work(item: Tuple[Path, str]) -> Tuple[Path, str, Optional[TranslationUnit], str, float]:
        path, stamp = item
        tu, detail, elapsed = _run_one(exe, path, base_args, timeout)
        return path, stamp, tu, detail, elapsed

    workers = jobs if jobs > 0 else min(32, (os.cpu_count() or 4))
    workers = max(1, min(workers, len(pending) or 1))

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for path, stamp, tu, detail, elapsed in pool.map(work, pending):
            rel = _relative(path, plan.root)
            if tu is None:
                # The previous entry, if any, stays. A partial result must not
                # displace a complete one from an earlier run.
                result = TuResult(path=path, status="failed", detail=detail,
                                  seconds=elapsed)
                report.failed += 1
                report.failures.append(result)
                report.results.append(result)
                if progress:
                    progress(f"  failed  {rel}: {detail}")
                continue

            try:
                store.ingest(tu, stamp=stamp)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                result = TuResult(path=path, status="failed",
                                  detail=f"could not store facts: {exc}",
                                  seconds=elapsed)
                report.failed += 1
                report.failures.append(result)
                report.results.append(result)
                if progress:
                    progress(f"  failed  {rel}: {exc}")
                continue

            result = TuResult(
                path=path,
                status="indexed",
                symbols=int(tu.stats.get("symbols", 0)),
                edges=int(tu.stats.get("edges", 0)),
                includes=int(tu.stats.get("includes", 0)),
                errors=tu.errors,
                degraded=tu.degraded,
                seconds=elapsed,
            )
            report.indexed += 1
            if tu.degraded:
                report.degraded += 1
            report.results.append(result)
            if progress:
                flag = " (degraded config)" if tu.degraded else ""
                if tu.errors:
                    flag += f" ({tu.errors} errors)"
                progress(f"  indexed {rel}  "
                         f"{result.symbols} symbols, {result.edges} edges{flag}")

    if report.indexed:
        store.rebuild_symbols()

    report.seconds = time.monotonic() - started
    return report


def _relative(path: Path, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return str(path)
