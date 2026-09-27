"""SQLite-backed semantic store.

The store owns three things: the mapping from file paths to ids, the raw facts
of each translation unit, and the merged symbol table derived from them.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .facts import TranslationUnit
from .schema import DDL, REBUILD_SYMBOLS, SCHEMA_VERSION

DEFAULT_INDEX_DIR = ".astroclang"
DB_NAME = "index.db"


def default_db_path(project_root: Path) -> Path:
    return Path(project_root) / DEFAULT_INDEX_DIR / DB_NAME


def read_meta(path: Path, key: str) -> Optional[str]:
    """One value out of an index, without opening it for use.

    Read-only and silent about failure: the callers ask before they know
    whether there is an index there at all, and "no answer" is the same
    answer as "no index" for what they are deciding.
    """
    if not Path(path).is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?",
                           (key,)).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


class Store:
    def __init__(self, path: Path, project_root: Optional[Path] = None,
                 cross_thread: bool = False):
        """`cross_thread` lets the connection be used from a thread that did not
        open it, which the HTTP transport needs: it answers each request on its
        own thread.  SQLite refuses that by default, and the flag is only safe
        because that caller serialises access itself - so it is off here, where
        nothing else would hold the other end of the bargain.

        It is kept on the instance so a transport can check the promise was
        made before it starts answering, rather than discovering it one failed
        request at a time."""
        self.cross_thread = cross_thread
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.project_root = Path(project_root).resolve() if project_root else None
        self._file_ids: Dict[str, int] = {}
        self._canon_cache: Dict[str, str] = {}
        # Both directions of the file table, and its project flag, kept here
        # because a query builds one entry per symbol it looks at: a project
        # with sixty thousand symbols would otherwise spend a few hundred
        # thousand statements reading a table that fits in a few kilobytes.
        self._file_path_cache: Dict[int, str] = {}
        self._in_project_cache: Dict[int, bool] = {}
        self._conn = sqlite3.connect(str(self.path),
                                     check_same_thread=not cross_thread)
        self._conn.row_factory = sqlite3.Row
        # WAL keeps a long re-index from blocking reads, and the durability
        # trade is the right one here: a corrupt index is rebuilt from source,
        # never recovered from.
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        # The server reads this file while another process may be rebuilding
        # it.  Waiting for the writer is the right answer to a locked index:
        # the alternative is failing a query that would have succeeded a
        # moment later.
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(DDL)
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()
        self._load_file_ids()

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _load_file_ids(self) -> None:
        for row in self._conn.execute(
                "SELECT id, path, in_project FROM file"):
            self._file_ids[row["path"]] = row["id"]
            self._file_path_cache[row["id"]] = row["path"]
            self._in_project_cache[row["id"]] = bool(row["in_project"])

    # -- files ---------------------------------------------------------------

    def canonical(self, path: str) -> str:
        """The one spelling of `path` this store will file it under.

        `..` and `.` are removed, symlinks are followed, and a relative path
        is resolved against the project root - because the things that name
        files here do not agree.  A compilation database writes its entries
        relative to the build directory, an include is reported as the path
        the preprocessor opened, and a caller asks for whatever they typed.
        Two spellings of one file must intern to one row: otherwise the
        symbols attach to one id and the translation unit to another, and the
        file appears to contain nothing - which is exactly how this was found.
        """
        cached = self._canon_cache.get(path)
        if cached is not None:
            return cached
        p = path
        if not os.path.isabs(p) and self.project_root is not None:
            p = os.path.join(str(self.project_root), p)
        key = os.path.realpath(p)
        self._canon_cache[path] = key
        return key

    def file_id(self, path: str, is_system: bool = False) -> int:
        """Intern a path, returning its id."""
        key = self.canonical(path)
        existing = self._file_ids.get(key)
        if existing is not None:
            return existing
        in_project = 0
        if self.project_root is not None and not is_system:
            try:
                Path(key).resolve().relative_to(self.project_root)
                in_project = 1
            except (ValueError, OSError):
                in_project = 0
        cur = self._conn.execute(
            "INSERT INTO file(path, is_system, in_project) VALUES (?, ?, ?)",
            (key, 1 if is_system else 0, in_project),
        )
        fid = int(cur.lastrowid)
        self._file_ids[key] = fid
        self._file_path_cache[fid] = key
        self._in_project_cache[fid] = bool(in_project)
        return fid

    def file_path(self, file_id: Optional[int]) -> Optional[str]:
        if file_id is None or file_id < 0:
            return None
        cached = self._file_path_cache.get(file_id)
        if cached is not None:
            return cached
        row = self._conn.execute(
            "SELECT path FROM file WHERE id = ?", (file_id,)
        ).fetchone()
        if row is None:
            return None
        self._file_path_cache[file_id] = row["path"]
        return row["path"]

    def file_is_in_project(self, file_id: Optional[int]) -> bool:
        if file_id is None or file_id < 0:
            return False
        cached = self._in_project_cache.get(file_id)
        if cached is not None:
            return cached
        row = self._conn.execute(
            "SELECT in_project FROM file WHERE id = ?", (file_id,)
        ).fetchone()
        value = bool(row and row["in_project"])
        self._in_project_cache[file_id] = value
        return value

    def file_ids_for(self, paths: Iterable[str]) -> List[int]:
        out = []
        for p in paths:
            fid = self._file_ids.get(self.canonical(p))
            if fid is not None:
                out.append(fid)
        return out

    # -- ingest --------------------------------------------------------------

    def begin(self) -> None:
        self._conn.execute("BEGIN")

    def commit(self) -> None:
        self._conn.commit()

    def ingest(self, tu: TranslationUnit, stamp: Optional[str] = None) -> int:
        """Store one translation unit's facts, replacing any previous run.

        Returns the translation unit id.  File ids in the stream are local to
        the run that produced it, so every one of them is translated here; a
        consumer that skipped this step would silently attach edges to the
        wrong files as soon as two runs disagreed about numbering.
        """
        if not tu.path:
            raise ValueError("fact stream has no 'tu' record; cannot identify it")

        if self._conn.in_transaction:
            self._conn.commit()
        self._conn.execute("BEGIN")

        try:
            tu_file_id = self.file_id(tu.path)

            # Replace, never merge: a re-indexed translation unit must not leave
            # behind facts from the previous run that no longer hold.
            old = self._conn.execute(
                "SELECT id FROM tu WHERE file_id = ?", (tu_file_id,)
            ).fetchone()
            if old:
                self._conn.execute("DELETE FROM tu WHERE id = ?", (old["id"],))

            cur = self._conn.execute(
                "INSERT INTO tu(file_id, config_source, config_detail, degraded,"
                " errors, stamp, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tu_file_id, tu.config_source, tu.config_detail,
                 1 if tu.degraded else 0, tu.errors, stamp, time.time()),
            )
            tu_id = int(cur.lastrowid)

            # Local file id -> global file id for this run.
            remap: Dict[int, int] = {}
            for f in tu.files:
                remap[f.local_id] = self.file_id(f.path, f.is_system)

            def gid(local: Optional[int]) -> Optional[int]:
                if local is None or local < 0:
                    return None
                return remap.get(local)

            self._conn.executemany(
                "INSERT INTO raw_symbol(tu_id, usr, stub, kind, name, qualified,"
                " signature, type_text, file_id, line, col, end_line, end_col,"
                " def_file_id, def_line, def_col, parent_usr, flags)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    (
                        tu_id, s.usr, 1 if s.stub else 0, s.kind, s.name,
                        s.qualified, s.signature, s.type_text,
                        gid(s.file), s.line, s.col, s.end_line, s.end_col,
                        gid(s.def_file), s.def_line, s.def_col,
                        s.parent_usr or None,
                        json.dumps(s.flags, sort_keys=True) if s.flags else None,
                    )
                    for s in tu.symbols
                ),
            )

            self._conn.executemany(
                "INSERT INTO raw_edge(tu_id, kind, src, dst, file_id, line,"
                " weight, flags) VALUES (?,?,?,?,?,?,?,?)",
                (
                    (
                        tu_id, e.kind, e.src, e.dst, gid(e.file), e.line,
                        e.weight,
                        json.dumps(e.flags, sort_keys=True) if e.flags else None,
                    )
                    for e in tu.edges
                ),
            )

            self._conn.executemany(
                "INSERT INTO raw_include(tu_id, from_file, to_file, line,"
                " angled, spelled) VALUES (?,?,?,?,?,?)",
                (
                    (
                        tu_id,
                        remap[i.from_file], remap[i.to_file], i.line,
                        1 if i.angled else 0, i.spelled,
                    )
                    for i in tu.includes
                    if i.from_file in remap and i.to_file in remap
                ),
            )

            self._conn.executemany(
                "INSERT INTO raw_diag(tu_id, severity, file_id, line, col,"
                " message) VALUES (?,?,?,?,?,?)",
                (
                    (tu_id, d.severity, gid(d.file), d.line, d.col, d.message)
                    for d in tu.diags
                ),
            )

            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (f"tu_stats:{tu_id}", json.dumps(tu.stats, sort_keys=True)),
            )
            self._conn.commit()
            return tu_id
        except Exception:
            self._conn.rollback()
            raise

    def drop_tu(self, tu_id: int) -> None:
        if self._conn.in_transaction:
            self._conn.commit()
        self._conn.execute("BEGIN")
        self._conn.execute("DELETE FROM tu WHERE id = ?", (tu_id,))
        self._conn.commit()

    def rebuild_symbols(self) -> None:
        """Recompute the merged symbol table from the raw layer."""
        if self._conn.in_transaction:
            self._conn.commit()
        self._conn.execute("BEGIN")
        self._conn.executescript(REBUILD_SYMBOLS)
        self._conn.commit()

    def analyze(self) -> None:
        """Refresh the query planner's statistics.

        Without them SQLite guesses, and it guesses wrong: asked how many
        distinct callers a symbol has, it scanned every edge in the index
        rather than seeking the three that pointed at it.  Measured over six
        translation units of Clang tooling, that query went from 53 ms to
        0.02 ms.  Statistics are gathered per index rather than per query
        because they describe the whole table, and they go stale as it grows.
        """
        self._conn.execute("ANALYZE")
        self._conn.commit()

    def has_statistics(self) -> bool:
        """Whether the planner has anything better than a guess to work from."""
        try:
            row = self._conn.execute("SELECT COUNT(*) FROM sqlite_stat1").fetchone()
        except sqlite3.OperationalError:  # the table appears with the first ANALYZE
            return False
        return bool(row and row[0])

    # -- introspection -------------------------------------------------------

    def tu_ids(self) -> List[int]:
        return [r["id"] for r in self._conn.execute("SELECT id FROM tu ORDER BY id")]

    def tu_for_file(self, file_id: int) -> Optional[int]:
        row = self._conn.execute(
            "SELECT id FROM tu WHERE file_id = ?", (file_id,)
        ).fetchone()
        return row["id"] if row else None

    def tu_stamp(self, file_id: int) -> Optional[str]:
        row = self._conn.execute(
            "SELECT stamp FROM tu WHERE file_id = ?", (file_id,)
        ).fetchone()
        return row["stamp"] if row else None

    def tu_records(self) -> List[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT tu.*, f.path AS path FROM tu JOIN file f ON f.id = tu.file_id"
                " ORDER BY f.path"
            )
        )

    def get_meta(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
        self._conn.commit()

    def stats(self) -> Dict[str, int]:
        c = self._conn
        return {
            "files": c.execute("SELECT COUNT(*) FROM file").fetchone()[0],
            "project_files": c.execute(
                "SELECT COUNT(*) FROM file WHERE in_project = 1"
            ).fetchone()[0],
            "translation_units": c.execute("SELECT COUNT(*) FROM tu").fetchone()[0],
            "symbols": c.execute("SELECT COUNT(*) FROM symbol").fetchone()[0],
            "symbols_in_project": c.execute(
                "SELECT COUNT(*) FROM symbol s JOIN file f ON f.id = s.file_id"
                " WHERE f.in_project = 1"
            ).fetchone()[0],
            "edges": c.execute("SELECT COUNT(*) FROM raw_edge").fetchone()[0],
            "includes": c.execute("SELECT COUNT(*) FROM raw_include").fetchone()[0],
            "diagnostics": c.execute("SELECT COUNT(*) FROM raw_diag").fetchone()[0],
            "degraded_tus": c.execute(
                "SELECT COUNT(*) FROM tu WHERE degraded = 1"
            ).fetchone()[0],
            "failed_tus": c.execute(
                "SELECT COUNT(*) FROM tu WHERE errors > 0"
            ).fetchone()[0],
        }

    def connection(self) -> sqlite3.Connection:
        return self._conn
