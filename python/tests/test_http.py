"""The Streamable HTTP transport, over a real socket.

The unit tests here speak HTTP with `http.client` rather than calling into the
handler, because the things worth checking - which status a notification gets,
whether a foreign Origin is refused, whether the connection survives a second
request - are properties of the exchange and not of a function.  The last test
then hands the same server to the reference MCP client, which is the only way
to find out whether this server and the specification agree.
"""

import asyncio
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from astroclang import http_server, indexer, tools
from astroclang import store as store_module
from astroclang.http_server import make_server
from astroclang.mcp_server import PROTOCOL_VERSIONS, Server
from astroclang.query import Query

from tests.test_semantics import Corpus

try:  # the reference client, used only to check that we agree with it
    from mcp import ClientSession
except ImportError:  # pragma: no cover - the SDK is optional
    ClientSession = None

streamable_http_client = None
if ClientSession is not None:
    try:
        # The SDK renamed this.  The older spelling still resolves, but warns,
        # and a suite that prints warnings is a suite people stop reading.
        from mcp.client.streamable_http import streamable_http_client
    except ImportError:
        from mcp.client.streamable_http import (
            streamablehttp_client as streamable_http_client)

try:
    EXTRACTOR = indexer.find_extractor()
except indexer.ExtractorNotFound:
    EXTRACTOR = None


class Served(unittest.TestCase):
    """A server on an ephemeral port, driven over the loopback interface.

    Port 0 is asked for rather than a fixed one so that two runs of the suite -
    or two people on one machine - cannot collide, and so the test never
    depends on a port being free.
    """

    def setUp(self):
        self.server = Server(missing="no index at .astroclang/index.db")
        self.httpd, self.port, self.thread = self.serve(self.server)

    def serve(self, server, **kwargs):
        httpd = make_server(server, host="127.0.0.1", port=0, **kwargs)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd, httpd.server_address[1], thread

    def request(self, method, path=http_server.MCP_PATH, body=None,
                headers=None):
        """One exchange, returned as (status, headers, body)."""
        sent = {}
        for key, value in (headers or {}).items():
            if value is not None:  # lets a test omit a default on purpose
                sent[key] = value
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=sent)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def post(self, message, **kwargs):
        """A POST carrying `message`, framed the way a client frames one."""
        headers = {
            "Content-Type": http_server.JSON_TYPE,
            "Accept": f"{http_server.JSON_TYPE}, {http_server.SSE_TYPE}",
        }
        headers.update(kwargs.pop("headers", None) or {})
        body = message if isinstance(message, bytes) else json.dumps(message)
        return self.request("POST", body=body, headers=headers, **kwargs)

    @staticmethod
    def call(msg_id, method, params=None):
        message = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            message["params"] = params
        return message


