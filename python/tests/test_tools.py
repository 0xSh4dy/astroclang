"""The tool surface: argument handling, answer shape, and the cuts.

What is tested here is not whether the index can resolve a name - the query
tests own that - but whether the answers an agent receives are shaped so that
they can be acted on: a list that was cut says so, a name that denotes several
symbols comes back as several, and a question that cannot be asked fails in a
way that says what to do instead.
"""

import json
import unittest

from cpp_code_graph import indexer, tools
from cpp_code_graph.facts import FileFact, TranslationUnit
from cpp_code_graph.tools import ToolError

from tests.test_changes import RepoCase
from tests.test_query import Fixture, edge, sym
from tests.test_semantics import Corpus

try:
    EXTRACTOR = indexer.find_extractor()
except indexer.ExtractorNotFound:
    EXTRACTOR = None


class ToolCase(Fixture):
    def call(self, name, **arguments):
        return tools.call(self.q, name, arguments)

    def fails(self, name, **arguments):
        with self.assertRaises(ToolError) as caught:
            tools.call(self.q, name, arguments)
        return caught.exception


class TestFindSymbol(ToolCase):
    def test_a_unique_name_resolves_to_its_location(self):
        out = self.call("find_symbol", reference="mem::Helper::grow")
        self.assertEqual(out["match_count"], 1)
        self.assertEqual(out["matches"][0]["location"], "src/pool.cpp:1")

    def test_a_name_that_denotes_several_is_returned_as_several(self):
        # Four: one on Allocator, one on Fast, and two overloads on Buffer.
        out = self.call("find_symbol", reference="allocate")
        self.assertEqual(out["match_count"], 4)
        self.assertIn("location", out["matches"][0])
        self.assertIn("narrow", out["note"])

    def test_one_match_carries_no_ambiguity_note(self):
        out = self.call("find_symbol", reference="mem::Fast")
        self.assertEqual([m["symbol"] for m in out["matches"]], ["mem::Fast"])
        self.assertNotIn("note", out)

    def test_nothing_matching_is_an_answer_not_a_failure(self):
        # "Nothing is called that" is a fact about the code.  Raising would
        # teach an agent to treat a real answer as a broken call.
        out = self.call("find_symbol", reference="totally_absent_name")
        self.assertEqual(out["matches"], [])
        self.assertIn("search_symbols", out["note"])

    def test_a_path_narrows_an_ambiguous_name(self):
        out = self.call("find_symbol", reference="allocate", path="src/other.cpp")
        self.assertEqual({m["symbol"] for m in out["matches"]},
                         {"Buffer::allocate"})

    def test_a_missing_argument_is_refused(self):
        self.assertIn("reference", str(self.fails("find_symbol")))

    def test_a_limit_that_is_not_a_number_is_refused(self):
        self.assertIn("whole number",
                      str(self.fails("find_symbol", reference="allocate",
                                     limit="lots")))


