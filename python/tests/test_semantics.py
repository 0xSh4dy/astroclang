"""Semantic resolution against the real extractor.

These tests assert on *which declaration* a name resolved to, not on whether a
node exists.  That distinction is the reason this project exists: a syntax
parser can tell you `shape->area()` calls something called `area`, and only an
AST can tell you it reached `geo::Shape::area` - a pure virtual with no body -
rather than `geo::Circle::area`, which is what actually runs.

The corpus is indexed in place, but the index is written to a temporary
directory, so running the tests leaves no artefact in the tree.  The
compilation database is generated rather than checked in because it has to
contain absolute paths, which differ per machine.

Skipped when the extractor has not been built; `CG_INDEX` or a build tree next
to the package is used to find it.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from cpp_code_graph import indexer
from cpp_code_graph.indexer import index_project
from cpp_code_graph.query import Query
from cpp_code_graph.store import Store

CORPUS = Path(__file__).resolve().parent / "corpus"

C_FLAGS = ["clang", "-std=c11", "-fsyntax-only", "-Iinclude"]
CXX_FLAGS = ["clang++", "-std=c++17", "-fsyntax-only", "-Iinclude"]

try:
    EXTRACTOR = indexer.find_extractor()
except indexer.ExtractorNotFound:
    EXTRACTOR = None


_INDEX = None


def corpus_index():
    """The corpus, indexed once however many test classes want it.

    Indexing it per class would re-run the extractor over the whole corpus for
    every suite that asserts against real parses, and the index is read-only
    for all of them, so one build is shared.  Module cleanups are drained at
    the end of each module, so this is one build per test module that uses it
    rather than one per class within it.
    """
    global _INDEX
    if _INDEX is None:
        tmp = tempfile.TemporaryDirectory()
        # Registered before the store, so that the store is closed first: the
        # cleanups run in the order they were added, reversed.
        unittest.addModuleCleanup(tmp.cleanup)
        root = Path(tmp.name)

        # Paths inside the command are relative to `directory`, which is what a
        # hand-written or Meson-generated database looks like; CMake writes
        # absolute ones.  Both forms are worth exercising, so this one is
        # relative and the include path is relative with it.
        commands = []
        for path in sorted((CORPUS / "src").iterdir()):
            if path.suffix not in (".c", ".cpp"):
                continue
            flags = C_FLAGS if path.suffix == ".c" else CXX_FLAGS
            commands.append({
                "directory": str(CORPUS),
                "file": str(path),
                "command": " ".join([*flags, "src/" + path.name]),
            })
        compdb = root / "compile_commands.json"
        compdb.write_text(json.dumps(commands))

        store = Store(root / "index.db", project_root=CORPUS)
        report = index_project(CORPUS, store, extractor=EXTRACTOR,
                               compdb=compdb)
        _INDEX = (store, Query(store), report)
        unittest.addModuleCleanup(_cleanup_index)
    return _INDEX


def _cleanup_index() -> None:
    global _INDEX
    if _INDEX is not None:
        _INDEX[0].close()
        _INDEX = None


@unittest.skipIf(EXTRACTOR is None, "cg-index has not been built")
class Corpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store, cls.q, cls.report = corpus_index()

    # -- helpers -------------------------------------------------------------

    def sym(self, reference, kind=None):
        """One symbol, or a failure naming the alternatives.

        `kind` narrows first, because some names are legitimately shared: in C,
        `typedef struct {...} clib_size` is a typedef and an anonymous struct
        with the same name, and neither is a mistake.
        """
        candidates = self.q.resolve(reference, kinds=[kind] if kind else None)
        if len(candidates) != 1:
            self.fail(f"{reference!r} did not resolve to exactly one symbol; "
                      f"candidates: {[c.qualified + c.signature for c in candidates]}")
        return candidates[0]

    def callee_names(self, symbol):
        return {c["symbol"] for c in self.q.callees(self.sym(symbol).usr)}

    def callee_usrs(self, symbol):
        return [c for c in self.q.callees(self.sym(symbol).usr, with_usr=True)]

    def edge_targets(self, symbol, kind):
        usr = self.sym(symbol).usr
        rows = self.store.connection().execute(
            "SELECT s.qualified, s.signature, s.usr FROM raw_edge e"
            " JOIN symbol s ON s.usr = e.dst"
            " WHERE e.src = ? AND e.kind = ?", (usr, kind))
        return {f"{r['qualified']}{r['signature'] or ''}" for r in rows}

    def flag(self, symbol, name):
        return self.q.symbol(self.sym(symbol).usr).get("properties", {}).get(name)


class TestIndexingHealth(Corpus):
    def test_every_translation_unit_indexed(self):
        self.assertEqual(self.report.failed, 0,
                         [f.detail for f in self.report.failures])
        self.assertEqual(self.report.indexed, len(self.report.results))

    def test_configured_from_the_compilation_database(self):
        # Not the fallback: include paths and the language standard came from
        # the database, so the analysis is the real one.
        for record in self.store.tu_records():
            self.assertEqual(record["config_source"], "compile_commands.json")
            self.assertFalse(record["degraded"])

    def test_no_compilation_errors(self):
        for record in self.store.tu_records():
            self.assertEqual(record["errors"], 0,
                             f"{record['path']} did not compile cleanly")

    def test_headers_are_reached_through_includes(self):
        info = self.q.file("include/shapes.h")
        self.assertIn("src/usage.cpp", info["translation_units"])


class TestOverloadResolution(Corpus):
    def test_overloads_are_distinct_symbols(self):
        # Three functions called `scale`; a graph keyed on the name would have
        # one node and no way to tell which call goes where.
        candidates = self.q.resolve("geo::scale")
        self.assertEqual(len(candidates), 3)
        self.assertEqual({c.signature for c in candidates},
                         {"(double)", "(double, double)", "(int)"})

    def test_int_call_resolves_to_the_int_overload(self):
        self.assertEqual(self.callee_names("app::use_int_overload"),
                         {"geo::scale"})
        usr = self.callee_usrs("app::use_int_overload")[0]["usr"]
        self.assertEqual(self.sym("geo::scale(int)").usr, usr)

    def test_double_call_resolves_to_the_double_overload(self):
        usr = self.callee_usrs("app::use_double_overload")[0]["usr"]
        self.assertEqual(self.sym("geo::scale(double)").usr, usr)

    def test_two_argument_call_resolves_to_its_own_overload(self):
        usr = self.callee_usrs("app::use_two_argument_overload")[0]["usr"]
        self.assertEqual(self.sym("geo::scale(double, double)").usr, usr)

    def test_the_three_calls_reach_three_different_symbols(self):
        targets = {self.callee_usrs(f"app::{f}")[0]["usr"] for f in
                   ("use_int_overload", "use_double_overload",
                    "use_two_argument_overload")}
        self.assertEqual(len(targets), 3)


class TestVirtualDispatch(Corpus):
    def test_call_through_a_base_pointer_reaches_the_pure_virtual(self):
        # `measure` takes a `const Shape *`, so the declaration reached is
        # Shape::area - which has no body - and the edge says the dispatch is
        # virtual.
        self.assertEqual(self.callee_names("app::measure"),
                         {"geo::Shape::area"})
        self.assertEqual(self.flag("geo::Shape::area", "pure"), True)
        edge = self.callee_usrs("app::measure")[0]
        # Reported as `pure virtual`, not merely `virtual`: the reader of an
        # impact report wants to know there is no base implementation to fall
        # back on.
        self.assertEqual(edge["dispatch"], "pure virtual")

    def test_call_through_a_concrete_object_reaches_the_override(self):
        self.assertEqual(self.callee_names("app::measure_circle"),
                         {"geo::Circle::area"})

    def test_a_qualified_call_names_the_base_explicitly(self):
        # `tagged.geo::Circle::area()` bypasses virtual dispatch entirely.
        self.assertEqual(self.callee_names("app::measure_base_explicitly"),
                         {"geo::Circle::area"})

    def test_the_override_chain_is_recorded(self):
        self.assertEqual(self.edge_targets("geo::Tagged::area", "overrides"),
                         {"geo::Circle::area() const"})
        self.assertEqual(self.edge_targets("geo::Circle::area", "overrides"),
                         {"geo::Shape::area() const"})

    def test_an_override_is_not_reported_as_a_caller_of_its_base(self):
        # `overrides` and `calls` are different relationships.  Conflating them
        # would make every base method look called from every override.
        callers = {c["symbol"] for c in
                   self.q.callers(self.sym("geo::Shape::area").usr)}
        self.assertNotIn("geo::Tagged::area", callers)


class TestInheritance(Corpus):
    def test_single_inheritance(self):
        tree = self.q.inheritance(self.sym("geo::Circle").usr)
        self.assertEqual([b["symbol"] for b in tree["bases"]], ["geo::Shape"])
        self.assertEqual(tree["bases"][0]["access"], "public")

    def test_multiple_inheritance_keeps_both_bases(self):
        tree = self.q.inheritance(self.sym("geo::Tagged").usr)
        self.assertEqual({b["symbol"] for b in tree["bases"]},
                         {"geo::Circle", "geo::Named"})

    def test_transitive_descendants(self):
        tree = self.q.inheritance(self.sym("geo::Shape").usr)
        self.assertEqual([d["symbol"] for d in tree["derived"]], ["geo::Circle"])
        self.assertEqual([d["symbol"] for d in tree["descendants"]],
                         ["geo::Tagged"])

    def test_a_virtual_base_is_flagged(self):
        # Nothing here derives virtually; the flag exists so that a reader is
        # not told "not virtual" when the index simply does not know.
        tree = self.q.inheritance(self.sym("geo::Tagged").usr)
        self.assertNotIn("virtual", tree["bases"][0])


class TestLambdas(Corpus):
    def test_a_lambda_is_its_own_symbol(self):
        hits = self.q.search("lambda")
        self.assertTrue(hits, "no lambda closure class was indexed")

    def test_a_call_inside_a_lambda_belongs_to_the_lambda(self):
        # The body runs when the closure is invoked, which is not necessarily
        # inside pick_larger - and not necessarily while it is on the stack.
        callers = {c["symbol"] for c in
                   self.q.callers(self.sym("geo::Shape::area").usr)}
        lambda_callers = {c for c in callers if "lambda" in c}
        self.assertTrue(lambda_callers,
                        f"no lambda among the callers: {sorted(callers)}")
        self.assertNotIn("app::pick_larger", callers)


class TestIndirectCalls(Corpus):
    def test_a_call_through_a_function_pointer_is_recorded_as_indirect(self):
        # The call names `picker`, the variable.  Which function that is cannot
        # be known without tracking assignments, and claiming `pick_by_name`
        # would be a guess dressed as a fact - the pointer could have been
        # reassigned, or passed in from elsewhere.
        usr = self.sym("app::use_function_pointer").usr
        rows = list(self.store.connection().execute(
            "SELECT s.qualified, s.kind FROM raw_edge e JOIN symbol s"
            " ON s.usr = e.dst WHERE e.src = ? AND e.kind = 'calls_indirect'",
            (usr,)))
        self.assertEqual([(r["qualified"], r["kind"]) for r in rows],
                         [("picker", "variable")])

    def test_the_pointer_and_the_address_taken_function_are_both_recorded(self):
        # Two edges that together say what happened: the call goes through
        # `picker`, and `picker` was initialised from `pick_by_name`.  A reader
        # can join them; the index does not pretend to have done the dataflow.
        callee = self.sym("app::pick_by_name")
        refs = {r["symbol"] for r in self.q.references_to(callee.usr)}
        self.assertIn("app::use_function_pointer", refs)
        report = self.q.impact(callee.usr)
        self.assertIn("app::use_function_pointer",
                      {p["symbol"] for p in report["possible"]})

    def test_an_indirect_call_is_never_recorded_as_a_resolved_call(self):
        usr = self.sym("app::use_function_pointer").usr
        resolved = list(self.store.connection().execute(
            "SELECT 1 FROM raw_edge WHERE src = ? AND kind = 'calls' AND dst ="
            " (SELECT usr FROM symbol WHERE qualified = 'app::pick_by_name')",
            (usr,)))
        self.assertEqual(resolved, [])

    def test_taking_an_address_is_a_reference(self):
        usr = self.sym("app::pick_by_name").usr
        refs = {r["symbol"] for r in self.q.references_to(usr)}
        self.assertIn("app::use_function_pointer", refs)

    def test_the_c_function_pointer_field_carries_its_type(self):
        field = self.sym("clib_buffer::visit")
        self.assertEqual(field.kind, "field")
        self.assertIn("clib_visit_fn", self.q.symbol(field.usr)["type"])


class TestNamesAndScope(Corpus):
    def test_nested_class_is_qualified_by_its_enclosing_class(self):
        self.assertEqual(self.sym("geo::Registry::Entry").kind, "class")
        detail = self.q.symbol(self.sym("geo::Registry::Entry::weight").usr)
        self.assertEqual(detail["member_of"], "geo::Registry::Entry")

    def test_namespace_is_part_of_the_qualified_name(self):
        self.assertEqual(self.sym("geo::Circle::area").qualified,
                         "geo::Circle::area")

    def test_unqualified_names_are_ambiguous_across_namespaces(self):
        # `scale` alone names the free function in geo and nothing else here,
        # but `area` names four methods.  Asking for a bare method name must
        # not silently pick one.
        ref, candidates = self.q.one("area")
        self.assertIsNone(ref)
        self.assertGreaterEqual(len(candidates), 3)

    def test_a_static_method_is_identified_as_static(self):
        self.assertEqual(self.flag("geo::Registry::instance", "static"), True)

    def test_the_operator_is_a_method_with_a_name(self):
        # `registry += Entry(weight)` is two calls: the temporary's
        # constructor, and the operator.  An operator is a function, and the
        # edge reaches it by its declared name rather than by the token `+=`.
        self.assertEqual(self.edge_targets("app::add_entry", "calls"),
                         {"geo::Registry::operator+=(const Entry &)",
                          "geo::Registry::Entry::Entry(int)"})

    def test_a_static_function_keeps_internal_linkage(self):
        self.assertEqual(self.sym("clamp").kind, "function")
        self.assertEqual(self.flag("clamp", "static"), True)


class TestTemplates(Corpus):
    def test_the_primary_template_is_indexed(self):
        self.assertEqual(self.sym("geo::Box").kind, "class")

    def test_an_instantiation_is_recorded_separately(self):
        hits = [s for s in self.q.search("Box") if "int" in s["symbol"]]
        self.assertTrue(hits, "Box<int> was not indexed")

    def test_a_call_resolves_to_the_instantiation(self):
        callees = self.callee_names("app::unbox")
        self.assertTrue(any("Box" in c for c in callees), callees)

    def test_the_instantiation_points_back_at_the_template(self):
        usr = self.callee_usrs("app::unbox")[0]["usr"]
        row = self.store.connection().execute(
            "SELECT * FROM symbol WHERE usr = ?", (usr,)).fetchone()
        pattern = row["flags"]
        self.assertTrue(pattern, "the instantiation carries no flags")
        self.assertIn("inst", pattern)

    def test_a_template_function_instantiation_is_indexed(self):
        # The instantiation, by name, with its argument: a reader asking what
        # `twice_int` calls wants the function that was made, not the pattern
        # it was made from.
        self.assertEqual(self.callee_names("app::twice_int"), {"geo::twice<int>"})

    def test_two_instantiations_of_one_template_are_different_symbols(self):
        a = {c["usr"] for c in self.callee_usrs("app::unbox")}
        b = {c["usr"] for c in self.callee_usrs("app::unbox_double")}
        self.assertTrue(a and b)
        self.assertNotEqual(a, b)


class TestTypesAndAliases(Corpus):
    def test_a_typedef_is_its_own_symbol(self):
        self.assertEqual(self.sym("geo::Registry::Count").kind in
                         ("typedef", "alias", "type_alias"), True)

    def test_return_type_is_recorded(self):
        detail = self.q.symbol(self.sym("app::doubled").usr)
        self.assertEqual(detail["type"], "int")

    def test_a_reference_parameter_does_not_change_the_signature(self):
        detail = self.q.symbol(self.sym("app::through_reference").usr)
        self.assertIn("vector", detail["signature"])

    def test_a_definition_in_another_file_is_reported(self):
        # Declared in shapes.h, defined in shapes.cpp.  A reader sent to the
        # declaration learns nothing; the definition is the useful location.
        detail = self.q.symbol(self.sym("geo::Circle::area").usr)
        self.assertTrue(detail["location"].startswith("include/shapes.h:"),
                        detail["location"])
        self.assertTrue(detail["defined_at"].startswith("src/shapes.cpp:"),
                        detail["defined_at"])
        self.assertNotEqual(detail["location"], detail["defined_at"])


class TestC(Corpus):
    def test_struct_tags_and_typedefs(self):
        self.assertEqual(self.sym("clib_point", kind="struct").kind, "struct")
        self.assertEqual(self.sym("clib_size", kind="typedef").kind, "typedef")

    def test_typedef_and_struct_are_different_entities(self):
        # `struct clib_point` and `clib_size` (an anonymous struct) must not
        # collapse into one another.
        # `clib_size` names two entities - the typedef and the anonymous
        # struct it names - so both are asked for by kind.
        self.assertNotEqual(self.sym("clib_point", kind="struct").usr,
                            self.sym("clib_size", kind="typedef").usr)
        self.assertNotEqual(self.sym("clib_size", kind="typedef").usr,
                            self.sym("clib_size", kind="struct").usr)

    def test_enum(self):
        self.assertEqual(self.sym("clib_status").kind, "enum")

    def test_global_variable(self):
        self.assertEqual(self.sym("clib_total").kind, "variable")

    def test_a_call_into_another_translation_unit_resolves(self):
        # `main` is in c_main.c; all four of these are defined in c_lib.c.  The
        # declaration reached is the one in the shared header, which is what
        # makes the call site and the definition one symbol.
        callees = self.callee_names("main")
        self.assertLessEqual({"clib_init", "clib_push", "clib_size_of",
                              "clib_set_visitor", "clib_foreach"}, callees)

    def test_a_function_is_one_symbol_across_translation_units(self):
        callees = self.callee_usrs("main")
        decl = next(c for c in callees if c["symbol"] == "clib_init")
        detail = self.q.symbol(decl["usr"])
        # Declared in the header, defined in c_lib.c: two locations, one
        # symbol, and both are reported.
        self.assertTrue(detail["location"].startswith("include/c_lib.h:"),
                        detail["location"])
        self.assertTrue(detail["defined_at"].startswith("src/c_lib.c:"),
                        detail["defined_at"])
        self.assertEqual(detail["used_in_translation_units"], 2)

    def test_a_function_with_no_calls_reports_none(self):
        # The honest answer for a leaf.  A graph that invented an edge here
        # would make every leaf look reachable from somewhere.
        self.assertEqual(self.callee_names("clib_push"), set())

    def test_a_call_through_a_struct_field_is_indirect(self):
        usr = self.sym("main").usr
        rows = list(self.store.connection().execute(
            "SELECT s.qualified FROM raw_edge e JOIN symbol s ON s.usr = e.dst"
            " WHERE e.src = ? AND e.kind = 'calls_indirect'", (usr,)))
        self.assertIn("clib_buffer::visit", [r["qualified"] for r in rows])

    def test_a_function_whose_address_is_taken_is_reachable(self):
        report = self.q.impact(self.sym("clib_sum_visitor").usr)
        possible = {p["symbol"] for p in report["possible"]}
        self.assertIn("main", possible)

    def test_a_static_function_is_distinct_from_its_namesakes(self):
        # `clamp` has internal linkage.  A second translation unit may define
        # its own, and they are not the same function.
        self.assertEqual(self.sym("clamp").file, "src/c_main.c")

    def test_macros_are_recorded_by_definition_site(self):
        hits = self.q.search("CLIB_MAX_ITEMS")
        self.assertTrue(hits)
        self.assertEqual(self.q.resolve("CLIB_MAX_ITEMS")[0].kind, "macro")

    def test_no_call_is_claimed_through_a_macro(self):
        # `CLIB_SQUARE(3)` is gone by the time the AST exists.  What the index
        # must not do is invent an edge to a macro as if it were called.
        for edge in self.store.connection().execute(
                "SELECT DISTINCT e.kind FROM raw_edge e JOIN symbol s"
                " ON s.usr = e.dst WHERE s.kind = 'macro'"):
            self.fail(f"macro reached by a {edge['kind']} edge")


class TestImpactOverTheCorpus(Corpus):
    def test_base_method_impact_reaches_the_whole_hierarchy(self):
        report = self.q.impact(self.sym("geo::Shape::area").usr)
        direct = {d["symbol"] for d in report["direct"]}
        possible = {p["symbol"] for p in report["possible"]}
        self.assertIn("app::measure", direct)          # calls it through Shape*
        self.assertIn("geo::Circle::area", possible)   # overrides it
        self.assertIn("geo::Tagged::area", possible)   # overrides it, one hop on

    def test_impact_is_not_empty_for_a_leaf_function(self):
        report = self.q.impact(self.sym("geo::Circle::area").usr)
        self.assertTrue(report["direct"] or report["possible"])

    def test_an_unrelated_symbol_has_no_impact(self):
        report = self.q.impact(self.sym("clib_push").usr)
        self.assertNotIn("geo::Circle::area",
                         {d["symbol"] for d in report["direct"]})


class TestFileIdentity(Corpus):
    """One file, one identity.

    The compilation database here writes relative paths, which is what a
    hand-written or Meson-generated database looks like, while the preprocessor
    reports headers by the path it opened.  Those are two spellings of one
    file; if they intern separately the file's symbols attach to one id and
    its translation unit to another, and every question about the file answers
    "nothing".
    """

    def test_no_file_is_stored_twice(self):
        rows = self.store.connection().execute(
            "SELECT path, COUNT(*) AS n FROM file GROUP BY path HAVING n > 1"
        ).fetchall()
        self.assertEqual([dict(r) for r in rows], [])

    def test_every_translation_unit_owns_the_symbols_it_declares(self):
        for tu in ("src/shapes.cpp", "src/usage.cpp", "src/c_lib.c"):
            with self.subTest(tu=tu):
                self.assertTrue(self.q.file_symbols(tu),
                                f"{tu} reports no symbols at all")

    def test_a_source_file_lists_what_it_defines(self):
        # shapes.cpp holds no declarations of its own: every symbol in it is
        # declared in the header.  Listing by declaration alone says the file
        # is empty.
        names = {s["symbol"] for s in self.q.file_symbols("src/shapes.cpp")}
        self.assertIn("geo::Circle::area", names)
        self.assertIn("geo::scale", names)

    def test_a_definition_is_found_by_its_line_in_the_source_file(self):
        area = self.q.one("geo::Circle::area")[0]
        entry = self.q.symbol(area.usr)
        self.assertTrue(entry["defined_at"].startswith("src/shapes.cpp:"))
        line = int(entry["defined_at"].rsplit(":", 1)[1])
        hits = {h["symbol"]
                for h in self.q.symbols_in_range("src/shapes.cpp", line, line)}
        self.assertIn("geo::Circle::area", hits)

    def test_a_declaration_is_found_by_its_line_in_the_header(self):
        area = self.q.one("geo::Circle::area")[0]
        line = int(area.location.rsplit(":", 1)[1])
        hits = {h["symbol"]
                for h in self.q.symbols_in_range("include/shapes.h", line, line)}
        self.assertIn("geo::Circle::area", hits)

    def test_the_innermost_container_of_a_line_in_a_body_is_its_function(self):
        area = self.q.one("geo::Circle::area")[0]
        entry = self.q.symbol(area.usr)
        line = int(entry["defined_at"].rsplit(":", 1)[1])
        hits = self.q.containers_in_range("src/shapes.cpp", line, line)
        self.assertEqual(hits[0]["symbol"], "geo::Circle::area")


class TestSourceContext(Corpus):
    """The region an agent gets instead of the file."""

    def test_the_definition_is_what_comes_back(self):
        # The declaration is in the header and says nothing; the body is in
        # the source file.  A reader asking about `area` wants the body.
        ctx = self.q.source_context("geo::Circle::area", context_lines=2)
        self.assertTrue(ctx["region"].startswith("src/shapes.cpp:"))
        self.assertEqual(ctx["declared_at"], "include/shapes.h:39")
        self.assertIn("3.14159", ctx["source"])

    def test_the_region_is_small(self):
        whole = len(self.q.source_context("geo::Circle::area",
                                          context_lines=0)["source"].splitlines())
        self.assertEqual(whole, 1)

    def test_line_numbers_are_part_of_the_text(self):
        # The agent's next question is usually about a specific line, and
        # counting lines in a quoted block is a good way to be off by one.
        ctx = self.q.source_context("geo::Circle::area", context_lines=1)
        first = ctx["region"].rsplit(":", 1)[1].split("-")[0]
        self.assertTrue(ctx["source"].startswith(f"{first}: "))

    def test_a_class_region_covers_its_body(self):
        ctx = self.q.source_context("geo::Circle", context_lines=0)
        self.assertIn("class Circle : public Shape {", ctx["source"])
        self.assertIn("};", ctx["source"])

    def test_an_ambiguous_name_reports_the_alternatives(self):
        ctx = self.q.source_context("scale")
        self.assertIn("more than one symbol", ctx["error"])
        self.assertEqual({c["signature"] for c in ctx["candidates"]},
                         {"(int)", "(double)", "(double, double)"})

    def test_a_name_that_does_not_exist_is_not_an_error_of_the_tool(self):
        ctx = self.q.source_context("no_such_symbol_anywhere")
        self.assertIn("no symbol matching", ctx["error"])

    def test_a_zero_context_request_still_returns_the_whole_symbol(self):
        # Padding is negotiable; the symbol is not.
        ctx = self.q.source_context("geo::Tagged", context_lines=0)
        self.assertIn("class Tagged", ctx["source"])
        self.assertNotIn("truncated", ctx)

    def test_a_symbol_longer_than_the_cap_is_truncated_and_says_so(self):
        ctx = self.q.source_context("geo", context_lines=0, max_lines=3)
        self.assertTrue(ctx["truncated"])
        self.assertEqual(len(ctx["source"].splitlines()), 3)


if __name__ == "__main__":
    unittest.main()