class TestTheExchange(Served):
    def test_a_request_is_answered_with_json(self):
        status, headers, body = self.post(
            self.call(1, "initialize", {"protocolVersion": "2025-06-18"}))
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], http_server.JSON_TYPE)
        self.assertEqual(json.loads(body)["result"]["protocolVersion"],
                         "2025-06-18")

    def test_the_tool_list_comes_back_over_http(self):
        status, _, body = self.post(self.call(1, "tools/list"))
        self.assertEqual(status, 200)
        self.assertEqual({t["name"] for t in json.loads(body)["result"]["tools"]},
                         {t.name for t in tools.TOOLS})

    def test_a_notification_is_accepted_with_nothing_to_read(self):
        # The reply to a notification is not "an empty result", it is no
        # message at all - and 202 is how the transport says that.
        status, _, body = self.post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(status, 202)
        self.assertEqual(body, b"")

    def test_the_connection_can_carry_a_second_request(self):
        # Keep-alive is why Content-Length is sent on every reply, including
        # the empty one: without it a client waits for a body that never comes.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            for msg_id in (1, 2, 3):
                conn.request("POST", http_server.MCP_PATH,
                             body=json.dumps(self.call(msg_id, "ping")),
                             headers={"Content-Type": http_server.JSON_TYPE})
                response = conn.getresponse()
                self.assertEqual(json.loads(response.read())["id"], msg_id)
        finally:
            conn.close()

    def test_an_answer_the_client_may_not_want_is_still_correct(self):
        # A JSON-RPC error is a successfully delivered message, so the status
        # is 200 and the error is in the body - a non-2xx would say the
        # transport failed, which would be a different claim.
        status, _, body = self.post(self.call(1, "resources/list"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["error"]["code"], -32601)


class TestWhatIsRefused(Served):
    def test_get_is_refused_and_says_what_is_allowed(self):
        status, headers, _ = self.request("GET")
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "POST")

    def test_delete_is_refused_the_same_way(self):
        status, headers, _ = self.request("DELETE")
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "POST")

    def test_another_path_is_not_the_endpoint(self):
        status, _, _ = self.request("POST", path="/somewhere/else",
                                    body=json.dumps(self.call(1, "ping")))
        self.assertEqual(status, 404)

    def test_a_query_string_still_reaches_the_endpoint(self):
        status, _, _ = self.post(self.call(1, "ping"), path="/mcp?trace=1")
        self.assertEqual(status, 200)

    def test_a_body_that_is_not_json_is_a_400(self):
        status, _, body = self.post(b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], -32700)

    def test_an_empty_body_is_a_400(self):
        status, _, _ = self.post(b"")
        self.assertEqual(status, 400)

    def test_a_content_type_that_is_not_json_is_refused(self):
        status, _, _ = self.post(
            self.call(1, "ping"), headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_an_accept_that_excludes_both_types_is_refused(self):
        status, _, _ = self.post(
            self.call(1, "ping"), headers={"Accept": "application/xml"})
        self.assertEqual(status, 406)

    def test_an_absent_accept_is_tolerated(self):
        # A hand-written client that states no preference has not asked for
        # anything this endpoint cannot give it.
        status, _, _ = self.post(self.call(1, "ping"), headers={"Accept": None})
        self.assertEqual(status, 200)

    def test_a_protocol_version_we_do_not_speak_is_refused(self):
        status, _, body = self.post(
            self.call(1, "ping"), headers={http_server.VERSION_HEADER: "1999-01-01"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], -32600)

    def test_a_protocol_version_we_do_speak_is_forwarded(self):
        for version in PROTOCOL_VERSIONS:
            status, _, _ = self.post(
                self.call(1, "ping"),
                headers={http_server.VERSION_HEADER: version})
            self.assertEqual(status, 200, version)

    def test_a_session_header_is_ignored_rather_than_required(self):
        # This server is stateless: it never issues a session id, and a client
        # that has one from somewhere else is not thereby wrong.
        status, _, _ = self.post(self.call(1, "ping"),
                                 headers={"Mcp-Session-Id": "abc123"})
        self.assertEqual(status, 200)


class TestOrigin(Served):
    """A page in the browser can reach a loopback port, so it is checked."""

    def test_a_page_on_another_site_is_refused(self):
        status, _, _ = self.post(self.call(1, "ping"),
                                 headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_a_page_on_this_machine_is_allowed(self):
        for origin in ("http://localhost:3000", "http://127.0.0.1:8080",
                       "http://[::1]:5173"):
            status, _, _ = self.post(self.call(1, "ping"),
                                     headers={"Origin": origin})
            self.assertEqual(status, 200, origin)

    def test_an_origin_named_on_the_command_line_is_allowed(self):
        # What makes the server usable behind a proxy or from a container,
        # without editing code and without turning the check off.
        httpd, port, _ = self.serve(
            Server(missing="no index"), allow_origins=["https://ci.example"])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("POST", http_server.MCP_PATH,
                         body=json.dumps(self.call(1, "ping")),
                         headers={"Content-Type": http_server.JSON_TYPE,
                                  "Origin": "https://ci.example"})
            self.assertEqual(conn.getresponse().status, 200)
        finally:
            conn.close()

    def test_a_null_origin_is_refused(self):
        # A sandboxed iframe or a `file://` page sends `Origin: null`, which
        # is every bit as foreign as a named site.
        status, _, _ = self.post(self.call(1, "ping"),
                                 headers={"Origin": "null"})
        self.assertEqual(status, 403)


class TestTheLogSink(Served):
    """The sink is a plain function being carried, not behaviour of the handler.

    Everything assigned in a class body is a method once it is reached through
    the instance, so a sink stored there and called as `self.log_sink(line)` is
    handed the handler as its first argument and raises on the first request.
    The other cases here pass no sink, which is exactly why this one does.
    """

    def test_a_sink_is_called_with_the_line_and_nothing_else(self):
        said = []

        def sink(line):  # a plain function, like the CLI's `_progress`
            said.append(line)

        _, port, _ = self.serve(Server(missing="no index"), log=sink)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("POST", http_server.MCP_PATH,
                         body=json.dumps(self.call(1, "ping")),
                         headers={"Content-Type": http_server.JSON_TYPE})
            self.assertEqual(conn.getresponse().status, 200)
        finally:
            conn.close()
        self.assertTrue(any("POST" in line for line in said), said)

    def test_a_notification_logs_too(self):
        # 202 goes down the same path as 200 - `send_response` is what logs -
        # so a sink that breaks only the silent replies would still break every
        # client, which sends one as soon as it connects.
        said = []

        def sink(line):
            said.append(line)

        _, port, _ = self.serve(Server(missing="no index"), log=sink)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("POST", http_server.MCP_PATH,
                         body=json.dumps({"jsonrpc": "2.0",
                                          "method": "notifications/initialized"}),
                         headers={"Content-Type": http_server.JSON_TYPE})
            self.assertEqual(conn.getresponse().status, 202)
        finally:
            conn.close()
        self.assertTrue(any("202" in line for line in said), said)


class TestTheStoreContract(unittest.TestCase):
    """The HTTP transport and the store have to agree about threads."""

    def test_a_store_opened_for_one_thread_is_refused_at_startup(self):
        # Serving it anyway would answer every request with a sqlite error
        # about threads, which reads as a fault in the query layer and sends
        # the reader looking in the wrong module.
        with tempfile.TemporaryDirectory() as directory:
            store = store_module.Store(Path(directory) / "index.db")
            self.addCleanup(store.close)
            with self.assertRaises(ValueError) as caught:
                make_server(Server(store=store), port=0)
            self.assertIn("cross_thread=True", str(caught.exception))

    def test_the_flag_is_remembered_on_the_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.db"
            for flag in (False, True):
                store = store_module.Store(path, cross_thread=flag)
                self.assertEqual(store.cross_thread, flag)
                store.close()


class TestConcurrency(unittest.TestCase):
    """Many handler threads, one store behind them.

    This deliberately builds a real store rather than the missing-index server
    the other cases use: a server without an index never opens a connection, so
    a test written against one would pass while proving nothing about the
    arrangement this class is named after.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = store_module.Store(Path(self._tmp.name) / "index.db",
                                        cross_thread=True)
        self.addCleanup(self.store.close)
        self.httpd = make_server(
            Server(query=Query(self.store), store=self.store),
            host="127.0.0.1", port=0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)

    def test_requests_on_many_threads_all_reach_the_store(self):
        # The handler threads are not the thread that opened the connection, so
        # a store that was not opened for cross-thread use answers every one of
        # these with a sqlite ProgrammingError - which under a pipelining client
        # would show up as an occasional 500 rather than as a rule.
        results = []
        collected = threading.Lock()

        def ask():
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            try:
                conn.request("POST", http_server.MCP_PATH,
                             body=json.dumps({"jsonrpc": "2.0", "id": 1,
                                              "method": "tools/call",
                                              "params": {"name": "get_index_status",
                                                         "arguments": {}}}),
                             headers={"Content-Type": http_server.JSON_TYPE})
                response = conn.getresponse()
                with collected:
                    results.append((response.status, response.read()))
            finally:
                conn.close()

        threads = [threading.Thread(target=ask) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(results), 8)
        for status, body in results:
            self.assertEqual(status, 200)
            payload = json.loads(json.loads(body)["result"]["content"][0]["text"])
            self.assertFalse(json.loads(body)["result"].get("isError"), payload)


@unittest.skipIf(streamable_http_client is None,
                 "the mcp SDK's HTTP client is not installed")
@unittest.skipIf(EXTRACTOR is None, "astroclang-index has not been built")
class TestWithTheReferenceClient(Corpus):
    """The whole thing over a real socket, under the real MCP client.

    Everything above tests this server against its own idea of HTTP.  This is
    the test that would fail if that idea were wrong.
    """

    def setUp(self):
        super().setUp()
        from astroclang.mcp_server import open_index
        # `cross_thread` is what the CLI passes for --http, and is the promise
        # that make_server's lock is the other half of.
        self.httpd = make_server(
            open_index(self.store.path, root=self.q.root, cross_thread=True),
            host="127.0.0.1", port=0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.url = f"http://127.0.0.1:{self.port}{http_server.MCP_PATH}"

    def test_a_client_can_handshake_list_and_call(self):
        async def session():
            async with streamable_http_client(self.url) as (read, write, _):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    listed = await client.list_tools()
                    status = await client.call_tool("get_index_status", {})
                    callers = await client.call_tool(
                        "get_callers", {"symbol": "geo::Circle::area"})
                    return listed, status, callers

        listed, status, callers = asyncio.run(session())

        self.assertIn("get_callers", {t.name for t in listed.tools})
        # The payload, not just the flag: a tool that fails here has said why,
        # and a bare "True is not false" throws that away.
        self.assertFalse(status.isError, status.content[0].text)
        self.assertEqual(json.loads(status.content[0].text)["accuracy"],
                         "exact")
        self.assertFalse(callers.isError)
        found = json.loads(callers.content[0].text)
        self.assertIn("app::measure_circle",
                      {c["symbol"] for c in found["callers"]})


if __name__ == "__main__":
    unittest.main()