class TestDescribeSymbol(ToolCase):
    def test_detail_includes_where_it_is_and_what_it_is(self):
        out = self.call("get_symbol", symbol="mem::Allocator::size")
        self.assertEqual(out["kind"], "method")
        self.assertEqual(out["signature"], "()")
        self.assertEqual(out["location"], "include/iface.h:7")
        # The namespace is not a symbol in this fixture, so the chain
        # stops at the class - which is where a parent link exists.
        self.assertEqual(out["within"], ["mem::Allocator"])

    def test_counts_are_given_rather_than_the_lists_themselves(self):
        out = self.call("get_symbol", symbol="mem::Allocator::size")
        self.assertEqual(out["callers"], 1)
        self.assertNotIn("caller_list", out)

    def test_the_usr_is_returned_for_a_caller_that_wants_one(self):
        out = self.call("get_symbol", symbol="mem::Fast::allocate")
        self.assertTrue(out["usr"])

    def test_a_name_that_denotes_several_is_refused_with_the_candidates(self):
        # Refused rather than guessed: `allocate` is four symbols, and
        # answering about whichever one sorted first would be worse than
        # answering nothing.
        failure = self.fails("get_symbol", symbol="allocate")
        self.assertEqual(len(failure.payload["candidates"]), 4)
        self.assertIn("more than one", failure.payload["error"])

    def test_a_precise_location_is_accepted_in_place_of_a_name(self):
        out = self.call("get_symbol", symbol="include/iface.h:7")
        self.assertEqual(out["symbol"], "mem::Allocator::size")

    def test_a_function_is_described_as_a_function(self):
        out = self.call("get_function", symbol="mem::Fast::allocate")
        self.assertEqual(out["kind"], "method")
        self.assertEqual(out["signature"], "(unsigned long)")
        self.assertEqual(out["member_of"], "mem::Fast")
        self.assertTrue(out["properties"]["override"])

    def test_asking_for_a_class_as_a_function_says_what_it_is(self):
        failure = self.fails("get_function", symbol="mem::Fast")
        self.assertIn("not a function", failure.payload["error"])
        self.assertIn("get_class", failure.payload["hint"])

    def test_a_class_lists_its_family_and_its_members(self):
        out = self.call("get_class", symbol="mem::Allocator")
        # The empty list is the answer here, so it is stated rather than left
        # out: `Allocator` has no bases, and a missing key would leave the
        # reader unable to tell that from a question that was not asked.
        self.assertEqual(out["bases"], [])
        self.assertEqual([d["symbol"] for d in out["derived"]], ["mem::Fast"])
        self.assertEqual([m["symbol"] for m in out["methods"]],
                         ["mem::Allocator::allocate", "mem::Allocator::size"])

    def test_asking_for_a_function_as_a_class_says_what_it_is(self):
        failure = self.fails("get_class", symbol="mem::Fast::allocate")
        self.assertIn("not a type", failure.payload["error"])


class TestRelationships(ToolCase):
    def test_callers_come_with_the_site_of_the_call(self):
        out = self.call("get_callers", symbol="mem::Allocator::size")
        self.assertEqual([c["symbol"] for c in out["callers"]], ["run"])
        self.assertEqual(out["callers"][0]["call_site"], "src/use.cpp:4")
        self.assertEqual(out["location"], "include/iface.h:7")

    def test_callees_are_the_resolved_targets(self):
        out = self.call("get_callees", symbol="mem::Fast::allocate")
        self.assertEqual([c["symbol"] for c in out["callees"]],
                         ["mem::Helper::grow"])

    def test_a_function_that_calls_nothing_says_so(self):
        out = self.call("get_callees", symbol="mem::Helper::grow")
        self.assertEqual(out["callees"], [])
        self.assertIn("calls nothing", out["note"])

    def test_references_are_kept_apart_from_calls(self):
        out = self.call("get_references", symbol="mem::Fast::allocate")
        self.assertEqual([r["symbol"] for r in out["references"]], ["run"])
        self.assertIn("not calls", out["note"])

    def test_inheritance_reaches_the_whole_family(self):
        out = self.call("get_inheritance", symbol="mem::Allocator")
        self.assertEqual([d["symbol"] for d in out["derived"]], ["mem::Fast"])

    def test_overrides_hang_off_the_method_not_the_class(self):
        out = self.call("get_inheritance", symbol="mem::Allocator::allocate")
        self.assertEqual([o["symbol"] for o in out["overrides"]],
                         ["mem::Fast::allocate"])

    def test_dependencies_are_grouped_by_what_the_relationship_is(self):
        out = self.call("get_symbol_dependencies", symbol="mem::Fast::allocate")
        self.assertEqual([c["symbol"] for c in out["calls"]],
                         ["mem::Helper::grow"])

    def test_search_narrows_by_kind(self):
        out = self.call("search_symbols", query="Allocator", kind="class")
        self.assertEqual([m["symbol"] for m in out["matches"]],
                         ["mem::Allocator"])

    def test_search_reports_that_a_full_page_may_have_more(self):
        out = self.call("search_symbols", query="allocate", limit=2)
        self.assertEqual(len(out["matches"]), 2)
        self.assertTrue(out["more"])


