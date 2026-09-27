"""SQLite schema for the semantic index.

Two layers, deliberately kept apart.

The *raw* layer records what each translation unit said, with a ``tu_id`` on
every row.  It is a faithful copy of the extractor's fact stream and nothing
more.  Re-indexing a translation unit is therefore ``DELETE FROM raw_* WHERE
tu_id = ?`` followed by a re-insert: no bookkeeping, no partial merges, and no
way for a stale fact to survive a change.

The *merged* layer holds one row per symbol for the whole project.  The same
header symbol is reported by every translation unit that includes the header,
and a caller asking "what is Foo" wants one answer, not two hundred.  It is
derived from the raw layer and rebuilt wholesale whenever the raw layer
changes; rebuilding is a single INSERT ... SELECT, which is far cheaper than
making every query carry the merge logic.

Edges are left unmerged.  Aggregating a call edge across translation units
loses the call sites, and the call sites are what a reviewer actually wants.
"""

SCHEMA_VERSION = 1

DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- A file the index has seen, whether or not it belongs to the project.
-- System headers are kept because edges point into them; they are flagged so
-- a query can leave them out.
CREATE TABLE IF NOT EXISTS file (
    id         INTEGER PRIMARY KEY,
    path       TEXT NOT NULL UNIQUE,
    is_system  INTEGER NOT NULL DEFAULT 0,
    in_project INTEGER NOT NULL DEFAULT 0
);

-- One row per translation unit that has been analysed.
CREATE TABLE IF NOT EXISTS tu (
    id            INTEGER PRIMARY KEY,
    file_id       INTEGER NOT NULL REFERENCES file(id) ON DELETE CASCADE,
    config_source TEXT,
    config_detail TEXT,
    degraded      INTEGER NOT NULL DEFAULT 0,
    errors        INTEGER NOT NULL DEFAULT 0,
    -- Hash of the TU's own bytes plus the compile command, so a re-run can
    -- skip work that has not changed.
    stamp         TEXT,
    indexed_at    REAL,
    UNIQUE (file_id)
);

-- Facts exactly as the extractor reported them, scoped to a translation unit.
CREATE TABLE IF NOT EXISTS raw_symbol (
    tu_id       INTEGER NOT NULL REFERENCES tu(id) ON DELETE CASCADE,
    usr         TEXT    NOT NULL,
    stub        INTEGER NOT NULL DEFAULT 0,
    kind        TEXT    NOT NULL,
    name        TEXT,
    qualified   TEXT,
    signature   TEXT,
    type_text   TEXT,
    file_id     INTEGER REFERENCES file(id),
    line        INTEGER,
    col         INTEGER,
    end_line    INTEGER,
    end_col     INTEGER,
    def_file_id INTEGER REFERENCES file(id),
    def_line    INTEGER,
    def_col     INTEGER,
    parent_usr  TEXT,
    flags       TEXT
);

CREATE TABLE IF NOT EXISTS raw_edge (
    tu_id   INTEGER NOT NULL REFERENCES tu(id) ON DELETE CASCADE,
    kind    TEXT    NOT NULL,
    src     TEXT    NOT NULL,
    dst     TEXT    NOT NULL,
    file_id INTEGER REFERENCES file(id),
    line    INTEGER,
    weight  INTEGER NOT NULL DEFAULT 1,
    flags   TEXT
);

CREATE TABLE IF NOT EXISTS raw_include (
    tu_id     INTEGER NOT NULL REFERENCES tu(id) ON DELETE CASCADE,
    from_file INTEGER NOT NULL REFERENCES file(id),
    to_file   INTEGER NOT NULL REFERENCES file(id),
    line      INTEGER,
    angled    INTEGER NOT NULL DEFAULT 0,
    spelled   TEXT
);

CREATE TABLE IF NOT EXISTS raw_diag (
    tu_id    INTEGER NOT NULL REFERENCES tu(id) ON DELETE CASCADE,
    severity TEXT    NOT NULL,
    file_id  INTEGER REFERENCES file(id),
    line     INTEGER,
    col      INTEGER,
    message  TEXT
);

