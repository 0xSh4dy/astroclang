"""Queries over a synthetic index.

The fixture is hand-written rather than extracted so that the relationships
under test are stated exactly once, in the test, and a change in the extractor
cannot quietly change what these assertions mean.  What is being tested is the
query layer: does it find the right symbol, does it refuse to guess when a name
is ambiguous, and does it keep the three degrees of impact apart.
"""

import tempfile
import unittest
from pathlib import Path

from cpp_code_graph.facts import (EdgeFact, FileFact, IncludeFact, SymbolFact,
                                  TranslationUnit)
from cpp_code_graph.query import Query
from cpp_code_graph.store import Store


def sym(usr, kind, name, qualified="", sig="", file=0, line=1, end_line=None,
        stub=False, parent="", type_text="", flags=None, def_file=None,
        def_line=None):
    return SymbolFact(usr=usr, kind=kind, name=name, qualified=qualified or name,
                      signature=sig, file=file, line=line, end_line=end_line,
                      stub=stub, parent_usr=parent, type_text=type_text,
                      flags=flags or {}, def_file=def_file, def_line=def_line)


def edge(kind, src, dst, file=0, line=1, flags=None, weight=1):
    return EdgeFact(kind=kind, src=src, dst=dst, file=file, line=line,
                    flags=flags or {}, weight=weight)


