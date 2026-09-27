"""The index over HTTP: the MCP Streamable HTTP transport.

stdio is the transport for a client that can spawn a process, which is most of
them and is why it came first.  It is the wrong one when the client cannot: a
host in another container, an editor on another machine, one client holding a
connection to several servers.  Streamable HTTP is the protocol's answer, and
this is that transport, written against the standard library for the same
reason the stdio one is - the protocol is smaller than the dependency.

What the transport asks for, since the specification is easy to read past:

* one endpoint.  A POST carries a message in and the server answers either with
  `application/json` or with an SSE stream.  This server has nothing to push
  and one reply per request, so a single JSON document is both the simplest
  legal answer and the complete one.
* a POST carrying no request - a notification, or a reply to something the
  server asked - is answered `202 Accepted` with no body.  Sending a JSON-RPC
  message back would be a message the client never asked for, and some clients
  treat it as a protocol error.
* GET and DELETE belong to the server-initiated stream and to ending a session.
  Neither exists here, and the specification says how to say so: `405` with an
  `Allow` header, which is what a client is entitled to act on.
* `Origin` is checked.  A server on a loopback port is reachable by any page the
  browser has open, so an unchecked one is a DNS-rebinding hole; a page that is
  not ours is refused.

Concurrency is the one place this is more than a wrapper.  `Store` holds a
single `sqlite3` connection and fills caches as it answers, so the connection
is opened for cross-thread use and then guarded rather than shared - see
`Serve.lock`.  Every question answered here is an indexed read over a local
file, so the wait a lock imposes is measured in microseconds, and it buys the
removal of a class of bug that would otherwise appear only under a client that
pipelines.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from . import __version__
# `_dumps` and `_error` are this package's own framing.  Importing them rather
# than repeating the separators keeps the two transports byte-identical instead
# of merely similar, which is what a client that speaks both is entitled to.
from .mcp_server import (INVALID_REQUEST, PARSE_ERROR, PROTOCOL_VERSIONS,
                         Server, _dumps, _error)

MCP_PATH = "/mcp"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

JSON_TYPE = "application/json"
SSE_TYPE = "text/event-stream"
TEXT_TYPE = "text/plain; charset=utf-8"

# The header a client sends once it has negotiated a version.  Missing means an
# older client, which the specification says to read as the 2025-03-26 revision
# - earlier than anything this server would negotiate, so it is accepted and
# takes the same path as everything else.
VERSION_HEADER = "MCP-Protocol-Version"

# Names that mean "the machine the server is running on".  A browser page served
# from any of these is still the user's own machine talking to itself.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# A sentinel rather than None: a body that parses to JSON `null` is a perfectly
# valid thing to receive, and must not be confused with "already answered".
_REFUSED = object()

OK = 0
FAILED = 1


class _Handler(BaseHTTPRequestHandler):
    """One request.

    The class is built per server by `make_server`, so the attributes below are
    supply lines rather than class state: two servers in one process must not be
    able to see each other's index.
    """

    protocol_version = "HTTP/1.1"
    server_version = f"astroclang/{__version__}"
    sys_version = ""  # the header names this server, not the Python under it

    path_url = MCP_PATH
    mcp: Server
    lock: threading.Lock
    allowed_origins: Tuple[str, ...] = ()
    log_sink: Optional[Callable[[str], None]] = None

    # -- methods -------------------------------------------------------------

    def do_POST(self) -> None:
        if not self._routed():
            return
        if not self._origin_ok():
            return
        message = self._read_message()
        if message is _REFUSED:
            return
        with self.lock:
            # One at a time: the connection behind this server belongs to the
            # thread that opened it, and the query layer caches as it reads.
            response = self.mcp.handle(message)
        if response is None:
            # A notification, or a reply to a server-initiated request.  There
            # is nothing to say back, which is exactly what 202 means.
            self._respond(202)
            return
        self._respond(200, _dumps(response).encode("utf-8"), JSON_TYPE)

    def do_GET(self) -> None:
        self._refuse("GET")

    def do_DELETE(self) -> None:
        self._refuse("DELETE")

    # -- the checks, in the order a request meets them -----------------------

    def _routed(self) -> bool:
        if urlsplit(self.path).path == self.path_url:
            return True
        # Anything else is not this server's endpoint.  The path is compared
        # without its query string, so a client that appends one still arrives.
        self._respond(404, b"no MCP endpoint here\n", TEXT_TYPE)
        return False

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            # A native client - the SDK, curl - sends no Origin.  Only a browser
            # does, and only a browser is the attack.
            return True
        if origin in self.allowed_origins:
            return True
        parts = urlsplit(origin)
        if parts.scheme in ("http", "https") and parts.hostname in LOOPBACK_HOSTS:
            return True
        self._respond(403, b"this Origin is not allowed to reach this server\n",
                      TEXT_TYPE)
        return False

    def _refuse(self, method: str) -> None:
        """Answer a method the transport reserves for something we do not do."""
        if not self._routed():
            return
        self._respond(405,
                      f"this server offers no {method} stream; "
                      f"POST a message instead\n".encode("utf-8"),
                      TEXT_TYPE, allow="POST")

    def _read_message(self) -> Any:
        """The parsed body, or `_REFUSED` when a reply has already been sent."""
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype and ctype != JSON_TYPE:
            self._respond(415, f"a message must be {JSON_TYPE}\n".encode("utf-8"),
                          TEXT_TYPE)
            return _REFUSED

        accept = self.headers.get("Accept", "")
        # Checked only when present: a hand-written client that omits it is
        # asking for whatever this server sends, and refusing it would be
        # pedantry.  A client that states a preference and excludes both types
        # has asked for something this endpoint does not produce.
        if accept and JSON_TYPE not in accept and SSE_TYPE not in accept:
            self._respond(406, f"this endpoint answers {JSON_TYPE}\n".encode("utf-8"),
                          TEXT_TYPE)
            return _REFUSED

        version = self.headers.get(VERSION_HEADER)
        if version and version not in PROTOCOL_VERSIONS:
            self._respond(400, _dumps(_error(
                None, INVALID_REQUEST,
                f"unsupported {VERSION_HEADER}: {version}")).encode("utf-8"),
                JSON_TYPE)
            return _REFUSED

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            self._respond(400, b"Content-Length is not a number\n", TEXT_TYPE)
            return _REFUSED
        if length == 0:
            self._respond(400, b"a POST must carry a message\n", TEXT_TYPE)
            return _REFUSED

        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # 400 rather than a JSON-RPC error carrying a null id: there is no
            # id to correlate with, so the only useful thing to say is that
            # nothing was understood.
            self._respond(400, _dumps(_error(
                None, PARSE_ERROR, f"invalid JSON: {exc}")).encode("utf-8"),
                JSON_TYPE)
            return _REFUSED

    # -- writing -------------------------------------------------------------

    def _respond(self, status: int, body: bytes = b"",
                 ctype: Optional[str] = None,
                 allow: Optional[str] = None) -> None:
        self.send_response(status)
        if ctype:
            self.send_header("Content-Type", ctype)
        if allow:
            self.send_header("Allow", allow)
        # Always declared, because HTTP/1.1 keeps the connection open and a
        # response without a length leaves the client waiting for a body that
        # will never arrive.
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    # -- logging -------------------------------------------------------------

    def log_request(self, code: Any = "-", size: int = -1) -> None:
        if self.log_sink:
            self.log_sink(f"{self.address_string()} {self.command} "
                          f"{self.path} -> {code}")

    def log_message(self, fmt: str, *args: Any) -> None:
        # The base class writes to stderr directly.  Routing it through the
        # sink keeps every diagnostic on one path, which is what makes the
        # stdio transport's promise - nothing but protocol on stdout - hold
        # here too, and lets a host capture both.
        if self.log_sink:
            self.log_sink(f"{self.address_string()} {fmt % args}")


class _Server(ThreadingHTTPServer):
    daemon_threads = True      # one client holding a thread cannot pin the exit
    allow_reuse_address = True


def make_server(server: Server, host: str = DEFAULT_HOST,
                port: int = DEFAULT_PORT, path: str = MCP_PATH,
                allow_origins: Sequence[str] = (),
                log: Optional[Callable[[str], None]] = None) -> ThreadingHTTPServer:
    """A bound HTTP server, not yet serving.

    Split from `serve_http` so a test can bind port 0 and drive the result
    without starting a loop it then has to stop from another thread.  `port=0`
    asks the operating system for a free port; read back what it chose from
    `server_address`.
    """
    store = server.store
    if store is not None and not getattr(store, "cross_thread", False):
        # Every request would fail with a sqlite error from another thread,
        # which reads as a bug in the query layer.  Refusing here says what is
        # actually wrong, while whoever built the server is still listening.
        raise ValueError(
            "this index was opened for one thread; open it with "
            "cross_thread=True before serving it over HTTP")
    handler = type("_BoundHandler", (_Handler,), {
        "path_url": path,
        "mcp": server,
        "lock": threading.Lock(),
        "allowed_origins": tuple(allow_origins),
        # `staticmethod` because anything assigned in a class body is a method
        # by the time it is reached through the instance, and `self.log_sink(x)`
        # would then call it with the handler as the first argument.  This is a
        # function being carried to the handler, not behaviour of it.
        "log_sink": staticmethod(log) if log is not None else None,
    })
    return _Server((host, port), handler)


def serve_http(server: Server, host: str = DEFAULT_HOST,
               port: int = DEFAULT_PORT, path: str = MCP_PATH,
               allow_origins: Sequence[str] = (),
               log: Optional[Callable[[str], None]] = None) -> int:
    """Serve until interrupted.  Returns a process exit status."""
    try:
        httpd = make_server(server, host=host, port=port, path=path,
                            allow_origins=allow_origins, log=log)
    except OSError as exc:
        # A port already in use is the common case and deserves the reason,
        # not a traceback: nothing is wrong with the program.
        if log:
            log(f"cannot listen on {host}:{port}: {exc}")
        return FAILED
    if log:
        bound_host, bound_port = httpd.server_address[:2]
        log(f"listening on http://{bound_host}:{bound_port}{path}")
        log("point a client at that URL; diagnostics stay here on stderr")
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
    return OK