class TestAnEmptyAnswerSaysWhatItMeans(ToolCase):
    def test_a_macro_with_no_references_says_it_is_not_tracked(self):
        # An empty list is the same shape whether the index looked and found
        # nothing or never looked.  For a macro it never looks, and an agent
        # reading "no references" as "safe to delete" would remove live code.
        self.store.ingest(TranslationUnit(
            path=str(self.root / "src" / "mac.cpp"), complete=True,
            files=[FileFact(0, str(self.root / "src" / "mac.cpp"), False)],
            symbols=[sym("m:src/mac.cpp:4:WIDGET_MAX", "macro", "WIDGET_MAX",
                         "WIDGET_MAX", file=0, line=4),
                     sym("c:@F@never_called", "function", "never_called",
                         "never_called", "()", file=0, line=9)],
        ))
        self.store.rebuild_symbols()
        for tool in ("get_references", "get_callers"):
            out = self.call(tool, symbol="WIDGET_MAX")
            self.assertEqual(out["references" if tool == "get_references"
                                    else "callers"], [])
            self.assertIn("macro", out["note"])
            self.assertIn("not mean it is unused", out["note"])

    def test_an_ordinary_symbol_keeps_the_ordinary_note(self):
        # The special case is about macros, not about empty lists: a function
        # nothing calls is genuinely uncalled, and can be deleted.
        self.store.ingest(TranslationUnit(
            path=str(self.root / "src" / "quiet.cpp"), complete=True,
            files=[FileFact(0, str(self.root / "src" / "quiet.cpp"), False)],
            symbols=[sym("c:@F@never_called", "function", "never_called",
                         "never_called", "()", file=0, line=9)],
        ))
        self.store.rebuild_symbols()
        out = self.call("get_callers", symbol="never_called")
        self.assertEqual(out["callers"], [])
        self.assertEqual(out["note"], "nothing in the index calls this")


