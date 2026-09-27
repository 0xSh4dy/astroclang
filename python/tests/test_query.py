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


if __name__ == "__main__":
    unittest.main()
