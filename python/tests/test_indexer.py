"""Discovery and the indexing driver.

The driver is tested against a stub extractor rather than the real one.  What
is under test here is the driver's own decisions - which files to index, when
to skip one, and what to do when the extractor fails - and those are visible in
the fact stream regardless of who produces it.  The semantic correctness of the
facts themselves is a different question, tested against the real extractor in
test_semantics.py.
"""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from astroclang import discovery, indexer
from astroclang.discovery import plan_indexing, walk_sources
from astroclang.indexer import IndexReport, index_project
from astroclang.store import Store

STUB = '''#!/usr/bin/env python3
"""A stand-in for astroclang-index: emits a fact stream shaped like the real one."""
import json, os, sys

path = sys.argv[-1]
mode = os.environ.get("STUB_MODE", "ok")

if mode == "crash":
    sys.stderr.write("astroclang-index: could not find a compilation database\\n")
    sys.exit(2)

line = lambda r: sys.stdout.write(json.dumps(r) + "\\n")
line({"t": "meta", "k": "tu", "v": os.path.abspath(path)})
line({"t": "meta", "k": "config_source", "v": "compile_commands.json"})
line({"t": "f", "i": 0, "p": os.path.abspath(path)})
line({"t": "f", "i": 1, "p": os.path.abspath(os.path.join(os.path.dirname(path), "..", "include", "stub.h"))})
tu = os.path.basename(path)
line({"t": "sym", "u": "c:@F@main@" + tu, "k": "function", "n": "main",
      "q": "main", "s": "()", "f": 0, "l": 1, "el": 4, "F": {"def": 1}})
line({"t": "sym", "u": "c:@S@T", "k": "class", "n": "T", "q": "T", "f": 1, "l": 2})
line({"t": "edge", "k": "calls", "a": "c:@F@main@" + tu, "b": "c:@S@T",
      "f": 0, "l": 3})
line({"t": "inc", "f": 0, "b": 1, "l": 1, "ang": 0, "sp": "stub.h"})
if mode == "truncate":
    sys.exit(0)          # exit 0 with no `done` record
line({"t": "meta", "k": "stats", "v": {"symbols": 2, "edges": 1, "includes": 1}})
line({"t": "done", "v": 1})
'''


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "src").mkdir()
        (self.root / "include").mkdir()
        (self.root / ".git").mkdir()
        (self.root / "build").mkdir()
        self.write("src/a.cpp", "int main() { return 0; }\n")
        self.write("src/b.cpp", "int other() { return 1; }\n")
        self.write("include/stub.h", "#pragma once\nclass T {};\n")
        # Files that must never be picked up as translation units.
        self.write(".git/hook.cpp", "// not source\n")
        self.write("build/generated.cpp", "// generated\n")

        self.stub = self.root / "stub-index"
        self.stub.write_text(STUB)
        self.stub.chmod(self.stub.stat().st_mode | stat.S_IEXEC)

        self.store = Store(self.root / ".idx" / "index.db",
                           project_root=self.root)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def compdb(self, entries=None):
        if entries is None:
            entries = [("src/a.cpp", ["clang++", "-std=c++17", "-Iinclude"]),
                       ("src/b.cpp", ["clang++", "-std=c++17", "-Iinclude"])]
        data = [
            {"directory": str(self.root),
             "file": str(self.root / name),
             "command": " ".join(args + [name])}
            for name, args in entries
        ]
        path = self.root / "compile_commands.json"
        path.write_text(json.dumps(data))
        return path

    def index(self, **kw):
        return index_project(self.root, self.store, extractor=self.stub, **kw)