class TestACutListSaysSo(ToolCase):
    """The promise that a truncated answer carries its true length.

    This is the one place where the code was quietly wrong rather than merely
    incomplete, and it was wrong in a way no test could see: a page fetched
    with `LIMIT n` can never hold n+1 rows, so a handler comparing the result
    against the limit concludes "not truncated" every time.  The fix is to
    fetch one extra and count the rest exactly; these tests hold that down, for
    each tool that pages a list.
    """

    def setUp(self):
        super().setUp()
        # A symbol with more callers than any limit used below.  Twelve
        # distinct functions call `mem::Helper::grow`, which the fixture
        # otherwise has called exactly once.
        target = "c:@N@mem@S@Helper@F@grow#l#"
        callers = [sym(f"c:@F@caller{i}", "function", f"caller{i}",
                       f"caller{i}", "()", file=0, line=20 + i)
                   for i in range(12)]
        self.store.ingest(TranslationUnit(
            path=str(self.root / "src" / "many.cpp"), complete=True,
            files=[FileFact(0, str(self.root / "src" / "many.cpp"), False)],
            symbols=callers,
            edges=[edge("calls", c.usr, target, file=0, line=20 + i)
                   for i, c in enumerate(callers)],
        ))
        self.store.rebuild_symbols()

    def test_callers_are_cut_and_the_answer_carries_the_true_total(self):
        out = self.call("get_callers", symbol="mem::Helper::grow", limit=3)
        self.assertEqual(len(out["callers"]), 3)
        # Thirteen: the twelve above plus the fixture's own Fast::allocate.
        self.assertEqual(out["caller_count"], 13)

    def test_an_uncut_list_carries_no_count_to_misread(self):
        out = self.call("get_callers", symbol="mem::Helper::grow", limit=50)
        self.assertEqual(len(out["callers"]), 13)
        self.assertNotIn("caller_count", out)

    def test_callees_carry_their_total_too(self):
        hub = "c:@F@hub"
        callees = [sym(f"c:@F@callee{i}", "function", f"callee{i}",
                       f"callee{i}", "()", file=0, line=60 + i)
                   for i in range(9)]
        self.store.ingest(TranslationUnit(
            path=str(self.root / "src" / "hub.cpp"), complete=True,
            files=[FileFact(0, str(self.root / "src" / "hub.cpp"), False)],
            symbols=[sym(hub, "function", "hub", "hub", "()", file=0, line=50)]
            + callees,
            edges=[edge("calls", hub, c.usr, file=0, line=60 + i)
                   for i, c in enumerate(callees)],
        ))
        self.store.rebuild_symbols()
        out = self.call("get_callees", symbol="hub", limit=4)
        self.assertEqual(len(out["callees"]), 4)
        self.assertEqual(out["callee_count"], 9)

    def test_a_cut_dependency_group_still_counts_the_whole(self):
        # `dependency_count` summed the pages, so a cut shrank it by exactly
        # what had been withheld: the one number a reader would trust most was
        # the one number that lied.  A cut group carries its true total, and
        # the sum has to use it.
        #
        # Two groups, so the sum has something to add up: eight calls, cut to
        # three, and one field type that is not cut.
        hub = "c:@F@twelve"
        callees = [sym(f"c:@F@leaf{i}", "function", f"leaf{i}", f"leaf{i}",
                       "()", file=0, line=80 + i) for i in range(8)]
        self.store.ingest(TranslationUnit(
            path=str(self.root / "src" / "twelve.cpp"), complete=True,
            files=[FileFact(0, str(self.root / "src" / "twelve.cpp"), False)],
            symbols=[sym(hub, "function", "twelve", "twelve", "()",
                         file=0, line=70)] + callees,
            edges=[edge("calls", hub, c.usr, file=0, line=80 + i)
                   for i, c in enumerate(callees)]
            + [edge("returns", hub, "c:@N@mem@S@Allocator", file=0, line=70)],
        ))
        self.store.rebuild_symbols()

        out = self.call("get_symbol_dependencies", symbol="twelve", limit=3)
        self.assertEqual(len(out["calls"]), 3)
        self.assertEqual(out["calls_count"], 8)
        # Eight, not the three on the page, plus the one return type.
        self.assertEqual(out["dependency_count"], 9)

    def test_a_file_lists_a_page_and_counts_the_whole(self):
        # `symbol_count` used to be the page length, one more than asked for.
        out = self.call("get_file_symbols", path="include/iface.h", limit=2)
        self.assertEqual(len(out["symbols"]), 2)
        self.assertGreater(out["symbol_count"], 3)
        self.assertEqual(out["symbol_count"], self.q.count_file_symbols(
            "include/iface.h"))


class TestFiles(ToolCase):
    def test_a_file_reports_its_language_and_its_translation_units(self):
        out = self.call("get_file", path="src/use.cpp")
        self.assertEqual(out["language"], "c++")
        self.assertEqual(out["translation_units"], ["src/use.cpp"])

    def test_a_header_takes_its_language_from_who_includes_it(self):
        # `.h` is C or C++, and the file's own name cannot say which.  The
        # translation units that include it can.
        out = self.call("get_file", path="include/iface.h")
        self.assertEqual(out["language"], "c++")

    def test_a_file_the_index_has_not_seen_is_an_error_not_an_empty_file(self):
        failure = self.fails("get_file", path="src/imaginary.cpp")
        self.assertIn("has not seen", failure.payload["error"])
        self.assertIn("hint", failure.payload)

    def test_file_symbols_are_listed(self):
        # Both overloads, told apart by their signatures.
        out = self.call("get_file_symbols", path="src/other.cpp")
        self.assertEqual({s["symbol"] for s in out["symbols"]},
                         {"Buffer::allocate"})
        self.assertEqual({s["signature"] for s in out["symbols"]},
                         {"(unsigned long)", "(double)"})

    def test_includes_are_listed(self):
        out = self.call("get_includes", path="src/use.cpp")
        self.assertEqual([i["file"] for i in out["includes"]],
                         ["include/iface.h"])

    def test_includes_can_be_followed_transitively(self):
        out = self.call("get_file_dependencies", path="src/use.cpp")
        self.assertIn("include/iface.h", out["includes_transitively"])

    def test_a_long_list_is_cut_and_carries_its_length(self):
        out = self.call("get_file_symbols", path="include/iface.h", limit=2)
        self.assertEqual(len(out["symbols"]), 2)
        self.assertGreater(out["symbol_count"], 2)


