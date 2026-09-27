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


@dataclass
class SymbolRef:
    usr: str
    qualified: str
    kind: str
    signature: str = ""
    location: str = ""
    file: str = ""
    line: Optional[int] = None
    in_project: bool = True
    stub: bool = False


class Query:
    def __init__(self, store: Store):
        self.store = store
        self.conn = store.connection()
        self.root = store.project_root

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

    def loc(self, file_id: Optional[int], line: Optional[int]) -> str:
        if file_id is None:
            return ""
        path = self.store.file_path(file_id)
        name = self.rel(path)
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
        if file_id is None:
            return False
        row = self.conn.execute(
            "SELECT in_project FROM file WHERE id = ?", (file_id,)
        ).fetchone()
        return bool(row and row["in_project"])

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
            file=self.rel(self.store.file_path(row["file_id"])),
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

    def symbols_in_file(self, path: str,
                        include_system: bool = False) -> List[Dict[str, Any]]:
        file_id = self._file_id(path)
        if file_id is None:
            return []
        rows = self.conn.execute(
            "SELECT * FROM symbol WHERE file_id = ? ORDER BY line, col",
            (file_id,),
        )
        return [self._ref_from_row(r) for r in rows
                if include_system or self._is_project_file(r["file_id"])]

    def members(self, usr: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM symbol WHERE parent_usr = ? ORDER BY kind, line",
            (usr,),
        )
        return [self._ref_from_row(r) for r in rows]

    def search(self, text: str, kind: Optional[str] = None,
               limit: int = 25, include_system: bool = False
               ) -> List[Dict[str, Any]]:
        q = "SELECT s.* FROM symbol s WHERE (s.name LIKE ? OR s.qualified LIKE ?)"
        args: List[Any] = [f"%{text}%", f"%{text}%"]
        if kind:
            q += " AND s.kind = ?"
            args.append(kind)
        if not include_system:
            q += " AND s.file_id IN (SELECT id FROM file WHERE in_project = 1)"
        # Prefer the shortest qualified name: a search for `Buffer` should
        # surface app::Buffer before app::Buffer::count_::something.
        q += " ORDER BY LENGTH(COALESCE(s.qualified, s.name)), s.qualified LIMIT ?"
        args.append(limit)
        return [self._ref_from_row(r) for r in self.conn.execute(q, args)]

    # -- edges ---------------------------------------------------------------

    def _edges(self, usr: str, kinds: Sequence[str], direction: str,
               limit: int, include_system: bool = False,
               with_usr: bool = False) -> List[Dict[str, Any]]:
        col, other = ("dst", "src") if direction == "in" else ("src", "dst")
        # The edge columns are aliased because `s.*` also has `kind`, `file_id`,
        # `line` and `flags`; without the prefix the edge's values win the name
        # lookup and a caller comes back labelled with the edge kind instead of
        # its own.
        q = f"""
            SELECT e.{other} AS other_usr, e.file_id AS efile, e.line AS eline,
                   e.kind AS ekind, e.flags AS eflags, e.weight AS eweight, s.*
            FROM raw_edge e
            JOIN symbol s ON s.usr = e.{other}
            WHERE e.{col} = ? AND e.kind IN ({",".join("?" * len(kinds))})
            ORDER BY e.kind, s.qualified, e.line
            LIMIT ?
        """
        rows = self.conn.execute(q, [usr, *kinds, limit])
        out = []
        for r in rows:
            if not include_system and not self._is_project_file(r["file_id"]):
                continue
            # The edge carries the call site; the symbol carries the
            # declaration.  Both are useful and they are usually different
            # lines, so the call site is reported as its own field.
            ref = self._ref_from_row(r, with_usr=with_usr)
            site = self.loc(r["efile"], r["eline"])
            if site and site != ref.get("location"):
                ref["call_site"] = site
            eflags = _flags(r["eflags"])
            if eflags.get("virt"):
                ref["dispatch"] = "virtual"
            if eflags.get("pure"):
                ref["dispatch"] = "pure virtual"
            if r["eweight"] and r["eweight"] > 1:
                ref["occurrences"] = r["eweight"]
            if r["ekind"] != kinds[0]:
                ref["via"] = r["ekind"]
            out.append(ref)
        return out

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
        return self._edges(usr, ("references", "calls_indirect"), "in", limit,
                           include_system)

    def outgoing(self, usr: str, kinds: Sequence[str] = DEPENDENCY_EDGES,
                 limit: int = 200) -> List[Dict[str, Any]]:
        return self._edges(usr, kinds, "out", limit)

    # -- inheritance ---------------------------------------------------------

    def inheritance(self, usr: str, transitive: bool = True
                    ) -> Dict[str, Any]:
        out: Dict[str, Any] = {"bases": [], "derived": []}
        base_rows = self.conn.execute(
            "SELECT s.*, e.flags AS eflags, e.file_id AS efile, e.line AS eline"
            " FROM raw_edge e JOIN symbol s ON s.usr = e.dst"
            " WHERE e.kind = 'inherits' AND e.src = ?", (usr,))
        for r in base_rows:
            entry = self._ref_from_row(r)
            f = _flags(r["eflags"])
            if f.get("acc"):
                entry["access"] = _ACCESS_WORDS.get(f["acc"], f["acc"])
            if f.get("virtual"):
                entry["virtual"] = True
            out["bases"].append(entry)

        derived_rows = self.conn.execute(
            "SELECT s.*, e.flags AS eflags FROM raw_edge e"
            " JOIN symbol s ON s.usr = e.src"
            " WHERE e.kind = 'inherits' AND e.dst = ?", (usr,))
        for r in derived_rows:
            entry = self._ref_from_row(r)
            f = _flags(r["eflags"])
            if f.get("acc"):
                entry["access"] = _ACCESS_WORDS.get(f["acc"], f["acc"])
            out["derived"].append(entry)

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

        out["overrides"] = [
            self._ref_from_row(r) for r in self.conn.execute(
                "SELECT s.* FROM raw_edge e JOIN symbol s ON s.usr = e.src"
                " WHERE e.kind = 'overrides' AND e.dst = ?", (usr,))
        ]
        out["overridden"] = [
            self._ref_from_row(r) for r in self.conn.execute(
                "SELECT s.* FROM raw_edge e JOIN symbol s ON s.usr = e.dst"
                " WHERE e.kind = 'overrides' AND e.src = ?", (usr,))
        ]
        return {k: v for k, v in out.items() if v}

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

    def includes(self, path: str, transitive: bool = False,
                 limit: int = 200) -> Dict[str, Any]:
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
            "includes_transitively": self._closure(file_id, "includes", limit),
            "included_by_transitively": self._closure(file_id, "included_by",
                                                      limit),
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

    def _closure(self, file_id: int, direction: str, limit: int
                 ) -> List[str]:
        col, other = ("from_file", "to_file") if direction == "includes" else (
            "to_file", "from_file")
        seen: Set[int] = {file_id}
        frontier = {file_id}
        out: List[str] = []
        while frontier and len(out) < limit:
            placeholders = ",".join("?" * len(frontier))
            rows = self.conn.execute(
                f"SELECT DISTINCT {other} AS u FROM raw_include"
                f" WHERE {col} IN ({placeholders})", list(frontier))
            nxt = {r["u"] for r in rows} - seen
            if not nxt:
                break
            seen |= nxt
            out.extend(sorted(
                self.rel(self.store.file_path(u)) for u in nxt
            ))
            frontier = nxt
        return out[:limit]

    def file_symbols(self, path: str, kind: Optional[str] = None,
                     limit: int = 200) -> List[Dict[str, Any]]:
        out = self.symbols_in_file(path, include_system=True)
        if kind:
            out = [s for s in out if s.get("kind") == kind]
        return out[:limit]

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

        # -- direct: the index records this dependency ------------------------
        for entry in self.callers(usr, limit=limit):
            add(direct, entry, "calls this symbol")

        for entry in inherits.get("derived", []):
            add(direct, entry, "derives from this type")

        # Referencing a variable, field or type is a real dependency: change
        # the type and every one of these uses is affected immediately.  A
        # callable is different, and is handled below.
        if not _is_callable(target["kind"]):
            for entry in self._edges(usr, ("references",), "in", limit):
                add(direct, entry, "uses this symbol")

        # -- possible: it depends on run-time behaviour ----------------------
        for entry in inherits.get("overrides", []):
            add(possible, entry,
                "overrides this method, so a call through the base may reach it")

        for entry in inherits.get("descendants", []):
            add(possible, entry,
                "inherits from this type; inherited members carry the change")

        for entry in self._edges(usr, ("calls_indirect",), "in", limit):
            add(possible, entry,
                "calls this through a function pointer, so the target is only "
                "known at run time")

        for entry in self._edges(usr, ("references",), "in", limit):
            if _is_callable(target["kind"]):
                add(possible, entry,
                    "takes the address of this function; the call site is "
                    "recorded against the pointer, not against this symbol")

        for entry in self._edges(usr, ("specializes", "instantiates"), "in",
                                 limit):
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
                for entry in self.callers(caller_usr, limit=limit,
                                          with_usr=True):
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
            if not frontier or len(indirect) >= limit:
                break

        return {
            "symbol": target["qualified"] or target["name"],
            "location": self.loc(target["file_id"], target["line"]),
            "direct": list(direct.values())[:limit],
            "indirect": list(indirect.values())[:limit],
            "possible": list(possible.values())[:limit],
            "note": _IMPACT_NOTE,
        }

    # -- dependencies --------------------------------------------------------

    def symbol_dependencies(self, usr: str, limit: int = 200
                            ) -> Dict[str, Any]:
        rows = self.conn.execute(
            "SELECT e.kind, s.* FROM raw_edge e JOIN symbol s ON s.usr = e.dst"
            " WHERE e.src = ? AND e.kind IN (%s)"
            " ORDER BY e.kind, s.qualified LIMIT ?"
            % ",".join("?" * len(DEPENDENCY_EDGES)),
            [usr, *DEPENDENCY_EDGES, limit],
        )
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            grouped.setdefault(r["kind"], []).append(self._ref_from_row(r))
        return grouped

    def file_dependencies(self, path: str, limit: int = 200
                          ) -> Dict[str, Any]:
        return self.includes(path, transitive=True, limit=limit)

    # -- change detection ----------------------------------------------------

    def symbols_in_range(self, path: str, start: int, end: int
                         ) -> List[Dict[str, Any]]:
        file_id = self._file_id(path)
        if file_id is None:
            return []
        rows = self.conn.execute(
            "SELECT * FROM symbol WHERE file_id = ? AND line <= ?"
            " AND COALESCE(end_line, line) >= ? ORDER BY line",
            (file_id, end, start),
        )
        return [self._ref_from_row(r) for r in rows]

    def containers_in_range(self, path: str, start: int, end: int
                            ) -> List[Dict[str, Any]]:
        """Symbols that *contain* the range, not merely overlap its lines.

        A changed line inside a function body belongs to that function even
        when the edit is to a local variable the index does not track.
        """
        file_id = self._file_id(path)
        if file_id is None:
            return []
        rows = self.conn.execute(
            "SELECT * FROM symbol WHERE file_id = ? AND line <= ?"
            " AND COALESCE(end_line, line) >= ? AND kind IN"
            " ('function','method','constructor','destructor','class','struct',"
            "'namespace','conversion_function')"
            " ORDER BY (COALESCE(end_line,line) - line) ASC",
            (file_id, start, end),
        )
        return [self._ref_from_row(r) for r in rows]

    def tus_reaching(self, path: str, limit: int = 500) -> List[str]:
        """Translation units whose facts would change if this file changed."""
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
        for rel in self._closure(file_id, "included_by", 2000):
            fid = self._file_id(rel)
            if fid is not None:
                ids.add(fid)
        if ids:
            q = ("SELECT DISTINCT file_id FROM tu WHERE file_id IN (%s)"
                 % ",".join("?" * len(ids)))
            tus |= {r["file_id"] for r in self.conn.execute(q, list(ids))}
        return sorted(self.rel(self.store.file_path(t)) for t in tus)[:limit]

    # -- diagnostics ---------------------------------------------------------

    def diagnostics(self, path: Optional[str] = None, limit: int = 50
                    ) -> List[Dict[str, Any]]:
        args: List[Any] = []
        q = ("SELECT d.*, f.path AS path FROM raw_diag d"
             " LEFT JOIN file f ON f.id = d.file_id WHERE d.severity IN"
             " ('error','fatal','warning')")
        if path:
            file_id = self._file_id(path)
            if file_id is None:
                return []
            q += " AND d.file_id = ?"
            args.append(file_id)
        q += " ORDER BY d.severity DESC, f.path LIMIT ?"
        args.append(limit)
        return [
            {
                "severity": r["severity"],
                "location": self.loc(r["file_id"], r["line"]),
                "message": r["message"],
            }
            for r in self.conn.execute(q, args)
        ]


CALLABLE_KINDS = ("function", "method", "constructor", "destructor",
                  "conversion_function", "function_template")

_IMPACT_NOTE = (
    "direct: the index records this dependency. "
    "indirect: reached through the callers of the direct entries, so the effect "
    "is real but not at the first hop. "
    "possible: affected only if run-time dispatch reaches here - a virtual "
    "call, a function pointer, or a template instantiation."
)


def _is_callable(kind: Optional[str]) -> bool:
    return kind in CALLABLE_KINDS


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