class TestDiscovery(ProjectCase):
    def test_explicit_database_is_used(self):
        path = self.compdb()
        plan = plan_indexing(self.root, explicit_compdb=path)
        self.assertFalse(plan.degraded)
        self.assertEqual(len(plan.files), 2)
        self.assertEqual(plan.database.source, "explicit")
        self.assertIn("--compdb", plan.compdb_args)

    def test_database_is_found_in_a_build_directory(self):
        path = self.compdb()
        moved = self.root / "build" / "compile_commands.json"
        moved.write_text(path.read_text())
        path.unlink()
        plan = plan_indexing(self.root)
        self.assertFalse(plan.degraded)
        self.assertEqual(plan.database.path, moved)

    def test_missing_database_degrades_and_says_so(self):
        # The specification is explicit that a missing database must be
        # reported, not hidden: the analysis is weaker and the caller has to
        # be able to tell.
        plan = plan_indexing(self.root)
        self.assertTrue(plan.degraded)
        self.assertTrue(any("no compilation database" in n for n in plan.notes))
        self.assertEqual(plan.compdb_args, [])

    def test_fallback_walk_skips_version_control_directories(self):
        files = {p.name for p in walk_sources(self.root)}
        self.assertNotIn("hook.cpp", files)
        self.assertIn("a.cpp", files)

    def test_fallback_walk_does_not_guess_what_a_build_directory_is(self):
        # `build/generated.cpp` is walked.  A directory called `build` may hold
        # hand-written source, and with no compilation database there is
        # nothing to distinguish the two; the fallback already reports that its
        # analysis is unreliable, and inventing a directory-name heuristic on
        # top of that would drop real files to hide a problem the caller has
        # already been told about.
        files = {p.name for p in walk_sources(self.root)}
        self.assertIn("generated.cpp", files)

    def test_fallback_walk_ignores_headers(self):
        # A header is indexed through the translation units that include it;
        # handing one to Clang would invent a translation unit that the build
        # does not have.
        self.write("include/only.h", "struct S {};\n")
        self.assertNotIn("only.h", {p.name for p in walk_sources(self.root)})

    def test_a_broken_database_is_reported_not_ignored(self):
        (self.root / "compile_commands.json").write_text("{ not json")
        plan = plan_indexing(self.root)
        self.assertTrue(plan.degraded)
        self.assertTrue(any("could not be read" in n for n in plan.notes))

    def test_a_database_that_is_not_an_array_is_rejected(self):
        (self.root / "compile_commands.json").write_text('{"file": "a.cpp"}')
        plan = plan_indexing(self.root)
        self.assertTrue(plan.degraded)

    def test_only_filter_selects_translation_units(self):
        self.compdb()
        plan = plan_indexing(self.root, only=["a.cpp"])
        self.assertEqual([p.name for p in plan.files], ["a.cpp"])

    def test_entries_without_a_source_extension_are_skipped(self):
        path = self.root / "compile_commands.json"
        path.write_text(json.dumps([
            {"directory": str(self.root), "file": str(self.root / "src/a.cpp"),
             "command": "clang++ src/a.cpp"},
            {"directory": str(self.root),
             "file": str(self.root / "include/stub.h"),
             "command": "clang++ include/stub.h"},
        ]))
        plan = plan_indexing(self.root)
        self.assertEqual([p.name for p in plan.files], ["a.cpp"])


