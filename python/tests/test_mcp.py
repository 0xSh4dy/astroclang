"""The protocol layer: framing, dispatch, and what a client sees.

The unit tests drive `Server.handle` and `Server.serve` directly, so a protocol
mistake is reported as a protocol mistake rather than as a hung subprocess.
The last test in the file then runs the same server under a real MCP client
over a real pipe, because the one thing a hand-written server cannot verify
about itself is whether another implementation agrees with it.
"""

import asyncio
import io
import json
import os
import sys
import unittest
from pathlib import Path

from astroclang import indexer, tools
from astroclang.mcp_server import (DEFAULT_PROTOCOL, PARSE_ERROR,
                                       PROTOCOL_VERSIONS, Server, open_index)

from tests.test_query import Fixture
from tests.test_semantics import Corpus

PYTHON_DIR = Path(__file__).resolve().parents[1]

try:
    EXTRACTOR = indexer.find_extractor()
except indexer.ExtractorNotFound:
    EXTRACTOR = None

try:  # the reference client, used only to check that we agree with it
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:  # pragma: no cover - the SDK is optional
    ClientSession = None


def ask(server, method, params=None, msg_id=1):
    """One request, returned as the whole response object."""
    message = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return server.handle(message)


def payload(response):
    """The JSON a tool call answered with, from inside the content envelope."""
    return json.loads(response["result"]["content"][0]["text"])