class TestSourceContextFailures(ToolCase):
    """The refusals, which need no source on disk to be worth asserting."""

    def test_a_name_that_denotes_several_is_refused_with_the_candidates(self):
        failure = self.fails("get_source_context", symbol="allocate")
        self.assertEqual(len(failure.payload["candidates"]), 4)

    def test_a_symbol_the_index_does_not_know_is_refused(self):
        failure = self.fails("get_source_context", symbol="nothing_here")
        self.assertIn("no symbol matching", failure.payload["error"])

    def test_a_file_that_is_no_longer_there_is_reported_as_such(self):
        # The synthetic index names files that were never written, which is
        # exactly the state of an index whose tree has moved.  Saying so beats
        # quoting whatever now occupies those lines.
        failure = self.fails("get_source_context", symbol="mem::Fast::allocate")
        self.assertIn("re-index", failure.payload["error"])


class TestImpact(ToolCase):
    def test_the_three_degrees_are_kept_apart(self):
        out = self.call("get_impact_analysis",
                        symbol="mem::Allocator::allocate")
        self.assertEqual([d["symbol"] for d in out["direct"]], [])
        self.assertEqual([p["symbol"] for p in out["possible"]],
                         ["mem::Fast::allocate"])
        self.assertIn("may reach it", out["possible"][0]["reason"])

    def test_every_entry_says_why(self):
        out = self.call("get_impact_analysis", symbol="mem::Allocator::size")
        for bucket in ("direct", "indirect", "possible"):
            for entry in out.get(bucket, []):
                self.assertTrue(entry.get("reason"), entry)

    def test_indirect_reaches_the_caller_of_a_caller(self):
        # The frontier used to be read back off the direct entries, from a
        # field those queries never asked for.  It was therefore always empty,
        # and this bucket was empty with it - which reads exactly like a symbol
        # that genuinely has no indirect callers, so nothing failed.
        self._chain()
        out = self.call("get_impact_analysis", symbol="deep_c", depth=3)
        self.assertEqual([e["symbol"] for e in out["direct"]], ["deep_b"])
        self.assertEqual([e["symbol"] for e in out["indirect"]], ["deep_a"])
        self.assertEqual(out["indirect"][0]["hops"], 2)
        # The USR is how the traversal walks; it is not something a reader
        # needs, and every entry already carries a resolvable location.
        for bucket in ("direct", "indirect", "possible"):
            for entry in out[bucket]:
                self.assertNotIn("usr", entry)

    def test_a_type_is_reached_through_what_names_it(self):
        # A class has no callers.  Before type edges were walked here, asking
        # about a class returned three empty buckets for every class in the
        # project, whatever its documentation said.
        self._chain()
        self.store.ingest(TranslationUnit(
            path=str(self.root / "src" / "named.cpp"), complete=True,
            files=[FileFact(0, str(self.root / "src" / "named.cpp"), False)],
            symbols=[sym("c:@S@Widget", "class", "Widget", "Widget", file=0,
                         line=60),
                     sym("c:@F@holds", "function", "holds", "holds",
                         "(Widget &)", file=0, line=61)],
            edges=[edge("param_type", "c:@F@holds", "c:@S@Widget",
                        file=0, line=61)],
        ))
        self.store.rebuild_symbols()
        out = self.call("get_impact_analysis", symbol="Widget")
        self.assertEqual([e["symbol"] for e in out["direct"]], ["holds"])
        self.assertEqual(out["direct"][0]["reason"],
                         "takes this type as a parameter")

    def _chain(self):
        """deep_a calls deep_b calls deep_c, in one translation unit."""
        names = ("deep_a", "deep_b", "deep_c")
        self.store.ingest(TranslationUnit(
            path=str(self.root / "src" / "chain.cpp"), complete=True,
            files=[FileFact(0, str(self.root / "src" / "chain.cpp"), False)],
            symbols=[sym(f"c:@F@{n}", "function", n, n, "()", file=0,
                         line=40 + i) for i, n in enumerate(names)],
            edges=[edge("calls", "c:@F@deep_b", "c:@F@deep_c", file=0, line=41),
                   edge("calls", "c:@F@deep_a", "c:@F@deep_b", file=0, line=40)],
        ))
        self.store.rebuild_symbols()

    def test_the_answer_explains_what_the_buckets_mean(self):
        out = self.call("get_impact_analysis", symbol="mem::Allocator::size")
        self.assertIn("direct", out["note"])
        self.assertIn("possible", out["note"])


