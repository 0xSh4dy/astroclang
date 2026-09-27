"""The command line: exit statuses, output, and the entry points.

The query subcommands are a thin layer over `tools.call`, which has its own
tests, so what is tested here is what the CLI itself decides: which status a
script can branch on, whether a failure goes to stderr as a payload or to
stdout as an answer, whether an index is found from a subdirectory, and whether
the two entry points reach the same code.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from astroclang import cli, indexer
from astroclang.query import Query
from astroclang.store import Store, default_db_path, read_meta

from tests.test_changes import RepoCase
from tests.test_query import Fixture

PYTHON_DIR = Path(__file__).resolve().parents[1]

try:
    EXTRACTOR = indexer.find_extractor()
except indexer.ExtractorNotFound:
    EXTRACTOR = None


def run(argv):
    """The CLI as a session: captured streams and the status it returned."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = cli.main(argv)
    return status, out.getvalue(), err.getvalue()


class CliCase(Fixture):
    """The synthetic index, at the path the CLI looks in.

    The fixture builds its store wherever it likes; the CLI looks under the
    project root, so the index is copied into place and reopened.  Testing the
    lookup as well as the query is the point.
    """

    def setUp(self):
        super().setUp()
        self.db = default_db_path(self.root)
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.store.close()  # checkpoint the WAL, so the copy is complete
        shutil.copy(self.root / ".idx" / "index.db", self.db)
        self.store = Store(self.db, project_root=self.root)
        self.q = Query(self.store)
        self.store.set_meta("root", str(self.root))

    def cli(self, *argv):
        return run(["--db", str(self.db), *argv])


class TestStatuses(CliCase):
    def test_an_answered_question_is_a_success(self):
        status, out, _ = self.cli("find", "mem::Fast")
        self.assertEqual(status, cli.OK)
        self.assertIn("mem::Fast", out)

    def test_a_question_that_cannot_be_asked_is_a_failure_on_stderr(self):
        # Not a crash and not an answer: an ambiguous name is a question the
        # tool refuses to guess at, and a script has to be able to tell.
        status, out, err = self.cli("symbol", "allocate")
        self.assertEqual(status, cli.FAILED)
        self.assertEqual(out, "")
        self.assertIn("more than one", json.loads(err)["error"])

    def test_no_index_names_the_command_that_would_build_one(self):
        status, _, err = run(["--db", str(self.root / "absent" / "i.db"),
                              "status"])
        self.assertEqual(status, cli.FAILED)
        self.assertIn("astroclang index", err)

    def test_a_missing_command_prints_usage_and_says_usage_was_wrong(self):
        status, _, err = run([])
        self.assertEqual(status, cli.USAGE)
        self.assertIn("COMMAND", err)

    def test_an_unknown_command_is_refused_by_argparse(self):
        with self.assertRaises(SystemExit) as caught:
            run(["get_everything"])
        self.assertEqual(caught.exception.code, cli.USAGE)


class TestOutput(CliCase):
    def test_json_is_the_answer_itself(self):
        status, out, _ = self.cli("--json", "callers", "mem::Allocator::size")
        self.assertEqual(status, cli.OK)
        self.assertEqual(json.loads(out)["callers"][0]["symbol"], "run")

    def test_the_summary_names_the_file_and_line_of_each_answer(self):
        _, out, _ = self.cli("callers", "mem::Allocator::size")
        # The caller's own location, which is where it is defined; the site of
        # the call is the parenthesised part.
        self.assertIn("run  src/use.cpp:2  (at src/use.cpp:4)", out)

    def test_a_question_with_no_answer_is_still_a_success(self):
        status, out, _ = self.cli("callees", "mem::Helper::grow")
        self.assertEqual(status, cli.OK)
        self.assertIn("nothing", out)

    def test_a_file_shaped_list_prints_as_file_and_line(self):
        # Includes carry `file` and `line` rather than `symbol` and
        # `location`, and both spell the handle a caller passes back.
        _, out, _ = self.cli("includes", "src/use.cpp")
        self.assertIn("include/iface.h:1", out)

    def test_status_reports_accuracy_where_a_reader_will_see_it(self):
        _, out, _ = self.cli("status")
        self.assertIn("accuracy", out)
        # This index was assembled by hand and records no compiler arguments,
        # which is not the same as knowing there were none.
        self.assertIn("unknown", out)


