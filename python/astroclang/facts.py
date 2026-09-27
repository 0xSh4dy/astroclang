"""Reader for the extractor's JSON Lines fact stream.

Fact records use single-letter keys because they are written once per
occurrence and read once; the field names live here and nowhere else, so the
abbreviation costs a reader nothing after this file.

File ids are interned *per translation unit*.  Nothing outside the extractor
may assume otherwise: two runs over the same project will number the same
header differently, and a consumer that treated them as global would attribute
every edge to whichever file happened to share the number.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional


@dataclass
class FileFact:
    local_id: int
    path: str
    is_system: bool


@dataclass
class SymbolFact:
    usr: str
    kind: str
    name: str = ""
    qualified: str = ""
    signature: str = ""
    type_text: str = ""
    stub: bool = False
    file: Optional[int] = None
    line: Optional[int] = None
    col: Optional[int] = None
    end_line: Optional[int] = None
    end_col: Optional[int] = None
    def_file: Optional[int] = None
    def_line: Optional[int] = None
    def_col: Optional[int] = None
    parent_usr: str = ""
    flags: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EdgeFact:
    kind: str
    src: str
    dst: str
    file: Optional[int] = None
    line: Optional[int] = None
    weight: int = 1
    flags: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IncludeFact:
    from_file: int
    to_file: int
    line: Optional[int] = None
    angled: bool = False
    spelled: str = ""


@dataclass
class DiagFact:
    severity: str
    file: Optional[int] = None
    line: Optional[int] = None
    col: Optional[int] = None
    message: str = ""


@dataclass
class TranslationUnit:
    """Everything one run of the extractor reported."""

    path: str = ""
    config_source: str = ""
    config_detail: str = ""
    degraded: bool = False
    errors: int = 0
    stats: Dict[str, Any] = field(default_factory=dict)
    files: List[FileFact] = field(default_factory=list)
    symbols: List[SymbolFact] = field(default_factory=list)
    edges: List[EdgeFact] = field(default_factory=list)
    includes: List[IncludeFact] = field(default_factory=list)
    diags: List[DiagFact] = field(default_factory=list)
    complete: bool = False


class FactStreamError(ValueError):
    """The stream ended in a state that makes its contents untrustworthy."""


def _int(d: Dict[str, Any], key: str) -> Optional[int]:
    v = d.get(key)
    return None if v is None else int(v)


def _flags(d: Dict[str, Any]) -> Dict[str, Any]:
    v = d.get("F")
    return v if isinstance(v, dict) else {}


def read_facts(fp: Iterator[str], source: str = "<stream>") -> TranslationUnit:
    """Parse a fact stream.

    A stream that does not end with its ``done`` record is rejected rather than
    partially accepted.  A truncated stream is what a crashed or killed
    extractor leaves behind, and silently indexing half a translation unit
    would produce a graph that looks complete and is not - the exact failure
    this tool exists to avoid.
    """
    tu = TranslationUnit()

    for lineno, line in enumerate(fp, 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FactStreamError(
                f"{source}:{lineno}: malformed fact record: {exc}"
            ) from exc

        kind = rec.get("t")

        if kind == "meta":
            key, value = rec.get("k"), rec.get("v")
            if key == "tu":
                tu.path = value or ""
            elif key == "config_source":
                tu.config_source = value or ""
            elif key == "config_detail":
                tu.config_detail = value or ""
            elif key == "degraded":
                tu.degraded = True
            elif key == "errors":
                tu.errors = int((value or {}).get("errors", 0))
            elif key == "stats":
                tu.stats = value if isinstance(value, dict) else {}

        elif kind == "f":
            tu.files.append(
                FileFact(
                    local_id=int(rec["i"]),
                    path=rec["p"],
                    is_system=bool(rec.get("sys")),
                )
            )

        elif kind == "sym":
            tu.symbols.append(
                SymbolFact(
                    usr=rec["u"],
                    kind=rec.get("k", "unknown"),
                    name=rec.get("n", ""),
                    # `q` is omitted when it equals `n`, which is the common
                    # case in C.  Reading it as empty would leave every C
                    # symbol without a qualified name.
                    qualified=rec.get("q") or rec.get("n", ""),
                    signature=rec.get("s", ""),
                    type_text=rec.get("ty", ""),
                    stub=bool(rec.get("stub")),
                    file=_int(rec, "f"),
                    line=_int(rec, "l"),
                    col=_int(rec, "c"),
                    end_line=_int(rec, "el"),
                    end_col=_int(rec, "ec"),
                    def_file=_int(rec, "df"),
                    def_line=_int(rec, "dl"),
                    def_col=_int(rec, "dc"),
                    parent_usr=rec.get("p", ""),
                    flags=_flags(rec),
                )
            )

        elif kind == "edge":
            tu.edges.append(
                EdgeFact(
                    kind=rec["k"],
                    src=rec["a"],
                    dst=rec["b"],
                    file=_int(rec, "f"),
                    line=_int(rec, "l"),
                    weight=int(rec.get("w", 1)),
                    flags=_flags(rec),
                )
            )

        elif kind == "inc":
            tu.includes.append(
                IncludeFact(
                    from_file=int(rec["f"]),
                    to_file=int(rec["b"]),
                    line=_int(rec, "l"),
                    angled=bool(rec.get("ang")),
                    spelled=rec.get("sp", ""),
                )
            )

        elif kind == "diag":
            tu.diags.append(
                DiagFact(
                    severity=rec.get("sev", "note"),
                    file=_int(rec, "f"),
                    line=_int(rec, "l"),
                    col=_int(rec, "c"),
                    message=rec.get("m", ""),
                )
            )

        elif kind == "done":
            tu.complete = True

    if not tu.complete:
        raise FactStreamError(_truncated(tu, source))
    return tu


def _truncated(tu: TranslationUnit, source: str) -> str:
    """Why a stream that stopped early stopped.

    Usually a crash or a killed process, and there is nothing more to say.  But
    Clang also ends this way when it gives up on something it cannot read - an
    unreadable precompiled header, say - and reports that as a diagnostic
    instead of by exiting non-zero.  The diagnostics read before the stream
    ended are then the only account of why, and discarding them leaves a
    message that says what happened and never why: the reader is told the
    extractor did not finish, when it finished and said so.
    """
    message = (f"{source}: fact stream is truncated (no 'done' record); "
               "the extractor did not finish this translation unit")
    # The first of the worst.  A fatal is the cause of the stop; anything after
    # it is the cascade, and "too many errors emitted" is the loudest of those.
    for severity in ("fatal", "error"):
        for diag in tu.diags:
            if diag.severity == severity and diag.message:
                return (f"{message}; it reported {severity}: {diag.message} "
                        f"before stopping")
    return message


def read_facts_file(path) -> TranslationUnit:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return read_facts(fh, source=str(path))