class TestIndexStatus(ToolCase):
    def test_it_reports_what_is_indexed_and_how_trustworthy_it_is(self):
        out = self.call("get_index_status")
        self.assertTrue(out["symbols"] > 0)
        self.assertEqual(out["root"], str(self.root))
        # This index was built by hand, so the translation units record no
        # configuration and the honest answer is that it is not known.
        self.assertEqual(out["accuracy"], "unknown")
        self.assertEqual(out["compiler_arguments"], {"unknown": 3})

    def test_an_index_built_from_a_compilation_database_says_exact(self):
        from cpp_code_graph.facts import FileFact, TranslationUnit

        path = self.root / "src" / "exact.cpp"
        self.store.ingest(TranslationUnit(
            path=str(path), complete=True,
            config_source="compile_commands.json",
            files=[FileFact(0, str(path), False)]))
        out = self.call("get_index_status")
        self.assertEqual(out["accuracy"], "exact")
        self.assertNotIn("warnings", out)

    def test_a_fallback_configuration_is_reported_as_degraded(self):
        # The extractor writes `fallback` when it had no compilation database,
        # and an agent has to be able to tell a semantic index from a guess.
        from cpp_code_graph.facts import TranslationUnit
        from cpp_code_graph.facts import FileFact

        path = self.root / "src" / "guess.cpp"
        self.store.ingest(TranslationUnit(
            path=str(path), complete=True, config_source="fallback",
            degraded=True, files=[FileFact(0, str(path), False)],
        ))
        out = self.call("get_index_status")
        self.assertEqual(out["accuracy"], "degraded")
        self.assertTrue(any("compilation database" in w
                            for w in out["warnings"]))


@unittest.skipIf(EXTRACTOR is None, "cg-index has not been built")
class TestAgainstRealExtraction(Corpus):
    """The tools over an index built by the extractor from real files.

    The synthetic fixture above is right for argument handling, but the source
    region is one answer that only means anything against a file on disk.
    """

    def call(self, name, **arguments):
        return tools.call(self.q, name, arguments)

    def test_the_index_reports_itself_as_exact(self):
        out = self.call("get_index_status")
        self.assertEqual(out["accuracy"], "exact")
        self.assertNotIn("warnings", out)

    def test_a_source_region_comes_back_with_line_numbers(self):
        out = self.call("get_source_context", symbol="geo::Circle::area",
                        context_lines=2)
        lines = out["source"].splitlines()
        self.assertTrue(all(": " in line for line in lines), lines)
        numbers = [int(line.split(":", 1)[0]) for line in lines]
        self.assertEqual(numbers, sorted(numbers))
        self.assertEqual(numbers[-1] - numbers[0] + 1, len(numbers))

    def test_a_c_source_file_says_it_is_c(self):
        out = self.call("get_file", path="src/c_lib.c")
        self.assertEqual(out["language"], "c")

    def test_a_header_built_into_both_languages_says_both(self):
        # The corpus's C library and C++ both reach `c_lib.h`; a header is
        # whichever its includers are, and both is the honest answer.
        out = self.call("get_file", path="include/c_lib.h")
        self.assertIn("c", out["language"])