class TestIndexDiscovery(CliCase):
    def test_an_index_is_found_from_a_subdirectory(self):
        # Where an agent usually is: handed a path inside the tree rather than
        # the root of it.
        here = Path.cwd()
        try:
            os.chdir(self.root / "src")
            status, out, _ = run(["find", "mem::Fast"])
        finally:
            os.chdir(here)
        self.assertEqual(status, cli.OK)
        self.assertIn("mem::Fast", out)

    def test_a_query_uses_the_root_the_index_recorded(self):
        # Not the directory it was run from: otherwise `src/use.cpp` resolves
        # against the wrong tree and the answer is confidently wrong.
        self.assertEqual(cli._project_root(self.db, Path("/")), self.root)

    def test_an_index_that_recorded_no_root_falls_back_to_the_caller(self):
        self.store.set_meta("root", "")
        self.assertEqual(cli._project_root(self.db, Path("/tmp")), Path("/tmp"))


@unittest.skipIf(EXTRACTOR is None, "astroclang-index has not been built")
class TestIndexCommand(RepoCase):
    """`index` itself, over a real project with a real compilation database."""

    def db(self) -> Path:
        return Path(self._tmp.name) / "cli" / "index.db"

    def index(self, *extra, compdb=None):
        """`--quiet` and the rest belong to `index`, so they follow it.

        A compilation database is written unless `compdb=False`, and a caller
        testing what a *second* run does has to pass the same one back: the
        database's timestamp is part of what determines a translation unit's
        facts, so rewriting it is a real change and re-indexes everything.
        """
        argv = ["--db", str(self.db()), "index", str(self.root), "--quiet",
                *extra]
        if compdb is not False:
            argv += ["--compdb", str(compdb or self.write_compdb())]
        return run(argv)

    def test_indexing_writes_an_index_that_answers_questions(self):
        status, _, err = self.index()
        self.assertEqual(status, cli.OK)
        self.assertIn("indexed 4", err)
        status, out, _ = run(["--db", str(self.db()), "find", "geo::Circle"])
        self.assertEqual(status, cli.OK)
        self.assertIn("geo::Circle", out)

    def test_the_second_run_re_analyses_nothing(self):
        compdb = self.write_compdb()
        self.index(compdb=compdb)
        self.assertIn("unchanged 4", self.index(compdb=compdb)[2])

    def test_rewriting_the_compilation_database_re_analyses_everything(self):
        # The arguments a file is compiled with decide what the facts are, so a
        # new database is not a no-op however unchanged the sources are.
        self.index()
        self.assertIn("indexed 4", self.index()[2])

    def test_the_index_records_the_revision_it_describes(self):
        self.index()
        head = subprocess.run(["git", "-C", str(self.root), "rev-parse",
                               "--short", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
        self.assertEqual(read_meta(self.db(), "git_revision"), head)
        self.assertTrue(read_meta(self.db(), "indexed_at"))

    def test_a_stale_index_is_flagged_rather_than_trusted(self):
        self.index()
        self.edit("src/shapes.cpp",
                  "return 3.14159265358979 * radius_ * radius_;",
                  "return 3.14159265358979 * radius_ * radius_ * 2.0;")
        self.commit("change")
        _, out, _ = run(["--db", str(self.db()), "status"])
        self.assertIn("does not describe the current revision", out)

    def test_quiet_prints_the_summary_alone_when_there_is_nothing_to_warn_about(self):
        self.assertEqual(len(self.index()[2].strip().splitlines()), 1)

    def test_a_run_without_a_compilation_database_warns_even_when_quiet(self):
        # `--quiet` drops progress, not warnings: a script asking for silence
        # still has to be told that the index it just built was a guess.
        lines = self.index(compdb=False)[2].strip().splitlines()
        self.assertIn("indexed 4", lines[0])
        self.assertIn("fallback configuration", " ".join(lines[1:]))

    def test_a_compilation_database_makes_the_index_exact(self):
        self.index()
        _, out, _ = run(["--db", str(self.db()), "status"])
        self.assertIn("exact", out)

    def test_only_restricts_what_is_indexed(self):
        status, _, err = self.index("--only", "shapes.cpp")
        self.assertEqual(status, cli.OK)
        self.assertIn("indexed 1", err)

    def test_a_run_that_indexed_nothing_is_a_failure(self):
        # Every translation unit failing is not a partial index with a warning;
        # it is an index that cannot answer anything.
        status, _, err = run(["--db", str(self.db()), "index", str(self.root),
                              "--quiet", "--compdb", str(self.write_compdb()),
                              "--extractor", "/bin/false"])
        self.assertEqual(status, cli.FAILED)
        self.assertIn("failed 4", err)


class TestMcpCommand(CliCase):
    def test_list_tools_prints_the_surface_without_serving(self):
        status, out, _ = self.cli("mcp", "--list-tools")
        self.assertEqual(status, cli.OK)
        self.assertIn("get_callers", {t["name"] for t in json.loads(out)})

    def test_serving_answers_a_handshake_and_a_call(self):
        messages = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18"}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "get_callers",
                                   "arguments":
                                       {"symbol": "mem::Allocator::size"}}}),
        ]) + "\n"
        stdin = sys.stdin
        try:
            sys.stdin = io.StringIO(messages)
            status, out, _ = self.cli("mcp", str(self.root))
        finally:
            sys.stdin = stdin
        self.assertEqual(status, cli.OK)
        replies = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2])
        answer = json.loads(replies[1]["result"]["content"][0]["text"])
        self.assertEqual(answer["callers"][0]["symbol"], "run")

    def serve(self, *argv):
        """Run `mcp` to completion with no client on the other end."""
        stdin = sys.stdin
        try:
            sys.stdin = io.StringIO("")
            return run([*argv])
        finally:
            sys.stdin = stdin

    def test_serving_says_so_on_stderr_before_it_waits(self):
        # stdout is the protocol channel, so stderr is the only place a person
        # running this by hand can see that the server came up at all.  It
        # used to print nothing anywhere, which reads as a hang - and the
        # documentation already promised diagnostics on stderr.
        status, out, err = self.serve("--db", str(self.db), "mcp", str(self.root))
        self.assertEqual(status, cli.OK)
        self.assertEqual(out, "", "a banner on stdout would corrupt the stream")
        self.assertIn("tools, index", err)
        self.assertIn(str(self.db), err)

    def test_a_missing_index_is_announced_rather_than_served_silently(self):
        # The server deliberately starts without an index so a client can be
        # told why its calls fail.  Saying so only in a reply to a call that
        # may never come is the same failure as saying nothing.
        empty = self.root / "unindexed"
        empty.mkdir()
        status, _, err = self.serve("mcp", str(empty))
        self.assertEqual(status, cli.OK)
        self.assertIn("there is no index", err)


