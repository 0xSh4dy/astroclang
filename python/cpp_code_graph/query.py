"""Queries over the semantic index.

Every answer here is shaped for an agent that is paying for tokens and does not
want a source file back.  A symbol is reported as a name, a signature, a
``file:line`` and the id needed to ask the next question; the source itself is
only ever returned by an explicit request for a region.

The other rule this module follows is that it will not claim more than the
index knows.  A call that goes through a function pointer is reported as
indirect.  A dependency that might exist because a method is virtual is
reported as possible, not as fact, and says why.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .store import Store

# Edge kinds that mean "this really does depend on that".
CALL_EDGES = ("calls",)
REFERENCE_EDGES = ("references", "calls_indirect")
DEPENDENCY_EDGES = ("calls", "references", "calls_indirect", "inherits",
                    "overrides", "field_type", "param_type", "returns",
                    "var_type", "aliases", "specializes", "instantiates")

DEFAULT_CALLER_LIMIT = 50
DEFAULT_IMPACT_LIMIT = 60

# How many call sites to name before summarising the rest.  A function called
# from fifty places does not need fifty lines of answer to say so.
MAX_CALL_SITES = 5

# How many are remembered at all.  The count stays exact; the list is only ever
# read as a sample, so holding every site of a very hot function would spend
# memory on something no answer quotes.
MAX_TRACKED_SITES = 200


@dataclass
class SymbolRef:
    usr: str
    qualified: str
    kind: str
    signature: str = ""
    location: str = ""
    file: str = ""
    # Where the body is, when that is not where the declaration is.  A caller
    # narrowing a name by file may mean either one.
    def_file: str = ""
    line: Optional[int] = None
    in_project: bool = True
    stub: bool = False


class Query:
    def __init__(self, store: Store):
        self.store = store
        self.conn = store.connection()
        self.root = store.project_root
        # A file's displayed path, computed once.  `rel` resolves the path
        # against the root, which is a filesystem call, and one answer can
        # carry a thousand symbols: without this, listing the callers of a
        # popular function costs a thousand `realpath`s to print the same
        # dozen file names.
        self._rel_cache: Dict[int, str] = {}

    # -- naming --------------------------------------------------------------

    def rel(self, path: Optional[str]) -> str:
        """A path as a reader wants it: relative to the project root."""
        if not path:
            return ""
        if self.root is not None:
            try:
                return str(Path(path).resolve().relative_to(self.root))
            except (ValueError, OSError):
                return path
        return path

    def rel_of(self, file_id: Optional[int]) -> str:
        """A file's path relative to the root, worked out once."""
        if file_id is None:
            return ""
        cached = self._rel_cache.get(file_id)
        if cached is None:
            cached = self.rel(self.store.file_path(file_id))
            self._rel_cache[file_id] = cached
        return cached

    def loc(self, file_id: Optional[int], line: Optional[int]) -> str:
        if file_id is None:
            return ""
        name = self.rel_of(file_id)
        return f"{name}:{line}" if line else name

    # -- symbol shape --------------------------------------------------------

    def _ref_from_row(self, row, with_usr: bool = False) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "symbol": row["qualified"] or row["name"] or row["usr"],
        }
        # A USR is the only unambiguous handle on an overload, but it is long
        # (`c:@N@app@S@Buffer@F@resize#I#` is about ten tokens).  Lists leave it
        # out: every entry already carries `file:line`, which resolves exactly
        # and costs three tokens.
        if with_usr:
            out["usr"] = row["usr"]
        if row["signature"]:
            out["signature"] = row["signature"]
        out["kind"] = row["kind"]
        loc = self.loc(row["file_id"], row["line"])
        if loc:
            out["location"] = loc
        def_file = row["def_file_id"]
        if def_file is not None and def_file != row["file_id"]:
            out["defined_at"] = self.loc(def_file, row["def_line"])
        if not self._is_project_file(row["file_id"]):
            out["external"] = True
        return out

    def _is_project_file(self, file_id: Optional[int]) -> bool:
        return self.store.file_is_in_project(file_id)

    # -- resolution ----------------------------------------------------------

    def resolve(self, reference: str,
                kinds: Optional[Sequence[str]] = None) -> List[SymbolRef]:
        """Find the symbols a caller could have meant.

        Returns every candidate rather than picking one.  `resize` names three
        different methods in a small project and a dozen in a large one, and
        silently choosing the first would hand back a confidently wrong answer
        to "who calls resize" - the failure mode this tool exists to remove.
        """
        if not reference:
            return []

        params: List[Any] = []
        where_kind = ""
        if kinds:
            where_kind = " AND s.kind IN (%s)" % ",".join("?" * len(kinds))
            params.extend(kinds)

        rows: List[Any] = []

        # 1. Exact USR.
        rows = list(self.conn.execute(
            "SELECT s.* FROM symbol s WHERE s.usr = ?" + where_kind,
            [reference, *params],
        ))
        if rows:
            return [self._ref(r) for r in rows]

        # 2. `file.cpp:142`, which is how every list in this module reports a
        #    symbol.  Accepting it back means an agent can follow a result to
        #    the next question without ever handling a USR.
        at = _split_location(reference)
        if at is not None:
            path, line = at
            file_id = self._file_id(path)
            if file_id is not None:
                rows = list(self.conn.execute(
                    "SELECT s.* FROM symbol s WHERE s.file_id = ? AND s.line = ?"
                    + where_kind, [file_id, line, *params]))
                if rows:
                    return [self._ref(r) for r in rows]

        # 3. Exact qualified name, optionally with a signature.
        name, sig = _split_signature(reference)
        q = "SELECT s.* FROM symbol s WHERE (s.qualified = ? OR s.name = ?)"
        args: List[Any] = [name, name]
        if sig:
            q += " AND s.signature LIKE ?"
            args.append(f"%{sig}%")
        rows = list(self.conn.execute(q + where_kind, args + params))
        if rows:
            return self._ranked(rows)

        # 4. Qualified suffix, so `Buffer::resize` finds `app::Buffer::resize`.
        rows = list(self.conn.execute(
            "SELECT s.* FROM symbol s WHERE s.qualified LIKE ?" + where_kind,
            [f"%::{name}", *params],
        ))
        if rows:
            return self._ranked(rows)

        # 5. Last resort: a substring, which is what a vague question looks
        #    like.  Cheap on the index and clearly marked by returning several.
        rows = list(self.conn.execute(
            "SELECT s.* FROM symbol s WHERE s.name LIKE ?" + where_kind,
            [f"%{name}%", *params],
        ))
        return self._ranked(rows)

    def _ranked(self, rows) -> List[SymbolRef]:
        """Project symbols first, then the closest thing to an exact match."""
        def key(r):
            return (
                0 if self._is_project_file(r["file_id"]) else 1,
                0 if not r["stub"] else 1,
                len(r["qualified"] or r["name"] or ""),
                r["qualified"] or "",
            )
        return [self._ref(r) for r in sorted(rows, key=key)]

    def _ref(self, row) -> SymbolRef:
        return SymbolRef(
            usr=row["usr"],
            qualified=row["qualified"] or row["name"] or row["usr"],
            kind=row["kind"],
            signature=row["signature"] or "",
            location=self.loc(row["file_id"], row["line"]),
            file=self.rel_of(row["file_id"]),
            def_file=self.rel_of(row["def_file_id"]),
            line=row["line"],
            in_project=self._is_project_file(row["file_id"]),
            stub=bool(row["stub"]),
        )

    def _row(self, usr: str):
        return self.conn.execute(
            "SELECT * FROM symbol WHERE usr = ?", (usr,)
        ).fetchone()

    def one(self, reference: str,
            kinds: Optional[Sequence[str]] = None) -> Tuple[Optional[SymbolRef],
                                                            List[SymbolRef]]:
        """Resolve to exactly one symbol, or report the ambiguity."""
        candidates = self.resolve(reference, kinds)
        if not candidates:
            return None, []
        if len(candidates) == 1:
            return candidates[0], candidates
        # An exact qualified or USR hit is not an ambiguity even when the
        # substring pass would also have matched other things.
        exact = [c for c in candidates if c.usr == reference]
        if len(exact) == 1:
            return exact[0], candidates
        return None, candidates[:20]

    # -- symbol queries ------------------------------------------------------

    def symbol(self, usr: str, detail: bool = True) -> Optional[Dict[str, Any]]:
        row = self._row(usr)
        if row is None:
            return None
        out = self._ref_from_row(row)
        out["usr"] = row["usr"]
        if detail and row["type_text"]:
            out["type"] = row["type_text"]
        if detail:
            flags = _flags(row["flags"])
            interesting = _interesting_flags(flags)
            if interesting:
                out["properties"] = interesting
            out["used_in_translation_units"] = row["tu_count"]
            if row["parent_usr"]:
                parent = self._row(row["parent_usr"])
                if parent:
                    out["member_of"] = (parent["qualified"] or parent["name"])
        return out

    def symbols_in_file(self, path: str, include_system: bool = False,
                        kind: Optional[str] = None,
                        limit: int = 500) -> List[Dict[str, Any]]:
        """What the file holds - declared or defined there.

        A symbol appears in two places when its declaration and its definition
        are apart, and a source file that holds only definitions would look
        empty if only the declaration were consulted.  Each entry carries both
        locations, so a reader can tell which one is in this file.
        """
        file_id = self._file_id(path)
        if file_id is None:
            return []
        rows = self.conn.execute(
            f"WITH spans AS ({self._SPANS}) SELECT * FROM spans WHERE"
            " (file_id = :fid OR def_file_id = :fid)"
            " AND (:kind IS NULL OR kind = :kind)"
            f" ORDER BY {self._SPAN_START}, col LIMIT :limit",
            {"fid": file_id, "kind": kind, "limit": limit},
        )
        return [self._ref_from_row(r) for r in rows
                if include_system or self._is_project_file(r["file_id"])]

    def ancestors(self, usr: str, limit: int = 8) -> List[Dict[str, Any]]:
        """What lexically encloses this symbol, innermost first.

        Read from the parent chain rather than from source ranges: the
        enclosing namespace of a function defined in a `.cpp` spans a body in
        a different file, so a range test would miss it, while the parent
        link records exactly what the compiler considered the scope.
        """
        out: List[Dict[str, Any]] = []
        seen = {usr}
        row = self._row(usr)
        while row is not None and row["parent_usr"] and len(out) < limit:
            if row["parent_usr"] in seen:
                break
            seen.add(row["parent_usr"])
            row = self._row(row["parent_usr"])
            if row is None:
                break
            out.append(self._ref_from_row(row))
        return out

    def members(self, usr: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM symbol WHERE parent_usr = ? ORDER BY kind, line",
            (usr,),
        )
        return [self._ref_from_row(r) for r in rows]

    def _search_filter(self, text: str, kind: Optional[str],
                       include_system: bool) -> Tuple[str, List[Any]]:
        """The predicate a search and its count are both built from.

        Shared so the two cannot disagree about what matched.  A total that
        counted symbols the list would not show is worse than no total: it
        reads as an answer to "how many are there" and is not one.
        """
        where = "(s.name LIKE ? OR s.qualified LIKE ?)"
        args: List[Any] = [f"%{text}%", f"%{text}%"]
        if kind:
            where += " AND s.kind = ?"
            args.append(kind)
        if not include_system:
            where += (" AND s.file_id IN (SELECT id FROM file WHERE"
                      " in_project = 1)")
        return where, args

    def search(self, text: str, kind: Optional[str] = None,
               limit: int = 25, include_system: bool = False
               ) -> List[Dict[str, Any]]:
        where, args = self._search_filter(text, kind, include_system)
        # Prefer the shortest qualified name: a search for `Buffer` should
        # surface app::Buffer before app::Buffer::count_::something.
        rows = self.conn.execute(
            f"SELECT s.* FROM symbol s WHERE {where}"
            " ORDER BY LENGTH(COALESCE(s.qualified, s.name)), s.qualified"
            " LIMIT ?", [*args, limit])
        return [self._ref_from_row(r) for r in rows]

    def count_search(self, text: str, kind: Optional[str] = None,
                     include_system: bool = False) -> int:
        """How many symbols match, without listing them.

        Asked for only when a page came back full: the LIKE cannot use an
        index, so this costs what the search it is counting cost.
        """
        where, args = self._search_filter(text, kind, include_system)
        return self.conn.execute(
            f"SELECT COUNT(*) FROM symbol s WHERE {where}",
            args).fetchone()[0]

    # -- edges ---------------------------------------------------------------

    def _edges(self, usr: str, kinds: Sequence[str], direction: str,
               limit: int, include_system: bool = False,
               with_usr: bool = False) -> List[Dict[str, Any]]:
        col, other = ("dst", "src") if direction == "in" else ("src", "dst")
        marks = ",".join("?" * len(kinds))

        # Which symbols, first.  The limit is a limit on the answer - how many
        # callers to name - so it applies to the symbols and not to the rows of
        # evidence behind them.  A call written in a header is reported by every
        # translation unit that includes the header, so a limit on rows would
        # spend the whole budget on one caller and hide the rest.
        find = (f"SELECT DISTINCT e.{other} AS u FROM raw_edge e"
                f" JOIN symbol s ON s.usr = e.{other}"
                f" WHERE e.{col} = ? AND e.kind IN ({marks})")
        params: List[Any] = [usr, *kinds]
        if not include_system:
            find += " AND s.file_id IN (SELECT id FROM file WHERE in_project = 1)"
        find += " ORDER BY s.qualified LIMIT ?"
        params.append(limit)
        targets = [r["u"] for r in self.conn.execute(find, params)]
        if not targets:
            return []

        # Then the evidence: one row per call site rather than one per
        # translation unit.  Two translation units reporting the same file and
        # line are reporting one call, and saying so twice would both double the
        # count and read as a second call that was never made.
        #
        # The edge columns are aliased because `s.*` also has `kind`, `file_id`,
        # `line` and `flags`; without the prefix the edge's values win the name
        # lookup and a caller comes back labelled with the edge kind instead of
        # its own.
        rows = self.conn.execute(
            f"SELECT e.{other} AS other_usr, e.file_id AS efile, e.line AS eline,"
            f" e.kind AS ekind, e.flags AS eflags, MAX(e.weight) AS eweight, s.*"
            f" FROM raw_edge e JOIN symbol s ON s.usr = e.{other}"
            f" WHERE e.{col} = ? AND e.kind IN ({marks})"
            f" AND e.{other} IN ({','.join('?' * len(targets))})"
            f" GROUP BY e.{other}, e.file_id, e.line, e.kind, e.flags"
            f" ORDER BY s.qualified, e.line",
            [usr, *kinds, *targets],
        )

        folded: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            key = r["other_usr"]
            entry = folded.get(key)
            if entry is None:
                # The join is on the merged symbol table, so every row for one
                # target describes the symbol identically.
                entry = self._ref_from_row(r, with_usr=with_usr)
                entry["_weight"] = 0
                entry["_sites"] = []
                entry["_seen"] = set()
                entry["_kinds"] = set()
                folded[key] = entry
            entry["_weight"] += r["eweight"] or 1
            entry["_kinds"].add(r["ekind"])
            eflags = _flags(r["eflags"])
            if eflags.get("virt"):
                entry["dispatch"] = "virtual"
            if eflags.get("pure"):
                entry["dispatch"] = "pure virtual"
            site = self.loc(r["efile"], r["eline"])
            # Counted exactly, kept bounded: a function called from ten
            # thousand lines needs a count, not ten thousand entries.
            if site and site not in entry["_seen"]:
                entry["_seen"].add(site)
                if len(entry["_sites"]) < MAX_TRACKED_SITES:
                    entry["_sites"].append(site)

        out = []
        for entry in folded.values():
            sites = entry.pop("_sites")
            count = len(entry.pop("_seen"))
            weight = entry.pop("_weight")
            ekinds = entry.pop("_kinds")
            # The edge carries the call site; the symbol carries the
            # declaration.  Both are useful and are usually different lines,
            # so the call site is reported as its own field - capped, because a
            # function called from fifty places does not need fifty lines of
            # answer to say so.
            if count == 1:
                entry["call_site"] = sites[0]
            elif count:
                entry["call_sites"] = sites[:MAX_CALL_SITES]
                if count > MAX_CALL_SITES:
                    entry["call_site_count"] = count
            # Only when it says something the call sites do not: the same place
            # calling this more than once, which is a loop rather than a
            # separate use.
            if weight > count:
                entry["occurrences"] = weight
            if ekinds != {kinds[0]}:
                entry["via"] = sorted(ekinds)
            out.append(entry)
        return out

    def degree(self, usr: str, kinds: Sequence[str] = CALL_EDGES,
               direction: str = "in", include_system: bool = False) -> int:
        """How many distinct symbols are on the other end of these edges.

        Distinct, because an edge is recorded once per translation unit that
        could see it: counting rows would report the size of the build rather
        than the number of relationships.  This is the count a summary wants -
        it is exact, and it costs one indexed query rather than a list.
        """
        col, other = ("dst", "src") if direction == "in" else ("src", "dst")
        q = (f"SELECT COUNT(DISTINCT e.{other}) FROM raw_edge e"
             f" JOIN symbol s ON s.usr = e.{other}"
             f" WHERE e.{col} = ? AND e.kind IN ({','.join('?' * len(kinds))})")
        args: List[Any] = [usr, *kinds]
        if not include_system:
            q += " AND s.file_id IN (SELECT id FROM file WHERE in_project = 1)"
        return self.conn.execute(q, args).fetchone()[0]

    def callers(self, usr: str, limit: int = DEFAULT_CALLER_LIMIT,
                include_system: bool = False,
                with_usr: bool = False) -> List[Dict[str, Any]]:
        return self._edges(usr, CALL_EDGES, "in", limit, include_system,
                           with_usr)

    def callees(self, usr: str, limit: int = DEFAULT_CALLER_LIMIT,
                include_system: bool = False,
                with_usr: bool = False) -> List[Dict[str, Any]]:
        return self._edges(usr, CALL_EDGES, "out", limit, include_system,
                           with_usr)

    def references_to(self, usr: str, limit: int = DEFAULT_CALLER_LIMIT,
                      include_system: bool = False) -> List[Dict[str, Any]]:
        return self._edges(usr, REFERENCE_EDGES, "in", limit,
                           include_system)

    def outgoing(self, usr: str, kinds: Sequence[str] = DEPENDENCY_EDGES,
                 limit: int = 200) -> List[Dict[str, Any]]:
        return self._edges(usr, kinds, "out", limit)

    # -- inheritance ---------------------------------------------------------

    def inheritance(self, usr: str, transitive: bool = True
                    ) -> Dict[str, Any]:
        out: Dict[str, Any] = {"bases": [], "derived": []}
        # Deduplicated by USR: a base clause in a header is re-reported by
        # every translation unit that includes it.
        def relatives(sql: str, args) -> List[Dict[str, Any]]:
            seen: Dict[str, Dict[str, Any]] = {}
            for r in self.conn.execute(sql, args):
                entry = seen.get(r["usr"])
                if entry is None:
                    entry = self._ref_from_row(r)
                    seen[r["usr"]] = entry
                f = _flags(r["eflags"])
                if f.get("acc"):
                    entry["access"] = _ACCESS_WORDS.get(f["acc"], f["acc"])
                if f.get("virtual"):
                    entry["virtual"] = True
            return list(seen.values())

        out["bases"] = relatives(
            "SELECT s.*, e.flags AS eflags FROM raw_edge e"
            " JOIN symbol s ON s.usr = e.dst"
            " WHERE e.kind = 'inherits' AND e.src = ?", (usr,))
        out["derived"] = relatives(
            "SELECT s.*, e.flags AS eflags FROM raw_edge e"
            " JOIN symbol s ON s.usr = e.src"
            " WHERE e.kind = 'inherits' AND e.dst = ?", (usr,))

        # A template is what a reader means by "what derives from Base" even
        # when the base clause named an instantiation.
        for r in self.conn.execute(
                "SELECT s.* FROM raw_edge e JOIN symbol s ON s.usr = e.src"
                " WHERE e.kind = 'instantiates' AND e.dst = ?", (usr,)):
            entry = self._ref_from_row(r)
            entry["via_template"] = True
            if entry["symbol"] not in [d["symbol"] for d in out["derived"]]:
                out["derived"].append(entry)

        if transitive:
            out["ancestors"] = self._transitive(usr, "inherits", "out")
            # The closure contains the direct bases and derivatives already
            # listed above; repeating them would spend tokens on nothing.
            direct_names = {e["symbol"] for e in out["bases"] + out["derived"]}
            out["descendants"] = [
                e for e in self._transitive(usr, "inherits", "in")
                if e["symbol"] not in direct_names
            ]
            out["ancestors"] = [
                e for e in out["ancestors"] if e["symbol"] not in
                {b["symbol"] for b in out["bases"]}
            ]

        out["overrides"] = self._distinct(
            "SELECT s.* FROM raw_edge e JOIN symbol s ON s.usr = e.src"
            " WHERE e.kind = 'overrides' AND e.dst = ?", (usr,))
        out["overridden"] = self._distinct(
            "SELECT s.* FROM raw_edge e JOIN symbol s ON s.usr = e.dst"
            " WHERE e.kind = 'overrides' AND e.src = ?", (usr,))

        # `overrides` edges point at the method actually overridden, so a
        # three-level hierarchy - Tagged::area over Circle::area over
        # Shape::area - leaves the top level two hops from the bottom.  A call
        # through a Shape* can reach Tagged::area, so a reader asking what
        # implements this interface needs the whole chain, not the first link.
        direct = {o["symbol"] for o in out["overrides"]}
        deeper = [o for o in self._transitive(usr, "overrides", "in")
                  if o["symbol"] not in direct]
        if deeper:
            out["overrides_indirectly"] = deeper
        return {k: v for k, v in out.items() if v}

    def _distinct(self, sql: str, args) -> List[Dict[str, Any]]:
        """One entry per distinct symbol, in the order the query returned them."""
        seen: Dict[str, Dict[str, Any]] = {}
        for row in self.conn.execute(sql, args):
            if row["usr"] not in seen:
                seen[row["usr"]] = self._ref_from_row(row)
        return list(seen.values())

    def _transitive(self, usr: str, kind: str, direction: str,
                    max_depth: int = 8) -> List[Dict[str, Any]]:
        col, other = ("dst", "src") if direction == "in" else ("src", "dst")
        seen: Set[str] = {usr}
        frontier = {usr}
        out: List[Dict[str, Any]] = []
        for _ in range(max_depth):
            if not frontier:
                break
            placeholders = ",".join("?" * len(frontier))
            rows = self.conn.execute(
                f"SELECT DISTINCT e.{other} AS u FROM raw_edge e"
                f" WHERE e.kind = ? AND e.{col} IN ({placeholders})",
                [kind, *frontier],
            )
            nxt = {r["u"] for r in rows} - seen
            if not nxt:
                break
            seen |= nxt
            for u in sorted(nxt):
                row = self._row(u)
                if row:
                    out.append(self._ref_from_row(row))
            frontier = nxt
        return out

    # -- files ---------------------------------------------------------------

    def _file_id(self, path: str) -> Optional[int]:
        candidates = [path]
        if self.root is not None:
            candidates.append(str(Path(self.root) / path))
        for c in candidates:
            row = self.conn.execute(
                "SELECT id FROM file WHERE path = ?", (os.path.normpath(c),)
            ).fetchone()
            if row:
                return row["id"]
        # Fall back to a suffix match, so `allocator.h` works.
        row = self.conn.execute(
            "SELECT id FROM file WHERE path LIKE ? ORDER BY LENGTH(path) LIMIT 1",
            (f"%/{path}",),
        ).fetchone()
        return row["id"] if row else None

    def has_file(self, path: str) -> bool:
        """Whether the index has ever seen this file."""
        return self._file_id(path) is not None

    def file(self, path: str) -> Optional[Dict[str, Any]]:
        file_id = self._file_id(path)
        if file_id is None:
            return None
        row = self.conn.execute(
            "SELECT * FROM file WHERE id = ?", (file_id,)).fetchone()
        symbols = self.symbols_in_file(path, include_system=True)
        included_by = [
            self.rel(self.store.file_path(r["from_file"]))
            for r in self.conn.execute(
                "SELECT DISTINCT from_file FROM raw_include WHERE to_file = ?",
                (file_id,))
        ]
        includes = [
            self.rel(self.store.file_path(r["to_file"]))
            for r in self.conn.execute(
                "SELECT DISTINCT to_file FROM raw_include WHERE from_file = ?",
                (file_id,))
        ]
        return {
            "file": self.rel(row["path"]),
            "is_system": bool(row["is_system"]),
            "in_project": bool(row["in_project"]),
            "symbols": len(symbols),
            "includes": sorted(includes),
            "included_by": sorted(included_by),
            # A header is not a translation unit; what a reader wants is the
            # translation units it is compiled into, which is how a change to
            # the header propagates.
            "translation_units": self.tus_reaching(path),
        }

    def includes(self, path: str, transitive: bool = False) -> Dict[str, Any]:
        """What this file includes, and what includes it.

        Both lists come back whole, however long they are.  How much of one to
        show is the caller's decision, and a closure cut off here could not be
        told from a complete one by anyone downstream - the caller would slice
        it against its own limit and, finding it no longer, report it as
        everything there was.
        """
        file_id = self._file_id(path)
        if file_id is None:
            return {"file": path, "includes": [], "included_by": []}
        if not transitive:
            return {
                "file": self.rel(self.store.file_path(file_id)),
                "includes": self._direct_includes(file_id),
                "included_by": self._direct_includers(file_id),
            }
        return {
            "file": self.rel(self.store.file_path(file_id)),
            "includes_transitively": self._closure(file_id, "includes"),
            "included_by_transitively": self._closure(file_id, "included_by"),
        }

    def _direct_includes(self, file_id: int) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT DISTINCT to_file, line, angled, spelled FROM raw_include"
            " WHERE from_file = ? ORDER BY to_file", (file_id,))
        return [
            {
                "file": self.rel(self.store.file_path(r["to_file"])),
                "line": r["line"],
                "angled": bool(r["angled"]),
                "spelled": r["spelled"],
            }
            for r in rows
        ]

    def _direct_includers(self, file_id: int) -> List[str]:
        return sorted({
            self.rel(self.store.file_path(r["from_file"]))
            for r in self.conn.execute(
                "SELECT DISTINCT from_file FROM raw_include WHERE to_file = ?",
                (file_id,))
        })

    # A frontier is asked about in batches, because it goes into an `IN (...)`
    # and SQLite bounds how many parameters one statement may carry.  A single
    # wide level - a common header reached from most of a project - passes
    # that bound well before an include closure runs out of files.
    _FRONTIER_BATCH = 400

    def _closure(self, file_id: int, direction: str) -> List[str]:
        """Every file reachable from this one, in full.

        Deliberately unbounded.  The result is bounded by the number of files
        in the tree, and a caller that wants less can slice a complete list;
        it cannot recover a truncated one.
        """
        col, other = ("from_file", "to_file") if direction == "includes" else (
            "to_file", "from_file")
        seen: Set[int] = {file_id}
        frontier = [file_id]
        out: List[str] = []
        while frontier:
            nxt: Set[int] = set()
            for start in range(0, len(frontier), self._FRONTIER_BATCH):
                batch = frontier[start:start + self._FRONTIER_BATCH]
                placeholders = ",".join("?" * len(batch))
                rows = self.conn.execute(
                    f"SELECT DISTINCT {other} AS u FROM raw_include"
                    f" WHERE {col} IN ({placeholders})", batch)
                nxt |= {r["u"] for r in rows}
            nxt -= seen
            if not nxt:
                break
            seen |= nxt
            out.extend(sorted(
                self.rel(self.store.file_path(u)) for u in nxt
            ))
            frontier = sorted(nxt)
        return out

    def file_symbols(self, path: str, kind: Optional[str] = None,
                     limit: int = 200) -> List[Dict[str, Any]]:
        return self.symbols_in_file(path, include_system=True, kind=kind,
                                    limit=limit)

    def count_file_symbols(self, path: str, kind: Optional[str] = None) -> int:
        """How many symbols a file holds, without listing them.

        Needed because the list is paged - `limit + 1` says whether the page
        was cut, and this says what the whole list was.  Counted with the same
        predicate as `symbols_in_file`, so the two cannot disagree about which
        symbols belong to the file.
        """
        file_id = self._file_id(path)
        if file_id is None:
            return 0
        return self.conn.execute(
            f"WITH spans AS ({self._SPANS}) SELECT COUNT(*) FROM spans WHERE"
            " (file_id = :fid OR def_file_id = :fid)"
            " AND (:kind IS NULL OR kind = :kind)",
            {"fid": file_id, "kind": kind},
        ).fetchone()[0]

    # -- source --------------------------------------------------------------

    def source_context(self, reference: str, context_lines: int = 20,
                       max_lines: int = 200, path: Optional[str] = None
                       ) -> Dict[str, Any]:
        """The smallest region of source that answers a question about a symbol.

        The alternative - handing back the file - is what makes an agent read
        twenty thousand lines to change three, so the region is the symbol's
        definition where one exists, its declaration otherwise, padded by
        `context_lines` and clamped to the file.

        The span is not guessed by scanning for braces.  The extractor records
        the source range of the definition, so the body's first and last lines
        are already known exactly - padding around a known span is a smaller
        and more honest computation than re-deriving the span from the text.

        Line numbers are part of the returned text because the agent's next
        question is usually about a specific line, and counting lines in a
        quoted block is a good way to be off by one.
        """
        usr, failure = self.resolve_one(reference, path)
        if usr is None:
            return failure or {"error": f"no symbol matching {reference!r}"}
        row = self._row(usr)
        if row is None:  # pragma: no cover - resolve() returns a known USR
            return {"error": f"no symbol matching {reference!r}"}

        out: Dict[str, Any] = {"symbol": row["qualified"] or row["name"] or usr}
        if row["kind"]:
            out["kind"] = row["kind"]
        declared = self.loc(row["file_id"], row["line"])
        if declared:
            out["declared_at"] = declared

        # The definition's span when there is one, the declaration's otherwise.
        # `end_line` belongs to whichever of the two it was read from, which is
        # why the two are taken together or not at all.
        if row["def_file_id"] is not None:
            file_id, first, last = (row["def_file_id"], row["def_line"],
                                    row["end_line"] or row["def_line"])
        else:
            file_id, first, last = (row["file_id"], row["line"],
                                    row["end_line"] or row["line"])
        if file_id is None or not first:
            return out
        if row["end_line"] is None and row["kind"] in _CONTAINER_KINDS:
            last = self._container_end(usr, file_id) or last

        disk = self.store.file_path(file_id)
        lines = _read_lines(disk)
        if lines is None:
            out["error"] = f"cannot read {self.rel(disk)}; re-index if it moved"
            return out
        total = len(lines)
        first, last = max(1, first), max(1, last)
        if last > total:
            # The file shrank under a stale index.  Say so rather than quote
            # whatever now occupies those lines.
            out["error"] = (f"{self.rel(disk)} has {total} lines; the index "
                            f"places this symbol at {first}-{last}. Re-index.")
            return out

        span = last - first + 1
        if span >= max_lines:
            start, end, truncated = first, first + max_lines - 1, True
        else:
            pad = min(context_lines, max_lines - span)
            start = max(1, first - pad)
            end = min(total, last + pad)
            truncated = False

        out["region"] = f"{self.rel(disk)}:{start}-{end}"
        out["at"] = f"{self.rel(disk)}:{first}-{last}"
        out["source"] = "\n".join(
            f"{n}: {lines[n - 1]}" for n in range(start, end + 1)
        )
        if truncated:
            out["truncated"] = True
            out["note"] = (f"the symbol runs to line {last}; only the first "
                           f"{max_lines} lines are shown")
        return out

    def _container_end(self, usr: str, file_id: int, depth: int = 4
                       ) -> Optional[int]:
        """The last line a container occupies, read off what is inside it.

        A namespace carries no end of its own: the extractor records a source
        range for a definition, and `namespace geo` is a declaration however
        many braces follow it.  Quoting that one line answers "where does this
        namespace start" and nothing else.  Its members know where they end,
        and the last of them closes it for every purpose a reader has.
        """
        best: Optional[int] = None
        rows = self.conn.execute(
            "SELECT usr, kind, COALESCE(end_line, line) AS e FROM symbol"
            " WHERE parent_usr = ? AND file_id = ?", (usr, file_id))
        for r in rows:
            if r["e"] is not None:
                best = r["e"] if best is None else max(best, r["e"])
            # A nested namespace has no end either, so its own extent has to
            # come from its members in turn.  A class does not, and its
            # children are inside the range already counted.
            if depth > 0 and r["kind"] == "namespace":
                deeper = self._container_end(r["usr"], file_id, depth - 1)
                if deeper is not None:
                    best = deeper if best is None else max(best, deeper)
        return best

    def _candidates(self, reference: str,
                    path: Optional[str] = None) -> List[SymbolRef]:
        """What `reference` could mean, narrowed by file when it is given.

        An optional `path` settles the common case: `allocate` names a dozen
        methods in a large project, but a caller asking about one of them
        usually knows which file they are looking at.
        """
        candidates = self.resolve(reference)
        if path and len(candidates) > 1:
            want = self.rel(path)
            narrowed = [c for c in candidates
                        if _same_file(c.file, want)
                        or _same_file(c.def_file, want)]
            if narrowed:
                candidates = narrowed
        return candidates

    def resolve_one(self, reference: str, path: Optional[str] = None
                    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """A single USR, or the reason there is not one.

        Returns ``(usr, None)`` or ``(None, explanation)``.  Every entry point
        that takes a symbol name goes through this, so the way an ambiguous
        name is reported - which is to name the alternatives rather than pick
        one - is decided in one place.
        """
        candidates = self._candidates(reference, path)
        if not candidates:
            return None, {"error": f"no symbol matching {reference!r}"}
        # An exact USR hit is not an ambiguity, even when the substring pass
        # would also have matched other things.
        exact = [c for c in candidates if c.usr == reference]
        if len(exact) == 1:
            return exact[0].usr, None
        if len(candidates) == 1:
            return candidates[0].usr, None
        return None, {
            "error": f"{reference!r} names more than one symbol",
            "candidates": self.ambiguity(reference, path),
        }

    def ambiguity(self, reference: str, path: Optional[str] = None,
                  limit: int = 20) -> List[Dict[str, Any]]:
        """Every symbol `reference` could mean, for a caller to report.

        Paged here rather than by the caller because a bare name in a large
        project can match hundreds of symbols, and an entry is built for each
        one kept.  A caller that needs the total asks `count_ambiguity`, which
        counts without building them.
        """
        return [
            {"symbol": c.qualified, "signature": c.signature, "kind": c.kind,
             "location": c.location}
            for c in self._candidates(reference, path)[:limit]
        ]

    def count_ambiguity(self, reference: str,
                        path: Optional[str] = None) -> int:
        """How many symbols a spelling could mean, without listing them.

        Only asked for when a page came back full: it re-runs the candidate
        search, which is the price of not building an entry per candidate just
        to count them.
        """
        return len(self._candidates(reference, path))

    # -- impact --------------------------------------------------------------

    def impact(self, usr: str, depth: int = 3,
               limit: int = DEFAULT_IMPACT_LIMIT) -> Dict[str, Any]:
        """What a change to this symbol could reach.

        Split into three buckets because they are not equally certain, and a
        reviewer deciding whether a change is safe needs to know which is
        which.  Reporting a possible dependency as a certain one would be worse
        than reporting nothing: it would make the tool's answer unusable
        exactly when the answer matters.
        """
        target = self._row(usr)
        if target is None:
            return {}

        inherits = self.inheritance(usr)
        direct: Dict[str, Dict[str, Any]] = {}
        indirect: Dict[str, Dict[str, Any]] = {}
        possible: Dict[str, Dict[str, Any]] = {}

        def add(bucket: Dict[str, Dict[str, Any]], entry: Dict[str, Any],
                reason: str) -> None:
            key = entry["symbol"]
            if key in direct or key in indirect or key in possible:
                return
            entry["reason"] = reason
            bucket[key] = entry

        # Every list below is fetched one longer than it will be shown, so that
        # a bucket the limit stopped can say so.  A bucket holding exactly
        # `limit` entries cannot be told from a complete one by looking at it,
        # and an impact analysis that quietly drops the forty-first caller is
        # the one answer this tool must not give: it is asked precisely when
        # somebody is deciding whether a change is safe.
        #
        # The flag is set from the *lookup* rather than from the bucket, so it
        # errs toward saying "there may be more".  An entry another bucket
        # already claimed is dropped by `add`, which can leave a bucket shorter
        # than the list it came from - reading the length would then call a cut
        # list complete, which is the failure being fixed.
        budget = limit + 1
        cut: Set[str] = set()

        def took(entries: List[Dict[str, Any]], bucket: str) -> None:
            """Note that a bucket's source list held more than was read."""
            if len(entries) > limit:
                cut.add(bucket)

        # -- direct: the index records this dependency ------------------------
        callers = self.callers(usr, limit=budget)
        took(callers, "direct")
        for entry in callers:
            add(direct, entry, "calls this symbol")

        for entry in inherits.get("derived", []):
            add(direct, entry, "derives from this type")

        # Referencing a variable, field or type is a real dependency: change
        # the type and every one of these uses is affected immediately.  A
        # callable is different, and is handled below.
        if not _is_callable(target["kind"]):
            uses = self._edges(usr, ("references",), "in", budget)
            took(uses, "direct")
            for entry in uses:
                add(direct, entry, "uses this symbol")

        # -- possible: it depends on run-time behaviour ----------------------
        for entry in inherits.get("overrides", []):
            add(possible, entry,
                "overrides this method, so a call through the base may reach it")

        for entry in inherits.get("overrides_indirectly", []):
            add(possible, entry,
                "overrides a method that overrides this one, so a call through "
                "the base may reach it")

        if not _is_callable(target["kind"]):
            # Only a type has descendants.  A method's subclasses are reachable
            # through its overrides, which are handled above.
            for entry in inherits.get("descendants", []):
                add(possible, entry,
                    "inherits from this type, so it carries the change through "
                    "the members it did not redefine")

        through_pointer = self._edges(usr, ("calls_indirect",), "in", budget)
        took(through_pointer, "possible")
        for entry in through_pointer:
            add(possible, entry,
                "calls this through a function pointer, so the target is only "
                "known at run time")

        addressed = self._edges(usr, ("references",), "in", budget)
        took(addressed, "possible")
        for entry in addressed:
            if _is_callable(target["kind"]):
                add(possible, entry,
                    "takes the address of this function; the call site is "
                    "recorded against the pointer, not against this symbol")

        instantiations = self._edges(usr, ("specializes", "instantiates"), "in",
                                     budget)
        took(instantiations, "possible")
        for entry in instantiations:
            add(possible, entry, "is an instantiation of this template")

        # -- indirect: callers of callers -------------------------------------
        # `direct` is keyed by display name; the traversal needs the stable id,
        # so it walks a parallel frontier of USRs.
        seen: Set[str] = {usr}
        frontier: List[str] = []
        for entry in list(direct.values()):
            entry_usr = entry.get("usr")
            if entry_usr and entry_usr not in seen:
                seen.add(entry_usr)
                frontier.append(entry_usr)

        for hop in range(2, depth + 1):
            nxt: List[str] = []
            for caller_usr in frontier:
                callers_of_caller = self.callers(caller_usr, limit=budget,
                                                 with_usr=True)
                took(callers_of_caller, "indirect")
                for entry in callers_of_caller:
                    entry_usr = entry.get("usr")
                    if not entry_usr or entry_usr in seen:
                        continue
                    seen.add(entry_usr)
                    entry.pop("usr", None)
                    add(indirect, entry,
                        f"calls a caller of this symbol ({hop} hops)")
                    entry["hops"] = hop
                    nxt.append(entry_usr)
            frontier = nxt
            if not frontier:
                break
            if len(indirect) >= budget:
                # A further hop would only add entries this answer will not
                # show, so the walk stops - and says it stopped.
                cut.add("indirect")
                break

        report = {
            "symbol": target["qualified"] or target["name"],
            "location": self.loc(target["file_id"], target["line"]),
            "direct": list(direct.values())[:limit],
            "indirect": list(indirect.values())[:limit],
            "possible": list(possible.values())[:limit],
            "note": _IMPACT_NOTE,
        }
        if cut:
            # Named per bucket: the buckets are not equally certain, and
            # neither is the confidence in each being complete.
            report["truncated"] = sorted(cut)
        return report

    # -- dependencies --------------------------------------------------------

    def symbol_dependencies(self, usr: str, limit: int = 200
                            ) -> Dict[str, Any]:
        # Distinct, because a type relationship declared in a header is
        # reported by every translation unit that includes the header: without
        # it, a class's dependencies are listed once per compilation.
        rows = self.conn.execute(
            "SELECT DISTINCT e.kind, s.* FROM raw_edge e"
            " JOIN symbol s ON s.usr = e.dst"
            " WHERE e.src = ? AND e.kind IN (%s)"
            " ORDER BY e.kind, s.qualified LIMIT ?"
            % ",".join("?" * len(DEPENDENCY_EDGES)),
            [usr, *DEPENDENCY_EDGES, limit],
        )
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            grouped.setdefault(r["kind"], []).append(self._ref_from_row(r))
        return grouped

    def file_dependencies(self, path: str) -> Dict[str, Any]:
        """What this file needs, transitively, and what needs it."""
        return self.includes(path, transitive=True)

    # -- change detection ----------------------------------------------------

    # A symbol occupies source in one or two places, and the columns only say
    # which is which if you know the rule behind them: the extractor records
    # the *declaration's* file and line, and then, when a separate definition
    # exists, the definition's file, line and **end** - the end line being read
    # off the definition's source range.  So `end_line` belongs to
    # `def_file_id` and never to `file_id`, while a method declared in a header
    # and defined in a source file carries a line number from each.
    #
    # Reading `end_line` against `file_id` therefore misses symbols that a
    # range plainly covers - it fails to match a declaration against its own
    # line, because the end line is a smaller number from another file.  Each
    # span is matched against the file it belongs to, and the declaration span
    # is the declaration's own line whenever there is a definition elsewhere.
    _SPANS = """
        SELECT *,
               (CASE WHEN def_file_id IS NULL THEN COALESCE(end_line, line)
                     ELSE line END) AS decl_end,
               COALESCE(end_line, def_line) AS def_end
        FROM symbol
    """
    _SPAN_MATCH = """
        (file_id = :fid AND line <= :end AND decl_end >= :start)
        OR (def_file_id = :fid AND def_line <= :end AND def_end >= :start)
    """
    # The span the range actually landed in, used for ordering.  A definition
    # in the queried file wins over a declaration there, because when both are
    # present the definition is the one with a body in it.
    _SPAN_START = "CASE WHEN def_file_id = :fid THEN def_line ELSE line END"
    _SPAN_END = ("CASE WHEN def_file_id = :fid THEN def_end ELSE decl_end END")

    def symbols_in_range(self, path: str, start: int, end: int,
                         limit: int = 200) -> List[Dict[str, Any]]:
        file_id = self._file_id(path)
        if file_id is None:
            return []
        rows = self.conn.execute(
            f"WITH spans AS ({self._SPANS}) SELECT * FROM spans"
            f" WHERE ({self._SPAN_MATCH})"
            f" ORDER BY {self._SPAN_START}, {self._SPAN_END} LIMIT :limit",
            {"fid": file_id, "start": start, "end": end, "limit": limit},
        )
        return [self._ref_from_row(r) for r in rows]

    def containers_in_range(self, path: str, start: int, end: int,
                            limit: int = 50) -> List[Dict[str, Any]]:
        """Symbols that *contain* the range, not merely overlap its lines.

        A changed line inside a function body belongs to that function even
        when the edit is to a local variable the index does not track.  The
        innermost container comes first, so the answer to "what is this line
        part of" is the first entry.
        """
        file_id = self._file_id(path)
        if file_id is None:
            return []
        rows = self.conn.execute(
            f"WITH spans AS ({self._SPANS}) SELECT * FROM spans"
            f" WHERE ({self._SPAN_MATCH}) AND kind IN"
            " ('function','method','constructor','destructor','class','struct',"
            "'namespace','conversion_function')"
            f" ORDER BY ({self._SPAN_END} - {self._SPAN_START}) ASC,"
            " kind, name LIMIT :limit",
            {"fid": file_id, "start": start, "end": end, "limit": limit},
        )
        return [self._ref_from_row(r) for r in rows]

    def tus_reaching(self, path: str) -> List[str]:
        """Translation units whose facts would change if this file changed.

        Whole, not paged.  Its caller reports how many there are, and a list
        cut here would turn that report into a number the index cannot stand
        behind - 500 translation units is a plausible header in a large
        project, and the answer would read as complete.
        """
        file_id = self._file_id(path)
        if file_id is None:
            return []
        tus = set()
        row = self.conn.execute(
            "SELECT file_id FROM tu WHERE file_id = ?", (file_id,)).fetchone()
        if row:
            tus.add(row["file_id"])
        # A file reaches a translation unit either directly or through the
        # headers it is included by, so a change to a header invalidates every
        # translation unit that can see it.
        ids = {file_id}
        for rel in self._closure(file_id, "included_by"):
            fid = self._file_id(rel)
            if fid is not None:
                ids.add(fid)
        if ids:
            q = ("SELECT DISTINCT file_id FROM tu WHERE file_id IN (%s)"
                 % ",".join("?" * len(ids)))
            tus |= {r["file_id"] for r in self.conn.execute(q, list(ids))}
        return sorted(self.rel(self.store.file_path(t)) for t in tus)

    # -- diagnostics ---------------------------------------------------------

    # Errors and warnings only.  A note is not something the index lost, and
    # counting it would make the total mean something other than "how much of
    # this translation unit failed to be analysed".
    _DIAG_WHERE = " WHERE d.severity IN ('error','fatal','warning')"

    def _diagnostic_filter(self, path: Optional[str]
                           ) -> Optional[Tuple[str, List[Any]]]:
        """The predicate a diagnostic listing and its count both use.

        Shared so the two cannot disagree about which diagnostics are in
        scope.  A count that included a file the list excludes would be worse
        than no count.  `None` means the named file is not in the index, which
        both callers answer with nothing.
        """
        where = self._DIAG_WHERE
        args: List[Any] = []
        if path:
            file_id = self._file_id(path)
            if file_id is None:
                return None
            where += " AND d.file_id = ?"
            args.append(file_id)
        return where, args

    def diagnostics(self, path: Optional[str] = None, limit: int = 50
                    ) -> List[Dict[str, Any]]:
        filt = self._diagnostic_filter(path)
        if filt is None:
            return []
        where, args = filt
        rows = self.conn.execute(
            "SELECT d.*, f.path AS path FROM raw_diag d"
            " LEFT JOIN file f ON f.id = d.file_id" + where +
            " ORDER BY d.severity DESC, f.path LIMIT ?", [*args, limit])
        return [
            {
                "severity": r["severity"],
                "location": self.loc(r["file_id"], r["line"]),
                "message": r["message"],
            }
            for r in rows
        ]

    def count_diagnostics(self, path: Optional[str] = None) -> int:
        """How many errors and warnings the index holds, without listing them.

        The join is not needed: `file_id` is on the diagnostic row, and `path`
        is only ever selected.
        """
        filt = self._diagnostic_filter(path)
        if filt is None:
            return 0
        where, args = filt
        return self.conn.execute(
            "SELECT COUNT(*) FROM raw_diag d" + where, args).fetchone()[0]


CALLABLE_KINDS = ("function", "method", "constructor", "destructor",
                  "conversion_function", "function_template")

# Kinds whose extent is their body rather than their first line.
_CONTAINER_KINDS = ("namespace", "class", "struct")

_IMPACT_NOTE = (
    "direct: the index records this dependency. "
    "indirect: reached through the callers of the direct entries, so the effect "
    "is real but not at the first hop. "
    "possible: affected only if run-time dispatch reaches here - a virtual "
    "call, a function pointer, or a template instantiation."
)


def _is_callable(kind: Optional[str]) -> bool:
    return kind in CALLABLE_KINDS


def _same_file(a: str, b: str) -> bool:
    """Whether two reported paths name the same file.

    A caller may name a file in full or by any unambiguous suffix - `pool.cpp`
    is how someone refers to the one they are looking at - so the comparison
    accepts a trailing path component match as well as equality.
    """
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def _read_lines(path: Optional[str]) -> Optional[List[str]]:
    """A file's lines, or None when it cannot be read.

    Decoding is lossy on purpose: a source file with a stray byte in a comment
    is still worth quoting, and refusing to show it would be a worse answer
    than showing it with one character replaced.
    """
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    except OSError:
        return None


def _split_location(reference: str) -> Optional[Tuple[str, int]]:
    """Recognise `src/pool.cpp:142` (a path may itself contain a colon on
    Windows, so the numeric part is what decides)."""
    head, sep, tail = reference.rpartition(":")
    if not sep or not head or not tail.isdigit():
        return None
    if "/" not in head and "." not in head:
        return None
    return head, int(tail)


def _split_signature(reference: str) -> Tuple[str, str]:
    """Split `Foo::bar(int)` into its name and its parameter list."""
    open_paren = reference.find("(")
    if open_paren < 0:
        return reference, ""
    if not reference.rstrip().endswith(")"):
        return reference, ""
    return reference[:open_paren], reference[open_paren:]


def _flags(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


# Flags that change what a reader should do with a symbol.  The rest (which
# file kind it came from, whether a definition was seen) are bookkeeping.
_NOTABLE_FLAGS = (
    "virtual", "pure", "override", "abstract", "static", "const", "explicit",
    "deleted", "defaulted", "inline", "constexpr", "deprecated", "variadic",
    "union", "lambda", "scoped", "using", "extern_c", "tmpl", "inst",
)


_ACCESS_WORDS = {"pub": "public", "prot": "protected", "priv": "private",
                 "none": "none"}


def _interesting_flags(flags: Dict[str, Any]) -> Dict[str, Any]:
    """The flags worth showing, as JSON booleans rather than the 1/0 the
    extractor writes, so a reader can tell `true` from `"true"`."""
    out: Dict[str, Any] = {}
    for k in _NOTABLE_FLAGS:
        v = flags.get(k)
        if not v:
            continue
        out[k] = True if v == 1 else v
    return out