@unittest.skipIf(EXTRACTOR is None, "cg-index has not been built")
class TestChangeTools(RepoCase):
    def test_a_changed_body_reaches_the_tool_with_its_scope(self):
        self.edit("src/shapes.cpp",
                  "return 3.14159265358979 * radius_ * radius_;",
                  "return 3.14159265358979 * radius_ * radius_ * 2.0;")
        query = self.build_index()
        out = tools.call(query, "get_changed_symbols", {"revision": "HEAD"})
        entry = next(s for s in out["changed_symbols"]
                     if s["symbol"] == "geo::Circle::area")
        self.assertEqual(entry["within"], ["geo::Circle", "geo"])

    def test_impact_can_be_asked_for_in_the_same_call(self):
        self.edit("src/shapes.cpp",
                  "return 3.14159265358979 * radius_ * radius_;",
                  "return 3.14159265358979 * radius_ * radius_ * 2.0;")
        query = self.build_index()
        out = tools.call(query, "get_changed_symbols",
                         {"revision": "HEAD", "include_impact": True})
        self.assertIn("app::measure_circle",
                      {d["symbol"] for d in out["affected"]["direct"]})

    def test_a_revision_git_cannot_resolve_is_a_failure_not_a_crash(self):
        query = self.build_index()
        with self.assertRaises(ToolError) as caught:
            tools.call(query, "get_changed_symbols",
                       {"revision": "no-such-revision"})
        self.assertIn("cannot read a diff", caught.exception.payload["error"])


class TestRegistry(unittest.TestCase):
    def test_every_tool_is_described(self):
        described = tools.describe()
        self.assertEqual(len(described), len(tools.TOOLS))
        for entry in described:
            self.assertTrue(entry["description"])
            self.assertEqual(entry["inputSchema"]["type"], "object")
            self.assertTrue(entry["annotations"]["readOnlyHint"])
            json.dumps(entry)  # the description has to survive the wire

    def test_the_names_are_unique_and_the_expected_set_is_there(self):
        names = [t.name for t in tools.TOOLS]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue({
            "find_symbol", "get_symbol", "get_function", "get_class",
            "get_file", "get_file_symbols", "get_callers", "get_callees",
            "get_references", "get_inheritance", "get_includes",
            "search_symbols", "get_symbol_dependencies", "get_file_dependencies",
            "get_impact_analysis", "get_changed_symbols", "get_source_context",
        } <= set(names), names)

    def test_no_tool_returns_source_except_the_one_that_says_so(self):
        # The whole point is that an agent does not read files to find out
        # where things are.  A tool that grew a `source` field would undo it.
        allowed = {"get_source_context"}
        for tool in tools.TOOLS:
            if tool.name in allowed:
                continue
            self.assertNotIn("source", tool.schema["properties"], tool.name)

    def test_an_unknown_tool_lists_the_known_ones(self):
        with self.assertRaises(ToolError) as caught:
            tools.call(None, "get_everything")
        self.assertIn("find_symbol", caught.exception.payload["tools"])

    def test_arguments_that_are_not_an_object_are_refused(self):
        with self.assertRaises(ToolError):
            tools.call(None, "get_index_status", ["nope"])


if __name__ == "__main__":
    unittest.main()