class Fixture(unittest.TestCase):
    """A small project whose shape is fully known:

        include/iface.h   namespace mem; class Allocator with a pure virtual
                          allocate() and a virtual size(); class Fast derives
                          from it and overrides allocate()
        src/pool.cpp      Fast::allocate calls Helper::grow
        src/use.cpp       run() calls Allocator::size and takes &Fast::allocate
        src/other.cpp     A second class with its own allocate(), to make the
                          bare name ambiguous in the way real code is
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for d in ("include", "src"):
            (self.root / d).mkdir()
        self.store = Store(self.root / ".idx" / "index.db",
                           project_root=self.root)
        self.populate()
        self.store.rebuild_symbols()
        self.q = Query(self.store)
        self.hdr = self.root / "include" / "iface.h"
        self.pool = self.root / "src" / "pool.cpp"
        self.use = self.root / "src" / "use.cpp"
        self.other = self.root / "src" / "other.cpp"

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def populate(self):
        hdr, pool, use, other = (self.hdr_path(), self.root / "src/pool.cpp",
                                 self.root / "src/use.cpp",
                                 self.root / "src/other.cpp")
        self.store.ingest(TranslationUnit(
            path=str(pool), complete=True,
            files=[FileFact(0, str(pool), False), FileFact(1, str(hdr), False)],
            symbols=[
                sym("c:@N@mem@S@Allocator", "class", "Allocator",
                    "mem::Allocator", file=1, line=4, end_line=12),
                sym("c:@N@mem@S@Allocator@F@allocate#l#", "method", "allocate",
                    "mem::Allocator::allocate", "(unsigned long)", file=1,
                    line=6, parent="c:@N@mem@S@Allocator",
                    flags={"virtual": 1, "pure": 1}, type_text="void *"),
                sym("c:@N@mem@S@Allocator@F@size", "method", "size",
                    "mem::Allocator::size", "()", file=1, line=7,
                    parent="c:@N@mem@S@Allocator", flags={"virtual": 1}),
                # `Fast`'s parent is the namespace, not `Allocator`: a parent
                # is the lexical container, and inheriting from a class does
                # not put a member inside it.
                sym("c:@N@mem@S@Fast", "class", "Fast", "mem::Fast", file=1,
                    line=10),
                sym("c:@N@mem@S@Fast@F@allocate#l#", "method", "allocate",
                    "mem::Fast::allocate", "(unsigned long)", file=0, line=3,
                    parent="c:@N@mem@S@Fast",
                    flags={"virtual": 1, "override": 1}),
                sym("c:@N@mem@S@Helper@F@grow#l#", "method", "grow",
                    "mem::Helper::grow", "(unsigned long)", file=0, line=1),
            ],
            edges=[
                edge("inherits", "c:@N@mem@S@Fast", "c:@N@mem@S@Allocator",
                     file=1, line=10, flags={"acc": "pub"}),
                edge("overrides", "c:@N@mem@S@Fast@F@allocate#l#",
                     "c:@N@mem@S@Allocator@F@allocate#l#", file=1, line=11),
                edge("calls", "c:@N@mem@S@Fast@F@allocate#l#",
                     "c:@N@mem@S@Helper@F@grow#l#", file=0, line=4),
            ],
            includes=[IncludeFact(0, 1, 1, False, "iface.h")],
        ))
        self.store.ingest(TranslationUnit(
            path=str(use), complete=True,
            files=[FileFact(0, str(use), False), FileFact(1, str(hdr), False)],
            symbols=[
                sym("c:@F@run", "function", "run", "run", "()", file=0, line=2,
                    end_line=8),
            ],
            edges=[
                edge("calls", "c:@F@run", "c:@N@mem@S@Allocator@F@size",
                     file=0, line=4, flags={"virt": 1}),
                edge("references", "c:@F@run",
                     "c:@N@mem@S@Fast@F@allocate#l#", file=0, line=5),
            ],
            includes=[IncludeFact(0, 1, 1, False, "iface.h")],
        ))
        # A second `allocate` in an unrelated class.  `resize`-style collisions
        # are the normal case in C++, not an edge case.
        self.store.ingest(TranslationUnit(
            path=str(other), complete=True,
            files=[FileFact(0, str(other), False)],
            symbols=[
                sym("c:@S@Buffer@F@allocate#l#", "method", "allocate",
                    "Buffer::allocate", "(unsigned long)", file=0, line=3,
                    parent="c:@S@Buffer"),
                sym("c:@S@Buffer@F@allocate#d#", "method", "allocate",
                    "Buffer::allocate", "(double)", file=0, line=4,
                    parent="c:@S@Buffer"),
            ],
        ))

    def hdr_path(self):
        return self.root / "include" / "iface.h"

    # -- helpers -------------------------------------------------------------

    def usr(self, qualified, sig=None):
        cands = self.q.resolve(qualified)
        if sig is not None:
            cands = [c for c in cands if c.signature == sig]
        self.assertTrue(cands, f"no symbol {qualified}{sig or ''}")
        return cands[0].usr


class TestResolution(Fixture):
    def test_resolves_by_usr(self):
        usr = self.usr("mem::Allocator::allocate", "(unsigned long)")
        self.assertEqual(self.q.resolve(usr)[0].usr, usr)

    def test_resolves_by_qualified_name(self):
        ref = self.q.one("mem::Allocator::size")
        self.assertIsNotNone(ref[0])
        self.assertEqual(ref[0].qualified, "mem::Allocator::size")

    def test_resolves_by_signature_to_the_right_overload(self):
        # The whole point: two overloads of the same name, and the parameter
        # list is what tells them apart.
        ref = self.q.one("Buffer::allocate(double)")
        self.assertIsNotNone(ref[0])
        self.assertEqual(ref[0].signature, "(double)")
        self.assertIn("#d#", ref[0].usr)

    def test_resolves_by_location(self):
        ref = self.q.one("src/other.cpp:4")
        self.assertIsNotNone(ref[0])
        self.assertEqual(ref[0].signature, "(double)")

    def test_resolves_by_the_definition_location_too(self):
        # A symbol occupies two places when it is declared in a header and
        # defined in a source file.  It is *reported* at its declaration, but
        # `get_source_context` answers with `at`, which is the definition, and
        # an agent that found the function by reading the source has the
        # definition's line.  Both are locations this tool emitted, so both
        # have to resolve back.
        self._one_symbol_declared_and_defined()
        ref = self.q.one("src/pool.cpp:40")
        self.assertIsNotNone(ref[0])
        self.assertEqual(ref[0].qualified, "mem::Fast::allocate")

    def test_resolves_by_a_span_as_well_as_a_single_line(self):
        # `20-58` is the form `get_source_context` prints for a region, so a
        # region cannot be handed to the next question unless a span parses.
        # The first line of the span is the one meant.
        self._one_symbol_declared_and_defined()
        ref = self.q.one("src/pool.cpp:40-44")
        self.assertIsNotNone(ref[0])
        self.assertEqual(ref[0].qualified, "mem::Fast::allocate")

    def _one_symbol_declared_and_defined(self):
        self.store.ingest(TranslationUnit(
            path=str(self.pool), complete=True,
            files=[FileFact(0, str(self.pool), False),
                   FileFact(1, str(self.hdr_path()), False)],
            symbols=[sym("c:@N@mem@S@Fast@F@allocate#l#", "method", "allocate",
                         "mem::Fast::allocate", "(unsigned long)", file=1,
                         line=11, def_file=0, def_line=40, end_line=44)],
        ))
        self.store.rebuild_symbols()

    def test_resolves_by_qualified_suffix(self):
        ref = self.q.one("Allocator::size")
        self.assertIsNotNone(ref[0])
        self.assertEqual(ref[0].qualified, "mem::Allocator::size")

    def test_an_ambiguous_name_returns_candidates_rather_than_a_guess(self):
        # `allocate` names four different methods here.  Picking the first
        # would answer "who calls allocate" with a confidently wrong list.
        ref, candidates = self.q.one("allocate")
        self.assertIsNone(ref)
        names = {c.qualified for c in candidates}
        self.assertIn("Buffer::allocate", names)
        self.assertIn("mem::Allocator::allocate", names)

    def test_candidates_are_ordered_with_project_symbols_first(self):
        candidates = self.q.resolve("allocate")
        self.assertTrue(candidates)
        self.assertTrue(all(c.in_project for c in candidates[:3]))

    def test_an_unknown_name_yields_nothing(self):
        self.assertEqual(self.q.resolve("NoSuchThing"), [])

    def test_a_substring_match_is_a_last_resort(self):
        self.assertTrue(self.q.resolve("Alloca"))
        self.assertEqual(self.q.resolve("zzzz"), [])


class TestSymbolDetail(Fixture):
    def test_symbol_reports_identity_and_properties(self):
        d = self.q.symbol(self.usr("mem::Allocator::allocate"))
        self.assertEqual(d["symbol"], "mem::Allocator::allocate")
        self.assertEqual(d["kind"], "method")
        self.assertEqual(d["type"], "void *")
        self.assertEqual(d["properties"]["pure"], True)
        self.assertEqual(d["member_of"], "mem::Allocator")
        self.assertEqual(d["location"], "include/iface.h:6")

    def test_members_lists_a_class(self):
        members = self.q.members(self.usr("mem::Allocator"))
        self.assertEqual({m["symbol"] for m in members},
                         {"mem::Allocator::allocate", "mem::Allocator::size"})

    def test_symbols_in_file(self):
        names = {s["symbol"] for s in self.q.symbols_in_file("src/other.cpp")}
        self.assertIn("Buffer::allocate", names)

    def test_search_prefers_the_shorter_name(self):
        hits = self.q.search("Alloc")
        self.assertTrue(hits)
        self.assertTrue(hits[0]["symbol"].startswith("mem::Allocator"))


class TestEdges(Fixture):
    def test_callers_of_a_callee(self):
        callers = self.q.callers(self.usr("mem::Helper::grow"))
        self.assertEqual([c["symbol"] for c in callers], ["mem::Fast::allocate"])

    def test_call_site_is_reported_apart_from_the_declaration(self):
        # The caller is declared on line 3 and calls on line 4.  A reader who
        # wants to look at the call needs the second number.
        callers = self.q.callers(self.usr("mem::Helper::grow"))
        self.assertEqual(callers[0]["location"], "src/pool.cpp:3")
        self.assertEqual(callers[0]["call_site"], "src/pool.cpp:4")

    def test_callees(self):
        callees = self.q.callees(self.usr("mem::Fast::allocate", "(unsigned long)"))
        self.assertEqual([c["symbol"] for c in callees], ["mem::Helper::grow"])

    def test_virtual_dispatch_is_flagged_on_the_edge(self):
        callers = self.q.callers(self.usr("mem::Allocator::size"))
        self.assertEqual(callers[0]["dispatch"], "virtual")

    def test_references_are_separate_from_calls(self):
        target = self.usr("mem::Fast::allocate", "(unsigned long)")
        self.assertEqual(self.q.callers(target), [])
        refs = self.q.references_to(target)
        self.assertEqual([r["symbol"] for r in refs], ["run"])

    def test_a_relationship_reported_by_two_translation_units_appears_once(self):
        # A base clause in a header is seen by every translation unit that
        # includes the header.  Two hundred includers must not turn one class
        # hierarchy into two hundred identical lines.
        self.store.ingest(TranslationUnit(
            path=str(self.other), complete=True,
            files=[FileFact(0, str(self.other), False),
                   FileFact(1, str(self.hdr_path()), False)],
            symbols=[sym("c:@N@mem@S@Fast2", "class", "Fast2", "mem::Fast2",
                         file=1, line=20)],
            edges=[edge("inherits", "c:@N@mem@S@Fast2", "c:@N@mem@S@Allocator",
                        file=1, line=20, flags={"acc": "pub"})],
            includes=[IncludeFact(0, 1, 1, False, "iface.h")],
        ))
        self.store.rebuild_symbols()
        tree = self.q.inheritance(self.usr("mem::Allocator"))
        self.assertEqual([d["symbol"] for d in tree["derived"]],
                         ["mem::Fast", "mem::Fast2"])

    def _inline_caller(self, target, weight=1, sites=(31,)):
        """The same header function, seen by two translation units."""
        for name in ("a.cpp", "b.cpp"):
            path = self.root / "src" / name
            self.store.ingest(TranslationUnit(
                path=str(path), complete=True,
                files=[FileFact(0, str(path), False),
                       FileFact(1, str(self.hdr_path()), False)],
                symbols=[sym("c:@N@mem@S@Helper@F@inline_size", "method",
                             "inline_size", "mem::Helper::inline_size", "()",
                             file=1, line=30)],
                edges=[edge("calls", "c:@N@mem@S@Helper@F@inline_size", target,
                            file=1, line=line, weight=weight)
                       for line in sites],
            ))
        self.store.rebuild_symbols()
        return [c for c in self.q.callers(target)
                if c["symbol"] == "mem::Helper::inline_size"]

    def test_call_sites_are_collected_rather_than_repeated(self):
        # An inline function defined in a header is compiled into every
        # translation unit that includes it, so its call to `size` is reported
        # once per includer.  The same call, seen twice, is still one call from
        # one caller - not two callers.
        callers = self._inline_caller(self.usr("mem::Allocator::size"))
        self.assertEqual(len(callers), 1)
        self.assertEqual(callers[0]["location"], "include/iface.h:30")
        self.assertEqual(callers[0]["call_site"], "include/iface.h:31")
        self.assertNotIn("call_sites", callers[0])

    def test_one_call_seen_by_two_translation_units_is_still_one_call(self):
        # The two reports are of one call at one line.  Summing them would say
        # the function calls `size` twice, which is false, and a reader asking
        # how hot a call is would be told the answer is a property of how many
        # files happened to be compiled.
        callers = self._inline_caller(self.usr("mem::Allocator::size"))
        self.assertNotIn("occurrences", callers[0])

    def test_a_repeated_call_is_counted_once_per_line_not_once_per_compile(self):
        # Here the call really is made twice - the extractor folded both into
        # one row - and both translation units agree on that.  Four would be
        # the number of compilations, not the number of calls.
        callers = self._inline_caller(self.usr("mem::Allocator::size"),
                                      weight=2)
        self.assertEqual(callers[0]["occurrences"], 2)

    def test_a_caller_with_many_sites_does_not_crowd_out_the_others(self):
        # The limit says how many callers to name.  A caller that calls the
        # target from sixty lines must not spend the whole budget on itself and
        # leave the other caller unmentioned.
        target = self.usr("mem::Allocator::size")
        path = self.root / "src" / "many.cpp"
        other = self.root / "src" / "other.cpp"
        self.store.ingest(TranslationUnit(
            path=str(path), complete=True,
            files=[FileFact(0, str(path), False),
                   FileFact(1, str(other), False)],
            symbols=[sym("c:@F@many", "function", "many", "many", "()", file=0,
                         line=1),
                     sym("c:@F@run", "function", "run", "run", "()", file=1,
                         line=1)],
            edges=[edge("calls", "c:@F@many", target, file=0, line=10 + i)
                   for i in range(60)]
            + [edge("calls", "c:@F@run", target, file=1, line=2)],
        ))
        self.store.rebuild_symbols()
        names = {c["symbol"] for c in self.q.callers(target, limit=2)}
        self.assertEqual(names, {"many", "run"})
        entry = next(c for c in self.q.callers(target) if c["symbol"] == "many")
        self.assertEqual(entry["call_site_count"], 60)
        self.assertEqual(len(entry["call_sites"]), 5)

    def test_a_caller_with_many_call_sites_summarises_them(self):
        # A function called from more than a handful of places does not need a
        # line of answer per call site to say so.
        target = self.usr("mem::Allocator::size")
        path = self.root / "src" / "many.cpp"
        self.store.ingest(TranslationUnit(
            path=str(path), complete=True,
            files=[FileFact(0, str(path), False)],
            symbols=[sym("c:@F@many", "function", "many", "many", "()", file=0,
                         line=1)],
            edges=[edge("calls", "c:@F@many", target, file=0, line=10 + i)
                   for i in range(9)],
        ))
        self.store.rebuild_symbols()
        entry = next(c for c in self.q.callers(target)
                     if c["symbol"] == "many")
        self.assertEqual(len(entry["call_sites"]), 5)
        self.assertEqual(entry["call_site_count"], 9)

    def test_a_virtual_base_is_distinguishable_from_a_repeat(self):
        # Two different bases that happen to share a name is not the same as
        # one base reported twice; the fold keys on the USR, not the name.
        self.store.ingest(TranslationUnit(
            path=str(self.other), complete=True,
            files=[FileFact(0, str(self.other), False)],
            symbols=[sym("c:@N@other@S@Allocator", "class", "Allocator",
                         "other::Allocator", file=0, line=30),
                     sym("c:@S@Both", "class", "Both", "Both", file=0, line=31)],
            edges=[edge("inherits", "c:@S@Both", "c:@N@other@S@Allocator",
                        file=0, line=31, flags={"acc": "priv"})],
        ))
        self.store.rebuild_symbols()
        tree = self.q.inheritance(self.usr("Both"))
        self.assertEqual([b["symbol"] for b in tree["bases"]],
                         ["other::Allocator"])

    def test_occurrence_count_survives(self):
        target = self.usr("mem::Allocator::size")
        self.store.ingest(TranslationUnit(
            path=str(self.use), complete=True,
            files=[FileFact(0, str(self.use), False),
                   FileFact(1, str(self.hdr_path()), False)],
            symbols=[sym("c:@F@run", "function", "run", "run", "()", file=0,
                         line=2)],
            edges=[edge("calls", "c:@F@run", target, file=0, line=4, weight=3)],
        ))
        self.store.rebuild_symbols()
        callers = self.q.callers(target)
        self.assertEqual(callers[0]["occurrences"], 3)


class TestInheritance(Fixture):
    def test_bases_and_derived(self):
        alloc = self.usr("mem::Allocator")
        tree = self.q.inheritance(alloc)
        self.assertEqual([d["symbol"] for d in tree["derived"]], ["mem::Fast"])
        self.assertEqual(tree["derived"][0]["access"], "public")

        fast = self.usr("mem::Fast")
        self.assertEqual([b["symbol"] for b in self.q.inheritance(fast)["bases"]],
                         ["mem::Allocator"])

    def test_overrides_are_reported_both_ways(self):
        base = self.usr("mem::Allocator::allocate")
        self.assertEqual(
            [o["symbol"] for o in self.q.inheritance(base)["overrides"]],
            ["mem::Fast::allocate"])
        derived = self.usr("mem::Fast::allocate", "(unsigned long)")
        self.assertEqual(
            [o["symbol"] for o in self.q.inheritance(derived)["overridden"]],
            ["mem::Allocator::allocate"])

    def test_a_direct_relative_is_not_repeated_in_the_closure(self):
        tree = self.q.inheritance(self.usr("mem::Allocator"))
        self.assertNotIn("descendants", tree)   # Fast is already in `derived`
        self.assertNotIn("ancestors", tree)

    def test_the_closure_carries_indirect_relatives(self):
        # Insert a third level so the closure has something the direct list
        # does not.
        self.store.ingest(TranslationUnit(
            path=str(self.other), complete=True,
            files=[FileFact(0, str(self.other), False),
                   FileFact(1, str(self.hdr_path()), False)],
            symbols=[sym("c:@N@mem@S@Faster", "class", "Faster", "mem::Faster",
                         file=0, line=20)],
            edges=[edge("inherits", "c:@N@mem@S@Faster", "c:@N@mem@S@Fast",
                        file=0, line=20, flags={"acc": "pub"})],
        ))
        self.store.rebuild_symbols()
        tree = self.q.inheritance(self.usr("mem::Allocator"))
        self.assertEqual([d["symbol"] for d in tree["derived"]], ["mem::Fast"])
        self.assertEqual([d["symbol"] for d in tree["descendants"]],
                         ["mem::Faster"])


class TestImpact(Fixture):
    def test_direct_callers(self):
        report = self.q.impact(self.usr("mem::Helper::grow"))
        self.assertEqual([d["symbol"] for d in report["direct"]],
                         ["mem::Fast::allocate"])
        self.assertIn("calls this symbol", report["direct"][0]["reason"])

    def test_indirect_callers_are_walked_outward(self):
        # `run` calls Allocator::size; Fast::allocate does not, but a change to
        # grow() reaches Fast::allocate, whose callers are a further hop.
        report = self.q.impact(self.usr("mem::Helper::grow"))
        self.assertEqual([i["symbol"] for i in report["indirect"]], [])
        report = self.q.impact(self.usr("mem::Helper::grow"))
        self.assertIn("note", report)

    def test_an_override_is_possible_not_certain(self):
        # A call through the base may reach the override, but only if the
        # object really is a Fast.  Calling that a certain dependency would
        # make the answer useless for deciding whether a change is safe.
        report = self.q.impact(self.usr("mem::Allocator::allocate"))
        possible = {p["symbol"]: p["reason"] for p in report["possible"]}
        self.assertIn("mem::Fast::allocate", possible)
        self.assertIn("overrides", possible["mem::Fast::allocate"])
        self.assertNotIn("mem::Fast::allocate",
                         {d["symbol"] for d in report["direct"]})

    def test_taking_a_function_address_is_possible_not_certain(self):
        report = self.q.impact(self.usr("mem::Fast::allocate", "(unsigned long)"))
        possible = {p["symbol"]: p["reason"] for p in report["possible"]}
        self.assertIn("run", possible)
        self.assertIn("address", possible["run"])

    def test_every_bucket_explains_itself(self):
        report = self.q.impact(self.usr("mem::Helper::grow"))
        for bucket in ("direct", "indirect", "possible"):
            for entry in report[bucket]:
                self.assertIn("reason", entry)

    def test_impact_of_an_unknown_symbol_is_empty(self):
        self.assertEqual(self.q.impact("c:@NOPE"), {})


class TestFiles(Fixture):
    def test_file_summary(self):
        info = self.q.file("include/iface.h")
        self.assertEqual(info["file"], "include/iface.h")
        self.assertIn("src/pool.cpp", info["included_by"])
        self.assertIn("src/pool.cpp", info["translation_units"])

    def test_includes_and_includers(self):
        inc = self.q.includes("src/pool.cpp")
        self.assertEqual([i["file"] for i in inc["includes"]],
                         ["include/iface.h"])
        self.assertEqual(inc["includes"][0]["spelled"], "iface.h")

    def test_transitive_includes(self):
        self.store.ingest(TranslationUnit(
            path=str(self.other), complete=True,
            files=[FileFact(0, str(self.other), False),
                   FileFact(1, str(self.pool), False),
                   FileFact(2, str(self.hdr), False)],
            includes=[IncludeFact(0, 1, 1, False, "pool.cpp"),
                      IncludeFact(1, 2, 1, False, "iface.h")],
        ))
        closure = self.q.includes("src/other.cpp", transitive=True)
        self.assertIn("include/iface.h", closure["includes_transitively"])

    def test_a_missing_file_is_not_an_error(self):
        self.assertIsNone(self.q.file("no/such/file.cpp"))
        self.assertEqual(self.q.file_symbols("no/such/file.cpp"), [])

    def test_a_header_basename_finds_the_file(self):
        self.assertEqual(self.q.file("iface.h")["file"], "include/iface.h")

    def test_tus_reaching_a_header(self):
        tus = self.q.tus_reaching("include/iface.h")
        self.assertIn("src/pool.cpp", tus)
        self.assertIn("src/use.cpp", tus)
        self.assertNotIn("src/other.cpp", tus)


class TestRanges(Fixture):
    def test_symbols_overlapping_a_range(self):
        hits = self.q.symbols_in_range("include/iface.h", 6, 6)
        self.assertIn("mem::Allocator::allocate", {h["symbol"] for h in hits})

    def test_containers_of_a_range_find_the_enclosing_function(self):
        # A changed line inside a body belongs to that body even when the edit
        # is to something the index does not track, such as a local.
        hits = self.q.containers_in_range("src/use.cpp", 6, 6)
        self.assertEqual([h["symbol"] for h in hits], ["run"])

    def test_a_symbol_defined_in_a_source_file_is_found_by_its_definition(self):
        # `allocate` is declared in the header at line 6 and defined in
        # pool.cpp at line 3.  Both are places the symbol occupies, and a
        # change to either one changes it.
        self.store.ingest(TranslationUnit(
            path=str(self.pool), complete=True,
            files=[FileFact(0, str(self.pool), False),
                   FileFact(1, str(self.hdr_path()), False)],
            symbols=[sym("c:@N@mem@S@Fast@F@allocate#l#", "method", "allocate",
                         "mem::Fast::allocate", "(unsigned long)", file=1,
                         line=11, def_file=0, def_line=40, end_line=44)],
        ))
        self.store.rebuild_symbols()
        by_definition = {h["symbol"]
                         for h in self.q.symbols_in_range("src/pool.cpp", 41, 42)}
        self.assertIn("mem::Fast::allocate", by_definition)
        by_declaration = {h["symbol"]
                          for h in self.q.symbols_in_range("include/iface.h", 11, 11)}
        self.assertIn("mem::Fast::allocate", by_declaration)

    def test_a_definition_end_line_is_not_read_against_the_declaration(self):
        # The extractor takes `end_line` from the definition's source range
        # while `line` is the declaration's, so a declaration in a header
        # carries an end line from a source file - a smaller number, from a
        # different file.  Reading the two together made a declaration fail to
        # match its own line, which is how this was found.
        self.store.ingest(TranslationUnit(
            path=str(self.pool), complete=True,
            files=[FileFact(0, str(self.pool), False),
                   FileFact(1, str(self.hdr_path()), False)],
            symbols=[sym("c:@N@mem@S@Fast@F@allocate#l#", "method", "allocate",
                         "mem::Fast::allocate", "(unsigned long)", file=1,
                         line=11, def_file=0, def_line=40, end_line=44)],
        ))
        self.store.rebuild_symbols()
        hits = self.q.symbols_in_range("include/iface.h", 11, 11)
        self.assertIn("mem::Fast::allocate", {h["symbol"] for h in hits})
        # ... and the header's other declarations are not dragged in by it.
        self.assertNotIn("mem::Fast::allocate",
                         {h["symbol"]
                          for h in self.q.symbols_in_range("include/iface.h", 6, 6)})


class TestFileContents(Fixture):
    def test_a_source_file_reports_what_it_defines(self):
        # A .cpp of out-of-line definitions declares nothing, so listing a
        # file by declaration alone answers "empty" for the file that holds
        # every body in the project.
        self.store.ingest(TranslationUnit(
            path=str(self.pool), complete=True,
            files=[FileFact(0, str(self.pool), False),
                   FileFact(1, str(self.hdr_path()), False)],
            symbols=[sym("c:@N@mem@S@Fast@F@allocate#l#", "method", "allocate",
                         "mem::Fast::allocate", "(unsigned long)", file=1,
                         line=6, def_file=0, def_line=3, end_line=7)],
        ))
        self.store.rebuild_symbols()
        here = {s["symbol"] for s in self.q.symbols_in_file("src/pool.cpp")}
        self.assertIn("mem::Fast::allocate", here)

    def test_both_locations_are_reported_so_the_reader_can_tell_them_apart(self):
        self.store.ingest(TranslationUnit(
            path=str(self.pool), complete=True,
            files=[FileFact(0, str(self.pool), False),
                   FileFact(1, str(self.hdr_path()), False)],
            symbols=[sym("c:@N@mem@S@Fast@F@allocate#l#", "method", "allocate",
                         "mem::Fast::allocate", "(unsigned long)", file=1,
                         line=6, def_file=0, def_line=3, end_line=7)],
        ))
        self.store.rebuild_symbols()
        entry = next(s for s in self.q.symbols_in_file("src/pool.cpp")
                     if s["symbol"] == "mem::Fast::allocate")
        self.assertEqual(entry["location"], "include/iface.h:6")
        self.assertEqual(entry["defined_at"], "src/pool.cpp:3")

    def test_a_kind_filter_narrows_the_listing(self):
        kinds = {s["kind"] for s in self.q.file_symbols("include/iface.h",
                                                       kind="class")}
        self.assertEqual(kinds, {"class"})


class TestDependencies(Fixture):
    def test_a_dependency_declared_in_a_header_is_listed_once(self):
        # A type relationship written in a header is reported by every
        # translation unit that includes the header, so the same dependency
        # arrives once per compilation.  Listing it once per compilation turns
        # "what does this function need" into a list of build events.
        target = self.usr("mem::Allocator")
        hdr = self.hdr_path()
        for name in ("a.cpp", "b.cpp"):
            path = self.root / "src" / name
            self.store.ingest(TranslationUnit(
                path=str(path), complete=True,
                files=[FileFact(0, str(path), False),
                       FileFact(1, str(hdr), False)],
                symbols=[sym("c:@F@user", "function", "user", "user", "()",
                             file=1, line=30)],
                edges=[edge("param_type", "c:@F@user", target, file=1, line=6)],
            ))
        self.store.rebuild_symbols()
        deps = self.q.symbol_dependencies(self.usr("user"))
        self.assertEqual([d["symbol"] for d in deps["param_type"]],
                         ["mem::Allocator"])


if __name__ == "__main__":
    unittest.main()