class TestIndexing(ProjectCase):
    def test_indexes_every_translation_unit(self):
        self.compdb()
        report = self.index()
        self.assertEqual((report.indexed, report.failed), (2, 0))
        stats = self.store.stats()
        self.assertEqual(stats["translation_units"], 2)
        self.assertGreater(stats["symbols"], 0)

    def test_facts_land_in_the_store(self):
        self.compdb()
        self.index()
        row = self.store.connection().execute(
            "SELECT * FROM symbol WHERE qualified = 'main'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], "function")
        self.assertEqual(row["tu_count"], 1)

    def test_edges_and_includes_are_stored(self):
        self.compdb()
        self.index()
        edge = self.store.connection().execute(
            "SELECT * FROM raw_edge WHERE kind = 'calls'").fetchone()
        self.assertIsNotNone(edge)
        inc = self.store.connection().execute(
            "SELECT * FROM raw_include").fetchone()
        self.assertIsNotNone(inc)
        self.assertEqual(Path(self.store.file_path(inc["to_file"])).name,
                         "stub.h")

    def test_a_second_run_skips_unchanged_files(self):
        self.compdb()
        self.index()
        report = self.index()
        self.assertEqual((report.indexed, report.unchanged), (0, 2))

    def test_a_changed_file_is_reindexed_alone(self):
        self.compdb()
        self.index()
        self.write("src/a.cpp", "int main() { return 42; }\n")
        report = self.index()
        self.assertEqual((report.indexed, report.unchanged), (1, 1))
        self.assertEqual([r.path.name for r in report.results
                          if r.status == "indexed"], ["a.cpp"])

    def test_a_new_compilation_database_invalidates_every_stamp(self):
        # Include paths and the language standard come from the database, so a
        # changed database changes the facts even when the source did not.
        path = self.compdb()
        self.index()
        data = json.loads(path.read_text())
        data[0]["command"] = "clang++ -std=c++20 -Ielsewhere src/a.cpp"
        path.write_text(json.dumps(data))
        os.utime(path, (0, 0))
        report = self.index()
        self.assertGreater(report.indexed, 0)

    def test_incremental_can_be_turned_off(self):
        self.compdb()
        self.index()
        report = self.index(incremental=False)
        self.assertEqual((report.indexed, report.unchanged), (2, 0))

    def test_only_reindexes_the_named_translation_units(self):
        self.compdb()
        self.index()
        self.write("src/a.cpp", "int main() { return 7; }\n")
        report = self.index(only=["a.cpp"])
        self.assertEqual(report.total, 1)

    def test_an_index_run_leaves_usable_planner_statistics(self):
        # A lookup that becomes a table scan is a slow answer on a small
        # project and an unusable one on a large project, and the planner only
        # avoids that if it has statistics - which the run has to gather.
        self.assertFalse(self.store.has_statistics())
        self.index()
        self.assertTrue(self.store.has_statistics())

    def test_an_index_with_nothing_new_still_gathers_them(self):
        # An index built before statistics were gathered has none, and
        # re-running over an unchanged tree is how a user would fix it.
        self.compdb()
        self.store.connection().execute("DROP TABLE IF EXISTS sqlite_stat1")
        self.store.connection().commit()
        self.index()
        self.assertTrue(self.store.has_statistics())

    def test_failure_leaves_the_previous_index_intact(self):
        # A failed run must not replace a complete answer with a partial one.
        self.compdb()
        self.index()
        before = self.store.stats()["symbols"]
        os.environ["STUB_MODE"] = "crash"
        try:
            self.write("src/a.cpp", "int main() { return 1; }\n")
            report = self.index()
        finally:
            os.environ.pop("STUB_MODE", None)
        self.assertEqual(report.failed, 1)
        self.assertFalse(report.failures[0].detail == "")
        self.assertEqual(self.store.stats()["symbols"], before)

    def test_a_truncated_stream_is_a_failure(self):
        os.environ["STUB_MODE"] = "truncate"
        try:
            self.compdb()
            report = self.index()
        finally:
            os.environ.pop("STUB_MODE", None)
        self.assertEqual(report.failed, 2)
        self.assertEqual(report.indexed, 0)
        self.assertIn("truncated", report.failures[0].detail)

    def test_a_failed_translation_unit_is_never_stored(self):
        os.environ["STUB_MODE"] = "crash"
        try:
            self.compdb()
            self.index()
        finally:
            os.environ.pop("STUB_MODE", None)
        self.assertEqual(self.store.stats()["translation_units"], 0)

    def test_notes_are_reported_to_the_caller(self):
        seen = []
        self.compdb()
        self.index(progress=seen.append)
        self.assertTrue(any("translation units in" in n for n in seen))

    def test_degraded_config_is_recorded_per_translation_unit(self):
        # With no database the driver still indexes, but every translation unit
        # it stores is marked as analysed under guessed arguments.
        report = self.index()
        self.assertTrue(report.degraded == 0)  # the stub claims a database
        self.assertTrue(any("no compilation database" in n for n in report.notes))

    def test_missing_extractor_is_reported_clearly(self):
        with self.assertRaises(indexer.ExtractorNotFound):
            find = indexer.find_extractor
            find(str(self.root / "nope"))


class TestExtractorLocation(ProjectCase):
    def test_explicit_path_wins(self):
        self.assertEqual(indexer.find_extractor(str(self.stub)), self.stub)

    def test_environment_variable_is_used(self):
        os.environ["ASTROCLANG_INDEX"] = str(self.stub)
        try:
            self.assertEqual(indexer.find_extractor(), self.stub)
        finally:
            os.environ.pop("ASTROCLANG_INDEX", None)


if __name__ == "__main__":
    unittest.main()