class WithoutAnIndex(unittest.TestCase):
    """A server started before any index exists, which is how it starts."""

    def setUp(self):
        self.server = Server(missing="no index at .idx/index.db")

    def test_the_handshake_still_happens(self):
        out = ask(self.server, "initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(out["result"]["protocolVersion"], "2025-06-18")

    def test_the_list_is_served_so_the_agent_can_see_what_it_is_missing(self):
        out = ask(self.server, "tools/list")["result"]
        self.assertEqual({t["name"] for t in out["tools"]},
                         {t.name for t in tools.TOOLS})

    def test_every_call_explains_the_absence_and_what_to_do_about_it(self):
        out = ask(self.server, "tools/call",
                  {"name": "get_file", "arguments": {"path": "x.cpp"}})
        self.assertTrue(out["result"]["isError"])
        self.assertIn("no index at", payload(out)["error"])
        self.assertIn("astroclang index", payload(out)["hint"])

    def test_a_missing_index_file_produces_exactly_that_server(self):
        server = open_index(Path("/nonexistent/index.db"))
        self.assertIsNone(server.query)
        out = ask(server, "tools/call", {"name": "get_index_status"})
        self.assertIn("astroclang index", payload(out)["hint"])


class TestHandshake(WithoutAnIndex):
    def test_a_supported_version_is_echoed_back(self):
        for version in PROTOCOL_VERSIONS:
            out = ask(self.server, "initialize",
                      {"protocolVersion": version})["result"]
            self.assertEqual(out["protocolVersion"], version)

    def test_an_unknown_version_gets_the_newest_we_speak(self):
        out = ask(self.server, "initialize",
                  {"protocolVersion": "1999-01-01"})["result"]
        self.assertEqual(out["protocolVersion"], DEFAULT_PROTOCOL)

    def test_the_handshake_names_the_server_and_what_it_offers(self):
        out = ask(self.server, "initialize", {})["result"]
        self.assertEqual(out["serverInfo"]["name"], "astroclang")
        self.assertIn("tools", out["capabilities"])
        # The instructions are the one place to explain the index to an agent
        # before it has asked anything.
        self.assertIn("get_index_status", out["instructions"])

    def test_a_notification_gets_no_reply(self):
        # `notifications/initialized` is the one every client sends first.
        self.assertIsNone(self.server.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_ping_is_answered(self):
        self.assertEqual(ask(self.server, "ping")["result"], {})


class WithAnIndex(Fixture):
    def setUp(self):
        super().setUp()
        self.server = Server(query=self.q, store=self.store)


class TestTools(WithAnIndex):
    def test_a_call_returns_the_payload_as_json_text(self):
        out = ask(self.server, "tools/call",
                  {"name": "get_index_status", "arguments": {}})["result"]
        self.assertFalse(out.get("isError"))
        self.assertEqual(out["content"][0]["type"], "text")
        self.assertIn("symbols", json.loads(out["content"][0]["text"]))

    def test_a_name_that_denotes_several_arrives_whole(self):
        out = ask(self.server, "tools/call",
                  {"name": "get_symbol", "arguments": {"symbol": "allocate"}})
        self.assertTrue(out["result"]["isError"])
        self.assertEqual(len(payload(out)["candidates"]), 4)

    def test_a_question_with_no_answer_is_not_an_error(self):
        out = ask(self.server, "tools/call",
                  {"name": "get_callees",
                   "arguments": {"symbol": "mem::Helper::grow"}})
        self.assertFalse(out["result"].get("isError"))
        self.assertEqual(payload(out)["callees"], [])

    def test_an_unknown_tool_is_an_error_with_the_list(self):
        out = ask(self.server, "tools/call", {"name": "get_everything"})
        self.assertTrue(out["result"]["isError"])
        self.assertIn("find_symbol", payload(out)["tools"])

    def test_a_call_without_a_name_is_a_protocol_error(self):
        out = ask(self.server, "tools/call", {"arguments": {}})
        self.assertEqual(out["error"]["code"], -32602)

    def test_a_failing_tool_does_not_take_the_session_with_it(self):
        # The client is a long-lived process and a bad call is routine: the
        # next message on the same server has to work.
        ask(self.server, "tools/call", {"name": "get_symbol",
                                        "arguments": {"symbol": "allocate"}})
        self.assertEqual(ask(self.server, "ping")["result"], {})


class TestFraming(unittest.TestCase):
    def _serve(self, text: str):
        out = io.StringIO()
        code = Server(missing="no index").serve(io.StringIO(text), out)
        self.assertEqual(code, 0)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def test_several_messages_in_one_stream(self):
        responses = self._serve(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            + "\n")
        self.assertEqual([r["id"] for r in responses], [1, 2])

    def test_blank_lines_are_not_messages(self):
        responses = self._serve("\n\n"
                                + json.dumps({"jsonrpc": "2.0", "id": 7,
                                              "method": "ping"}) + "\n\n")
        self.assertEqual([r["id"] for r in responses], [7])

    def test_a_line_that_is_not_json_is_reported_and_the_stream_goes_on(self):
        responses = self._serve("this is not json\n"
                                + json.dumps({"jsonrpc": "2.0", "id": 3,
                                              "method": "ping"}) + "\n")
        self.assertEqual(responses[0]["error"]["code"], PARSE_ERROR)
        self.assertEqual(responses[1]["id"], 3)

    def test_an_unknown_method_is_refused_by_code(self):
        out = ask(Server(), "resources/list")
        self.assertEqual(out["error"]["code"], -32601)

    def test_a_batch_is_refused_rather_than_ignored(self):
        responses = self._serve('[{"jsonrpc": "2.0", "id": 1, "method": "ping"}]')
        self.assertEqual(responses[0]["error"]["code"], -32600)

    def test_a_message_with_no_method_is_refused(self):
        out = Server().handle({"jsonrpc": "2.0", "id": 1})
        self.assertEqual(out["error"]["code"], -32600)

    def test_a_broken_pipe_ends_the_session_quietly(self):
        # A client that goes away mid-write is an ordinary end to a session,
        # not something to fail on: the process reading the error is gone.
        class Gone(io.StringIO):
            def write(self, _):
                raise BrokenPipeError

        self.assertEqual(
            Server().serve(io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}'),
                           Gone()), 0)


@unittest.skipIf(ClientSession is None, "the mcp SDK is not installed")
@unittest.skipIf(EXTRACTOR is None, "astroclang-index has not been built")
class TestWithTheReferenceClient(Corpus):
    """The whole thing, over a real pipe, under the real MCP client.

    Everything above tests this server against this server's idea of the
    protocol.  This is the test that would fail if that idea were wrong.
    """

    SCRIPT = (
        "import sys\n"
        "from astroclang.mcp_server import open_index, serve_stdio\n"
        "raise SystemExit(serve_stdio(open_index(sys.argv[1], sys.argv[2])))\n"
    )

    def test_a_client_can_handshake_list_and_call(self):
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", self.SCRIPT, str(self.store.path), str(self.q.root)],
            env={**os.environ, "PYTHONPATH": str(PYTHON_DIR)},
        )

        async def session():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    listed = await client.list_tools()
                    status = await client.call_tool("get_index_status", {})
                    callers = await client.call_tool(
                        "get_callers", {"symbol": "geo::Circle::area"})
                    return listed, status, callers

        listed, status, callers = asyncio.run(session())

        self.assertIn("get_callers", {t.name for t in listed.tools})
        self.assertFalse(status.isError)
        self.assertEqual(json.loads(status.content[0].text)["accuracy"],
                         "exact")
        self.assertFalse(callers.isError)
        found = json.loads(callers.content[0].text)
        self.assertIn("app::measure_circle",
                      {c["symbol"] for c in found["callers"]})


if __name__ == "__main__":
    unittest.main()
