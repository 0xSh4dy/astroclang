"""The store: file interning, per-translation-unit ingest, and the merge.

The merge is the part worth testing hard.  A header is reported by every
translation unit that includes it, so the same USR arrives many times with
different file ids and different amounts of information; something has to
decide which report becomes the symbol a caller sees.
"""

import tempfile
import unittest
from pathlib import Path

from astroclang.facts import (EdgeFact, FileFact, IncludeFact, SymbolFact,
                                  TranslationUnit)
from astroclang.store import Store


def tu(path, files, symbols=(), edges=(), includes=()):
    return TranslationUnit(
        path=str(path),
        config_source="compile_commands.json",
        files=[FileFact(local_id=i, path=str(p), is_system=sys)
               for i, (p, sys) in enumerate(files)],
        symbols=list(symbols),
        edges=list(edges),
        includes=list(includes),
        complete=True,
    )


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "src").mkdir()
        (self.root / "include").mkdir()
        self.store = Store(self.root / ".idx" / "index.db",
                           project_root=self.root)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def header(self):
        return self.root / "include" / "a.h"

    def source(self, name="a.cpp"):
        return self.root / "src" / name


class TestFileInterning(StoreCase):
    def test_same_path_is_one_file(self):
        a = self.store.file_id("/p/x.h")
        self.assertEqual(a, self.store.file_id("/p/x.h"))
        self.assertNotEqual(a, self.store.file_id("/p/y.h"))

    def test_paths_are_normalised(self):
        a = self.store.file_id("/p/./sub/../x.h")
        self.assertEqual(a, self.store.file_id("/p/x.h"))

    def test_a_relative_path_resolves_against_the_project_root(self):
        # A compilation database writes its entries relative to the build
        # directory, while an include is reported as the path the preprocessor
        # opened.  Both name the same file and must intern to one row, or the
        # file's symbols attach to one id and its translation unit to another.
        absolute = self.store.file_id(str(self.source()))
        relative = self.store.file_id("src/a.cpp")
        self.assertEqual(absolute, relative)

    def test_a_symlinked_path_is_the_same_file(self):
        link = self.root / "link-to-src"
        try:
            link.symlink_to(self.root / "src")
        except OSError as exc:  # pragma: no cover - filesystem dependent
            self.skipTest(f"cannot create symlink: {exc}")
        self.assertEqual(self.store.file_id(str(link / "a.cpp")),
                         self.store.file_id(str(self.source())))

    def test_project_membership_is_recorded(self):
        inside = self.store.file_id(str(self.source()))
        outside = self.store.file_id("/usr/include/vector", is_system=True)
        self.assertEqual(self.store.file_path(inside), str(self.source()))
        row = self.store.connection().execute(
            "SELECT in_project, is_system FROM file WHERE id = ?", (inside,)
        ).fetchone()
        self.assertEqual((row["in_project"], row["is_system"]), (1, 0))
        row = self.store.connection().execute(
            "SELECT in_project, is_system FROM file WHERE id = ?", (outside,)
        ).fetchone()
        self.assertEqual((row["in_project"], row["is_system"]), (0, 1))


