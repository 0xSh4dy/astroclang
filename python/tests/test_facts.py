"""The fact reader.

The reader's job is mostly to be suspicious.  A fact stream arrives from a
separate process that may have been killed, may have run out of memory, or may
have been handed a file it could not compile; the reader's contract is that it
never hands back a partially-populated translation unit as if it were whole.
"""

import json
import unittest

from cpp_code_graph.facts import FactStreamError, read_facts


def stream(*records) -> list:
    return [json.dumps(r) for r in records]


COMPLETE = [
    {"t": "meta", "k": "tu", "v": "/p/src/a.cpp"},
    {"t": "meta", "k": "config_source", "v": "compile_commands.json"},
    {"t": "meta", "k": "stats", "v": {"symbols": 2, "edges": 1}},
    {"t": "f", "i": 0, "p": "/p/src/a.cpp"},
    {"t": "f", "i": 1, "p": "/p/include/a.h"},
    {"t": "f", "i": 2, "p": "/usr/include/vector", "sys": 1},
    {"t": "sym", "u": "c:@F@main", "k": "function", "n": "main", "q": "main",
     "s": "()", "f": 0, "l": 4, "el": 9, "p": ""},
    {"t": "sym", "u": "c:@N@ns@S@T@F@go", "k": "method", "n": "go",
     "q": "ns::T::go", "stub": 1, "f": 2, "l": 100},
    {"t": "edge", "k": "calls", "a": "c:@F@main", "b": "c:@N@ns@S@T@F@go",
     "f": 0, "l": 6, "F": {"virt": 1}},
    {"t": "inc", "f": 0, "b": 1, "l": 1, "ang": 0, "sp": "a.h"},
    {"t": "done", "v": 1},
]


class TestReader(unittest.TestCase):
    def test_reads_a_complete_stream(self):
        tu = read_facts(iter(stream(*COMPLETE)), source="a.cpp")
        self.assertTrue(tu.complete)
        self.assertEqual(tu.path, "/p/src/a.cpp")
        self.assertEqual(tu.config_source, "compile_commands.json")
        self.assertEqual(tu.stats["symbols"], 2)
        self.assertEqual(len(tu.files), 3)
        self.assertEqual(len(tu.symbols), 2)
        self.assertEqual(len(tu.edges), 1)
        self.assertEqual(len(tu.includes), 1)

    def test_file_ids_stay_local(self):
        # The reader must not renumber: the extractor's ids are the only
        # handle the other records have on a file.
        tu = read_facts(iter(stream(*COMPLETE)))
        self.assertEqual([f.local_id for f in tu.files], [0, 1, 2])

    def test_system_header_flag(self):
        tu = read_facts(iter(stream(*COMPLETE)))
        self.assertFalse(tu.files[0].is_system)
        self.assertTrue(tu.files[2].is_system)

    def test_symbol_fields(self):
        tu = read_facts(iter(stream(*COMPLETE)))
        main = tu.symbols[0]
        self.assertEqual(main.usr, "c:@F@main")
        self.assertEqual(main.kind, "function")
        self.assertEqual(main.signature, "()")
        self.assertEqual(main.end_line, 9)
        self.assertFalse(main.stub)
        self.assertTrue(tu.symbols[1].stub)

    def test_edge_keeps_call_site_and_flags(self):
        tu = read_facts(iter(stream(*COMPLETE)))
        edge = tu.edges[0]
        self.assertEqual(edge.src, "c:@F@main")
        self.assertEqual(edge.dst, "c:@N@ns@S@T@F@go")
        self.assertEqual(edge.line, 6)
        self.assertEqual(edge.flags, {"virt": 1})

    def test_include_records_the_spelling(self):
        tu = read_facts(iter(stream(*COMPLETE)))
        inc = tu.includes[0]
        self.assertEqual((inc.from_file, inc.to_file), (0, 1))
        self.assertEqual(inc.spelled, "a.h")
        self.assertFalse(inc.angled)

    def test_truncated_stream_is_rejected(self):
        # This is what a crashed extractor leaves behind.  Accepting it would
        # index a prefix of the translation unit and report success.
        text = stream(*COMPLETE[:-1])
        with self.assertRaises(FactStreamError) as ctx:
            read_facts(iter(text), source="a.cpp")
        self.assertIn("a.cpp", str(ctx.exception))

    def test_malformed_line_is_rejected(self):
        text = stream(*COMPLETE[:-1]) + ['{"t": "sym", "u": "c:@F@x"']
        with self.assertRaises(FactStreamError):
            read_facts(iter(text))

    def test_empty_stream_is_rejected(self):
        with self.assertRaises(FactStreamError):
            read_facts(iter([]))

    def test_blank_lines_are_ignored(self):
        text = ["", *stream(*COMPLETE), "", ""]
        self.assertTrue(read_facts(iter(text)).complete)

    def test_diagnostics_are_read(self):
        text = stream(
            {"t": "meta", "k": "tu", "v": "/p/a.cpp"},
            {"t": "diag", "sev": "error", "f": 0, "l": 3, "c": 1,
             "m": "no member named 'x'"},
            {"t": "done", "v": 1},
        )
        tu = read_facts(iter(text))
        self.assertEqual(len(tu.diags), 1)
        self.assertEqual(tu.diags[0].severity, "error")
        self.assertEqual(tu.diags[0].line, 3)

    def test_degraded_config_is_recorded(self):
        text = stream(
            {"t": "meta", "k": "tu", "v": "/p/a.cpp"},
            {"t": "meta", "k": "degraded", "v": "no compilation database"},
            {"t": "meta", "k": "errors", "v": {"errors": 2}},
            {"t": "done", "v": 1},
        )
        tu = read_facts(iter(text))
        self.assertTrue(tu.degraded)
        self.assertEqual(tu.errors, 2)


if __name__ == "__main__":
    unittest.main()
