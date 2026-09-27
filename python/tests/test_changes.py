"""Reading a diff, and joining it to the index.

The parser tests run against real repositories built in a temporary directory
rather than against canned `git diff` text, because the text is git's to
change and a fixture that pins it would pass while the tool broke.  What is
asserted is the thing the rest of the system depends on: these lines of this
file are new.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from astroclang import changes, indexer
from astroclang.git import GitError, collapse_ranges, diff, repository_root
from astroclang.indexer import index_project
from astroclang.query import Query
from astroclang.store import Store

CORPUS = Path(__file__).resolve().parent / "corpus"

try:
    EXTRACTOR = indexer.find_extractor()
except indexer.ExtractorNotFound:
    EXTRACTOR = None


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True).stdout


class RepoCase(unittest.TestCase):
    """A copy of the corpus, committed, with an index built over it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "proj"
        shutil.copytree(CORPUS, self.root)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "t@example.invalid")
        git(self.root, "config", "user.name", "Test")
        self.commit("initial")

    def tearDown(self):
        self._tmp.cleanup()

    def commit(self, message: str) -> None:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", message)

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def edit(self, rel: str, old: str, new: str) -> Path:
        path = self.root / rel
        return self.write(rel, path.read_text().replace(old, new))

    def write_compdb(self) -> Path:
        """A compilation database for the corpus, one entry per source file."""
        commands = []
        for path in sorted((self.root / "src").iterdir()):
            if path.suffix not in (".c", ".cpp"):
                continue
            flags = (["clang", "-std=c11"] if path.suffix == ".c"
                     else ["clang++", "-std=c++17"])
            commands.append({
                "directory": str(self.root),
                "file": "src/" + path.name,
                "command": " ".join([*flags, "-fsyntax-only", "-Iinclude",
                                     "src/" + path.name]),
            })
        return self.write("compile_commands.json", json.dumps(commands))

    def build_index(self):
        compdb = self.write_compdb()
        store = Store(Path(self._tmp.name) / "index.db",
                      project_root=self.root)
        index_project(self.root, store, extractor=EXTRACTOR, compdb=compdb)
        self.addCleanup(store.close)
        return Query(store)


class TestDiffParsing(RepoCase):
    def test_a_modified_line_is_reported_in_the_new_file(self):
        self.edit("src/shapes.cpp",
                  'return "shape";', 'return "Shape";')
        d = diff(self.root, "HEAD")
        change = next(c for c in d.files if c.path == "src/shapes.cpp")
        self.assertEqual(change.status, "modified")
        self.assertEqual(change.lines, [(14, 14)])

    def test_a_deletion_names_the_line_it_was_on(self):
        # A deleted line does not exist in the new file, so there is no range
        # to report - but the change still belongs to whatever contains it.
        self.edit("src/shapes.cpp",
                  "Shape::Shape() = default;\nShape::~Shape() = default;\n",
                  "")
        d = diff(self.root, "HEAD")
        change = next(c for c in d.files if c.path == "src/shapes.cpp")
        self.assertTrue(change.lines)
        start, end = change.lines[0]
        self.assertEqual(start, end)

    def test_an_added_file_reports_its_whole_extent(self):
        self.write("src/extra.cpp", "#include \"shapes.h\"\n\n"
                                    "namespace app { int f() { return 1; } }\n")
        d = diff(self.root, "HEAD")
        change = next(c for c in d.files if c.path == "src/extra.cpp")
        self.assertEqual(change.status, "added")
        self.assertEqual(change.lines, [(1, 3)])

    def test_an_ignored_file_is_not_reported(self):
        self.write(".gitignore", "*.tmp\n")
        self.write("src/scratch.tmp", "junk\n")
        self.commit("ignore")
        d = diff(self.root, "HEAD")
        self.assertNotIn("src/scratch.tmp", {c.path for c in d.files})

    def test_a_diff_between_commits_has_no_working_tree_file_in_it(self):
        # An unsaved file is not in either commit, so it has no business in a
        # question about history.
        self.write("src/extra.cpp", "int f() { return 1; }\n")
        d = diff(self.root, "HEAD..HEAD")
        self.assertEqual([c.path for c in d.files], [])

    def test_a_directory_that_is_not_a_repository_says_so(self):
        outside = Path(self._tmp.name) / "loose"
        outside.mkdir()
        self.assertIsNone(repository_root(outside))
        with self.assertRaises(GitError):
            diff(outside, "HEAD")

    def test_ranges_are_collapsed_for_reading(self):
        # Adjacent and overlapping hunks are one region to a reader.
        self.assertEqual(collapse_ranges([(12, 14), (15, 15), (31, 31)]),
                         "12-15, 31")
        self.assertEqual(collapse_ranges([(7, 7)]), "7")