class TestIngest(StoreCase):
    def test_local_ids_are_remapped(self):
        # The stream numbers its files from zero on every run.  A second run
        # over a different file will number a different header as 0; if the
        # ids were stored as given, both headers would collapse into one.
        h1 = self.root / "include" / "one.h"
        h2 = self.root / "include" / "two.h"
        self.store.ingest(tu(self.source("a.cpp"), [(self.source("a.cpp"), False),
                                                    (h1, False)],
                             symbols=[SymbolFact(usr="u1", kind="function",
                                                 file=1, line=3)]))
        self.store.ingest(tu(self.source("b.cpp"), [(self.source("b.cpp"), False),
                                                    (h2, False)],
                             symbols=[SymbolFact(usr="u2", kind="function",
                                                 file=1, line=3)]))
        rows = self.store.connection().execute(
            "SELECT usr, file_id FROM raw_symbol ORDER BY usr").fetchall()
        ids = {r["usr"]: r["file_id"] for r in rows}
        self.assertNotEqual(ids["u1"], ids["u2"])
        self.assertEqual(self.store.file_path(ids["u1"]), str(h1))
        self.assertEqual(self.store.file_path(ids["u2"]), str(h2))

    def test_reindexing_replaces_rather_than_merges(self):
        src = self.source()
        self.store.ingest(tu(src, [(src, False)],
                             symbols=[SymbolFact(usr="gone", kind="function"),
                                      SymbolFact(usr="stays", kind="function")]))
        self.store.ingest(tu(src, [(src, False)],
                             symbols=[SymbolFact(usr="stays", kind="function")]))
        rows = {r["usr"] for r in self.store.connection().execute(
            "SELECT usr FROM raw_symbol")}
        self.assertEqual(rows, {"stays"})
        self.assertEqual(self.store.stats()["translation_units"], 1)

    def test_edges_and_includes_are_stored(self):
        src, hdr = self.source(), self.header()
        self.store.ingest(tu(
            src, [(src, False), (hdr, False)],
            symbols=[SymbolFact(usr="a", kind="function", file=0, line=1),
                     SymbolFact(usr="b", kind="function", file=1, line=1)],
            edges=[EdgeFact(kind="calls", src="a", dst="b", file=0, line=4,
                            weight=3, flags={"virt": 1})],
            includes=[IncludeFact(from_file=0, to_file=1, line=1,
                                  spelled="a.h")],
        ))
        row = self.store.connection().execute(
            "SELECT * FROM raw_edge").fetchone()
        self.assertEqual((row["kind"], row["src"], row["dst"]), ("calls", "a", "b"))
        self.assertEqual(row["weight"], 3)
        self.assertEqual(row["flags"], '{"virt": 1}')
        inc = self.store.connection().execute("SELECT * FROM raw_include").fetchone()
        self.assertEqual(self.store.file_path(inc["from_file"]), str(src))
        self.assertEqual(self.store.file_path(inc["to_file"]), str(hdr))

    def test_stats_are_kept_per_translation_unit(self):
        src = self.source()
        tu_id = self.store.ingest(
            tu(src, [(src, False)], symbols=[SymbolFact(usr="a", kind="function")]),
            stamp="abc")
        self.assertEqual(self.store.tu_stamp(self.store.file_id(str(src))), "abc")
        self.assertIsNotNone(self.store.tu_for_file(self.store.file_id(str(src))))
        self.assertGreater(tu_id, 0)

    def test_a_stream_without_a_translation_unit_is_refused(self):
        # Facts with no TU record cannot be attributed to anything, and
        # storing them would silently attach them to whatever was last seen.
        with self.assertRaises(ValueError):
            self.store.ingest(TranslationUnit(complete=True))