@unittest.skipIf(EXTRACTOR is None, "astroclang-index has not been built")
class TestInARealProcess(RepoCase):
    """The whole thing through a process, which is how it will be used."""

    def db(self) -> str:
        return str(Path(self._tmp.name) / "cli" / "index.db")

    def call(self, *argv):
        env = dict(os.environ, PYTHONPATH=str(PYTHON_DIR),
                   ASTROCLANG_INDEX=str(EXTRACTOR))
        assert env["ASTROCLANG_INDEX"]
        return subprocess.run([sys.executable, "-m", "astroclang", *argv],
                              cwd=str(PYTHON_DIR), capture_output=True, text=True,
                              env=env)

    def test_index_then_ask_in_a_separate_process(self):
        first = self.call("--db", self.db(), "index", str(self.root), "--quiet")
        self.assertEqual(first.returncode, cli.OK, first.stderr)
        second = self.call("--db", self.db(), "callers", "geo::Circle::area")
        self.assertEqual(second.returncode, cli.OK, second.stderr)
        self.assertIn("app::measure_circle", second.stdout)

    def test_a_failure_leaves_a_status_a_shell_can_branch_on(self):
        self.call("--db", self.db(), "index", str(self.root), "--quiet")
        bad = self.call("--db", self.db(), "symbol", "area")
        self.assertEqual(bad.returncode, cli.FAILED)
        self.assertEqual(bad.stdout, "")
        self.assertIn("more than one", bad.stderr)

    def test_the_module_and_the_console_script_are_the_same_command(self):
        # `python -m astroclang` and the `astroclang` script both land
        # in `run`; a difference between them would be a difference in
        # behaviour depending on how the tool was installed.
        import astroclang.__main__ as entry
        self.assertIs(entry.run, cli.run)


if __name__ == "__main__":
    unittest.main()