@unittest.skipIf(EXTRACTOR is None, "astroclang-index has not been built")
class TestChangedSymbols(RepoCase):
    def test_a_changed_body_names_the_function_that_contains_it(self):
        self.edit("src/shapes.cpp",
                  "return 3.14159265358979 * radius_ * radius_;",
                  "return 3.14159265358979 * radius_ * radius_ * 2.0;")
        query = self.build_index()
        result = changes.changed_symbols(query, diff(self.root, "HEAD"))
        found = {s["symbol"] for s in result["changed_symbols"]}
        self.assertIn("geo::Circle::area", found)

    def test_the_lexical_scope_is_reported_alongside(self):
        # A change inside a method is a change to its class and namespace too,
        # and those come from the parent links rather than from line ranges.
        self.edit("src/shapes.cpp",
                  "return 3.14159265358979 * radius_ * radius_;",
                  "return 3.14159265358979 * radius_ * radius_ * 2.0;")
        query = self.build_index()
        result = changes.changed_symbols(query, diff(self.root, "HEAD"))
        entry = next(s for s in result["changed_symbols"]
                     if s["symbol"] == "geo::Circle::area")
        self.assertEqual(entry["within"], ["geo::Circle", "geo"])

    def test_an_overload_is_told_apart_from_its_siblings(self):
        # Three functions named `scale`.  Reporting a change to one as a change
        # to `geo::scale` would name all three.
        self.edit("src/shapes.cpp",
                  "int scale(int value) { return value * 2; }",
                  "int scale(int value) { return value * 3; }")
        query = self.build_index()
        result = changes.changed_symbols(query, diff(self.root, "HEAD"))
        self.assertEqual([s["symbol"] for s in result["changed_symbols"]],
                         ["geo::scale"])
        self.assertEqual(result["changed_symbols"][0]["signature"], "(int)")

    def test_a_file_the_index_has_not_seen_is_named_rather_than_skipped(self):
        # An empty answer would read as "nothing changed", which is the one
        # thing it must not say.  The file is written after the index is
        # built, which is the case this is about: a file that appeared since.
        query = self.build_index()
        self.write("src/brand_new.cpp", "int g() { return 2; }\n")
        result = changes.changed_symbols(query, diff(self.root, "HEAD"))
        self.assertIn("src/brand_new.cpp", result.get("not_indexed", []))

    def test_a_changed_readme_is_not_a_gap_in_the_index(self):
        self.write("NOTES.md", "a note\n")
        query = self.build_index()
        result = changes.changed_symbols(query, diff(self.root, "HEAD"))
        self.assertNotIn("NOTES.md", result.get("not_indexed", []))

    def test_a_change_between_declarations_is_counted_rather_than_dropped(self):
        self.edit("include/shapes.h", "namespace geo {",
                  "namespace geo {\n// a comment between declarations\n")
        query = self.build_index()
        result = changes.changed_symbols(query, diff(self.root, "HEAD"))
        self.assertTrue(result.get("lines_outside_any_symbol")
                        or result["changed_symbols"])

    def test_impact_reaches_the_callers_of_what_changed(self):
        self.edit("src/shapes.cpp",
                  "return 3.14159265358979 * radius_ * radius_;",
                  "return 3.14159265358979 * radius_ * radius_ * 2.0;")
        query = self.build_index()
        result = changes.impact_of_changes(query, diff(self.root, "HEAD"))
        direct = {d["symbol"] for d in result["affected"].get("direct", [])}
        self.assertIn("app::measure_circle", direct)
        # The changed symbols are the subject of the question, not its answer.
        self.assertNotIn("geo::Circle::area", direct)

    def test_a_change_to_a_base_method_reports_the_overrides_as_possible(self):
        # A call through a `Shape *` may reach `Circle::area` at run time.  The
        # index cannot prove it does, and says `possible` rather than `direct`.
        self.edit("include/shapes.h",
                  "virtual double area() const = 0;",
                  "virtual double area() const = 0;  // pure")
        query = self.build_index()
        result = changes.impact_of_changes(query, diff(self.root, "HEAD"))
        possible = {p["symbol"]: p.get("reason", "")
                    for p in result["affected"].get("possible", [])}
        self.assertIn("geo::Circle::area", possible)

    def test_an_empty_diff_is_an_empty_answer_not_an_error(self):
        query = self.build_index()
        result = changes.changed_symbols(query, diff(self.root, "HEAD"))
        self.assertEqual(result["changed_symbols"], [])
        self.assertEqual(result["symbol_count"], 0)


if __name__ == "__main__":
    unittest.main()