-- The merge of raw_symbol across every translation unit.
CREATE TABLE IF NOT EXISTS symbol (
    usr         TEXT PRIMARY KEY,
    stub        INTEGER NOT NULL DEFAULT 0,
    kind        TEXT    NOT NULL,
    name        TEXT,
    qualified   TEXT,
    signature   TEXT,
    type_text   TEXT,
    file_id     INTEGER REFERENCES file(id),
    line        INTEGER,
    col         INTEGER,
    end_line    INTEGER,
    end_col     INTEGER,
    def_file_id INTEGER REFERENCES file(id),
    def_line    INTEGER,
    def_col     INTEGER,
    parent_usr  TEXT,
    flags       TEXT,
    -- How many translation units mention this symbol.  A cheap proxy for how
    -- widely used it is, and the first thing to look at when judging whether
    -- a change is local.
    tu_count    INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_raw_symbol_usr  ON raw_symbol(usr);
CREATE INDEX IF NOT EXISTS idx_raw_symbol_file ON raw_symbol(file_id);
CREATE INDEX IF NOT EXISTS idx_raw_edge_src    ON raw_edge(kind, src);
CREATE INDEX IF NOT EXISTS idx_raw_edge_dst    ON raw_edge(kind, dst);
CREATE INDEX IF NOT EXISTS idx_raw_include_from ON raw_include(from_file);
CREATE INDEX IF NOT EXISTS idx_raw_include_to   ON raw_include(to_file);
CREATE INDEX IF NOT EXISTS idx_symbol_name     ON symbol(name);
CREATE INDEX IF NOT EXISTS idx_symbol_qualified ON symbol(qualified);
CREATE INDEX IF NOT EXISTS idx_symbol_kind     ON symbol(kind);
CREATE INDEX IF NOT EXISTS idx_symbol_file     ON symbol(file_id);
CREATE INDEX IF NOT EXISTS idx_symbol_parent   ON symbol(parent_usr);
CREATE INDEX IF NOT EXISTS idx_symbol_def_file ON symbol(def_file_id);
"""

# Rebuilding `symbol` from `raw_symbol` in one statement keeps the merge rule
# in a single place.  The ORDER BY decides which of several reports of the same
# USR wins.  In order of precedence: a full record beats a stub; a record that
# knows where the definition is beats one that does not, because only the
# translation unit that contains a definition can report its location and the
# ones that merely included the header cannot; a project file beats a system
# header; and the remaining ties break on file and line so the result is stable
# across runs rather than dependent on insertion order.
#
# That third term is not cosmetic.  A method declared in a header is reported
# by every translation unit that includes it, all of them agreeing on the
# declaration and only one of them knowing the definition.  Without it the
# winner is whichever row the tie-break happened to favour, so adding an
# unrelated file to the project could strip a symbol of its definition - and
# with it, the ability to find the symbol by the line its body is on.
REBUILD_SYMBOLS = """
DELETE FROM symbol;

WITH counts(usr, tu_count) AS (
    SELECT usr, COUNT(DISTINCT tu_id) FROM raw_symbol GROUP BY usr
),
ranked AS (
    SELECT
        r.*,
        ROW_NUMBER() OVER (
            PARTITION BY r.usr
            ORDER BY r.stub ASC,
                     (r.def_file_id IS NULL) ASC,
                     COALESCE(f.in_project, 0) DESC,
                     COALESCE(f.is_system, 0) ASC,
                     COALESCE(r.file_id, 2147483647) ASC,
                     COALESCE(r.line, 0) ASC
        ) AS rn
    FROM raw_symbol r
    LEFT JOIN file f ON f.id = r.file_id
)
INSERT INTO symbol (
    usr, stub, kind, name, qualified, signature, type_text,
    file_id, line, col, end_line, end_col,
    def_file_id, def_line, def_col, parent_usr, flags, tu_count
)
SELECT ranked.usr, ranked.stub, ranked.kind, ranked.name, ranked.qualified,
       ranked.signature, ranked.type_text,
       ranked.file_id, ranked.line, ranked.col, ranked.end_line, ranked.end_col,
       ranked.def_file_id, ranked.def_line, ranked.def_col,
       ranked.parent_usr, ranked.flags, counts.tu_count
FROM ranked
JOIN counts ON counts.usr = ranked.usr
WHERE ranked.rn = 1;
"""
