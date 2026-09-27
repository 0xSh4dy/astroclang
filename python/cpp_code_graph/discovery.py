"""Finding the compilation database and the translation units to index.

The compilation database is what makes semantic analysis of C++ possible at
all.  Without it there is no include path, no language standard, no feature
macro: a header is simply not found, and every declaration behind it is
missing from the graph.  So the search for one is thorough, and the failure to
find one is reported rather than papered over.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

# Extensions the extractor can be handed.  `.h` is deliberately absent: a
# header is indexed through the translation units that include it, and feeding
# one to Clang as a source file invents a translation unit that does not exist
# in the build.
SOURCE_EXTENSIONS = (".c", ".cc", ".cpp", ".cxx", ".c++", ".m", ".mm")

# Headers are indexed through the translation units that include them, never
# fed to the compiler directly.  Kept apart from SOURCE_EXTENSIONS for that
# reason, but a file with one of these suffixes is still something the index is
# expected to know about.
HEADER_EXTENSIONS = (".h", ".hh", ".hpp", ".hxx", ".h++", ".inc", ".inl",
                     ".ipp", ".tcc", ".tpp")

# Directories never worth walking: version control metadata and caches, which
# hold no source of the project's own.
#
# Build directories are deliberately *not* here.  A directory called `build`
# may hold hand-written source, and in the fallback path there is no
# compilation database to say which files the build actually compiles, so
# skipping by name would drop real files to hide a problem the caller has
# already been told about.  Generated sources found this way produce duplicate
# symbols, which the merge collapses, rather than missing ones.
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".cpp-code-graph", "node_modules", "__pycache__",
    ".cache", ".ccache", "CMakeFiles",
}

# Places a build system conventionally leaves its compilation database.
COMPDB_SEARCH_DIRS = (
    "", "build", "Build", "cmake-build-debug", "cmake-build-release",
    "out", "out/build", "build/debug", "build/release",
)


@dataclass
class CompilationDatabase:
    """Where the compiler arguments for this project come from."""

    path: Optional[Path] = None
    directory: Optional[Path] = None
    files: List[Path] = field(default_factory=list)
    source: str = "none"
    detail: str = ""

    @property
    def found(self) -> bool:
        return self.path is not None


def find_compilation_database(root: Path,
                              explicit: Optional[Path] = None
                              ) -> CompilationDatabase:
    """Locate a compile_commands.json for `root`, newest conventions first."""
    root = Path(root).resolve()

    if explicit is not None:
        path = Path(explicit).resolve()
        if path.is_dir():
            path = path / "compile_commands.json"
        if path.is_file():
            db = _load(path)
            db.source = "explicit"
            return db
        return CompilationDatabase(
            detail=f"no compilation database at {explicit}"
        )

    for rel in COMPDB_SEARCH_DIRS:
        candidate = (root / rel / "compile_commands.json") if rel else (
            root / "compile_commands.json"
        )
        if candidate.is_file():
            db = _load(candidate)
            db.source = "discovered"
            return db

    return CompilationDatabase(
        detail=f"no compile_commands.json found in {root} or its build directories"
    )


def _load(path: Path) -> CompilationDatabase:
    db = CompilationDatabase(path=path, directory=path.parent)
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError) as exc:
        db.detail = f"{path} could not be read: {exc}"
        db.path = None
        return db

    if not isinstance(raw, list):
        db.detail = f"{path} is not a JSON array of compile commands"
        db.path = None
        return db

    seen = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        directory = entry.get("directory") or str(path.parent)
        for key in ("file", "source"):
            name = entry.get(key)
            if not name:
                continue
            p = Path(name)
            if not p.is_absolute():
                p = Path(directory) / p
            p = Path(os.path.normpath(str(p)))
            if p.suffix.lower() in SOURCE_EXTENSIONS and p not in seen:
                seen.add(p)
                db.files.append(p)
            break

    db.files.sort()
    db.detail = f"{len(db.files)} translation units in {path}"
    return db


def walk_sources(root: Path, extra_skip: Sequence[str] = ()) -> List[Path]:
    """Every source file under `root`, for projects with no database.

    The result is the honest best effort: these files can be parsed, but
    without the project's own compiler arguments the analysis is weaker, and
    the caller is expected to say so rather than present it as equivalent.
    """
    root = Path(root).resolve()
    skip = set(SKIP_DIRS) | set(extra_skip)
    out: List[Path] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in skip and not d.startswith(".")
        )
        for name in sorted(filenames):
            if Path(name).suffix.lower() in SOURCE_EXTENSIONS:
                out.append(Path(dirpath) / name)
    return out


@dataclass
class Plan:
    """What the driver is going to index, and how confident it can be."""

    root: Path
    files: List[Path]
    database: CompilationDatabase
    degraded: bool
    notes: List[str] = field(default_factory=list)

    @property
    def compdb_args(self) -> List[str]:
        """Arguments that tell the extractor where the arguments come from."""
        if self.database.path is not None:
            return ["--compdb", str(self.database.path)]
        return []


def plan_indexing(root: Path,
                  explicit_compdb: Optional[Path] = None,
                  only: Optional[Iterable[str]] = None,
                  limit: Optional[int] = None) -> Plan:
    root = Path(root).resolve()
    db = find_compilation_database(root, explicit_compdb)

    notes: List[str] = []
    if db.found:
        files = list(db.files)
        degraded = False
        notes.append(f"compiler arguments: {db.detail}")
    else:
        files = walk_sources(root)
        degraded = True
        notes.append(
            "no compilation database found; falling back to conventional "
            "include directories. Include paths, language standard and feature "
            "macros are guesses, so headers may be missed and declarations "
            "behind them absent from the graph."
        )
        if db.detail:
            notes.append(db.detail)

    if only:
        wanted = [str(Path(o)) for o in only]
        files = [f for f in files
                 if any(f.match(w) or str(f).endswith(w) for w in wanted)]
        notes.append(f"filtered to {len(files)} translation units")

    files = [f for f in files if f.exists()]
    if limit is not None:
        files = files[:limit]

    return Plan(root=root, files=files, database=db, degraded=degraded,
                notes=notes)