class TestMerge(StoreCase):
    def _merge(self):
        self.store.rebuild_symbols()

    def test_a_full_record_beats_a_stub(self):
        src = self.source()
        self.store.ingest(tu(src, [(src, False)], symbols=[
            SymbolFact(usr="u", kind="method", qualified="T::go", stub=True,
                       file=0, line=100),
            SymbolFact(usr="u", kind="method", qualified="T::go",
                       signature="(int)", file=0, line=7),
        ]))
        self._merge()
        row = self.store.connection().execute(
            "SELECT stub, line, signature FROM symbol WHERE usr = 'u'").fetchone()
        self.assertEqual(row["stub"], 0)
        self.assertEqual(row["line"], 7)
        self.assertEqual(row["signature"], "(int)")

    def test_a_project_file_beats_a_system_header(self):
        # A symbol declared in a system header and defined in the project must
        # resolve to the project's location, or every report sends the reader
        # into libstdc++.
        src = self.source()
        self.store.ingest(tu(src, [(src, False),
                                   ("/usr/include/x.h", True)], symbols=[
            SymbolFact(usr="u", kind="method", file=1, line=9),
            SymbolFact(usr="u", kind="method", file=0, line=42),
        ]))
        self._merge()
        row = self.store.connection().execute(
            "SELECT file_id, line FROM symbol WHERE usr = 'u'").fetchone()
        self.assertEqual(self.store.file_path(row["file_id"]), str(src))
        self.assertEqual(row["line"], 42)

    def test_a_record_that_knows_the_definition_beats_one_that_does_not(self):
        # A method declared in a header is reported by every translation unit
        # that includes it; only the one holding the definition can say where
        # the body is.  If the merge picks by file and line alone the winner
        # depends on how many files were indexed, so adding an unrelated file
        # could strip a symbol of its definition - and with it the ability to
        # find the symbol by the line its body is on.
        hdr = self.header()
        declaring = self.source("declaring.cpp")
        defining = self.source("defining.cpp")
        self.store.ingest(tu(declaring, [(declaring, False), (hdr, False)],
                             symbols=[SymbolFact(usr="u", kind="method",
                                                 qualified="T::go", file=1,
                                                 line=4)]))
        self.store.ingest(tu(defining, [(defining, False), (hdr, False)],
                             symbols=[SymbolFact(usr="u", kind="method",
                                                 qualified="T::go", file=1,
                                                 line=4, def_file=0,
                                                 def_line=90, end_line=95)]))
        self._merge()
        row = self.store.connection().execute(
            "SELECT def_file_id, def_line FROM symbol WHERE usr = 'u'"
        ).fetchone()
        self.assertEqual(self.store.file_path(row["def_file_id"]),
                         str(defining))
        self.assertEqual(row["def_line"], 90)

    def test_translation_unit_count_is_recorded(self):
        hdr = self.header()
        for name in ("a.cpp", "b.cpp", "c.cpp"):
            src = self.source(name)
            self.store.ingest(tu(src, [(src, False), (hdr, False)], symbols=[
                SymbolFact(usr="shared", kind="method", file=0, line=1),
                SymbolFact(usr="shared", kind="method", file=1, line=5),
            ]))
        self._merge()
        row = self.store.connection().execute(
            "SELECT tu_count FROM symbol WHERE usr = 'shared'").fetchone()
        self.assertEqual(row["tu_count"], 3)

    def test_merge_is_idempotent(self):
        src = self.source()
        self.store.ingest(tu(src, [(src, False)], symbols=[
            SymbolFact(usr="u", kind="function", file=0, line=1)]))
        self._merge()
        first = [tuple(r) for r in self.store.connection().execute(
            "SELECT usr, line, tu_count FROM symbol")]
        self._merge()
        second = [tuple(r) for r in self.store.connection().execute(
            "SELECT usr, line, tu_count FROM symbol")]
        self.assertEqual(first, second)

    def test_dropping_a_translation_unit_removes_its_facts(self):
        src = self.source()
        tu_id = self.store.ingest(tu(src, [(src, False)], symbols=[
            SymbolFact(usr="u", kind="function", file=0, line=1)],
            edges=[EdgeFact(kind="calls", src="u", dst="u", file=0, line=1)]))
        self.store.ingest(tu(self.source("b.cpp"),
                             [(self.source("b.cpp"), False)]))
        self.store.drop_tu(tu_id)
        self.assertEqual(
            self.store.connection().execute(
                "SELECT COUNT(*) FROM raw_symbol").fetchone()[0], 0)
        self.assertEqual(
            self.store.connection().execute(
                "SELECT COUNT(*) FROM raw_edge").fetchone()[0], 0)
        self.assertEqual(self.store.stats()["translation_units"], 1)


class TestSchemaRejection(StoreCase):
    def test_schema_version_is_recorded(self):
        row = self.store.connection().execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        self.assertEqual(int(row["value"]), 1)


class TestPlannerStatistics(StoreCase):
    """ANALYZE, which is what keeps a lookup from becoming a table scan."""

    def test_a_new_store_has_no_statistics(self):
        self.assertFalse(self.store.has_statistics())

    def test_analyzing_gives_the_planner_something_to_go_on(self):
        self.store.analyze()
        self.assertTrue(self.store.has_statistics())

    def test_analyzing_leaves_the_facts_alone(self):
        before = self.store.stats()
        self.store.analyze()
        self.assertEqual(self.store.stats(), before)

    def test_statistics_track_the_table_they_describe(self):
        # A count taken from stale statistics is a wrong answer, not a slow
        # one, so what the planner is told has to match what is in the table.
        src = self.source()
        self.store.ingest(tu(
            src, [(src, False)],
            symbols=[SymbolFact(usr="a", kind="function"),
                     SymbolFact(usr="b", kind="function")],
            edges=[EdgeFact(kind="calls", src="a", dst="b", file=0, line=4)],
        ))
        self.store.analyze()
        row = self.store.connection().execute(
            "SELECT stat FROM sqlite_stat1 WHERE tbl = 'raw_edge'").fetchone()
        self.assertEqual(int(row["stat"].split()[0]), self.store.stats()["edges"])


if __name__ == "__main__":
    unittest.main()
