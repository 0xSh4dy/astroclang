"""The index, reachable by a coding agent: MCP over stdio.

The Model Context Protocol is JSON-RPC 2.0 with a small vocabulary, and the
stdio transport is one JSON message per line.  That is the whole of it, which
is why this module speaks it directly instead of taking on a framework: the
protocol is smaller than the dependency, and the failure modes that matter here
- stdout carrying something that is not a message, a tool that dies mid-call -
are easier to see in code that has nowhere else to put them.

Two rules the transport imposes, and they are easy to break by accident:

* stdout belongs to the protocol.  Anything else written there - a log line, a
  warning from a library, a stray print - corrupts the stream, so diagnostics
  go to stderr.
* a notification has no id and gets no reply.  Answering one is not a protocol
  error the client will tolerate kindly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, TextIO

from . import __version__, tools
from .query import Query
from .store import Store

SERVER_NAME = "astroclang"

# Newest first.  The client proposes a version, and gets its own back when it is
# one of these; otherwise it gets the newest, which is the protocol's way of
# saying "this is what I speak, decide whether you can".
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = PROTOCOL_VERSIONS[0]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = (
    "A semantic index of this C/C++ project, built with Clang from the "
    "project's own compilation database. Ask it rather than reading files: "
    "symbols are resolved to declarations, so an overload or a same-named "
    "method on another class is never confused with the one you meant. Every "
    "symbol in a result is named by `file:line`, and that spelling is accepted "
    "back as an argument in place of a name. If an answer looks empty or "
    "surprising, call get_index_status: the index may not cover that file, or "
    "may have been built without a compilation database, in which case some "
    "declarations are missing and the absence is not a fact about the code."
)


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _dumps(payload: Any) -> str:
    """Compact JSON: the reader is paying for the whitespace too."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _text_content(payload: Any, is_error: bool = False) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "content": [{"type": "text", "text": _dumps(payload)}],
    }
    if is_error:
        out["isError"] = True
    return out


def _error(msg_id: Any, code: int, message: str, data: Any = None
           ) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


class Server:
    """One connection's worth of state.

    Holds the query layer, or nothing when there is no index yet - the server
    still starts and still answers `tools/list`, because an agent that cannot
    see the tools has no way to learn why nothing works.
    """

    def __init__(self, query: Optional[Query] = None,
                 store: Optional[Store] = None,
                 missing: str = "",
                 name: str = SERVER_NAME,
                 version: str = __version__,
                 log=None):
        self.query = query
        self.store = store
        self.missing = missing
        self.name = name
        self.version = version
        self.log = log

    # -- protocol ------------------------------------------------------------

    def handle(self, message: Any) -> Optional[Dict[str, Any]]:
        """Answer one message.  Returns None for a notification."""
        if isinstance(message, list):
            return _error(None, INVALID_REQUEST,
                          "batches are not supported by this transport")
        if not isinstance(message, dict):
            return _error(None, INVALID_REQUEST,
                          "a message must be a JSON object")

        msg_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str) or not method:
            return _error(msg_id, INVALID_REQUEST, "no method in message")
        if message.get("jsonrpc") not in (None, "2.0"):
            return _error(msg_id, INVALID_REQUEST,
                          f"unsupported jsonrpc version "
                          f"{message.get('jsonrpc')!r}")

        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error(msg_id, INVALID_PARAMS, "params must be an object")

        if msg_id is None:
            # A notification.  Nothing to do for any of the ones a client
            # sends here, and nothing to send back either way.
            return None

        try:
            result = self._request(method, params)
        except RpcError as exc:
            return _error(msg_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            if self.log:
                self.log(f"{type(exc).__name__} in {method}: {exc}")
            return _error(msg_id, INTERNAL_ERROR,
                          f"{type(exc).__name__}: {exc}")
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": tools.describe()}
        if method == "tools/call":
            return self._call_tool(params)
        raise RpcError(METHOD_NOT_FOUND, f"unknown method: {method}")

    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        requested = params.get("protocolVersion")
        version = (requested if requested in PROTOCOL_VERSIONS
                   else DEFAULT_PROTOCOL)
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": self.version},
            "instructions": INSTRUCTIONS,
        }

    def _call_tool(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise RpcError(INVALID_PARAMS, "tools/call requires a tool name")
        if self.query is None:
            return _text_content(
                {"error": self.missing or "no index is open",
                 "hint": "run `astroclang index` and retry"}, is_error=True)

        arguments = params.get("arguments")
        try:
            payload = tools.call(self.query, name, arguments)
        except tools.ToolError as exc:
            # The question could not be asked - an index that has not seen the
            # file, a name that denotes three symbols.  The payload says which,
            # so the agent can correct the call instead of the code.
            return _text_content(exc.payload, is_error=True)
        except Exception as exc:  # noqa: BLE001
            if self.log:
                self.log(f"{type(exc).__name__} in {name}: {exc}")
            return _text_content(
                {"error": f"{type(exc).__name__}: {exc}", "tool": name},
                is_error=True)
        return _text_content(payload)

    # -- transport -----------------------------------------------------------

    def serve(self, instream: TextIO, outstream: TextIO) -> int:
        """Read messages until the stream ends.  Returns an exit status."""
        for line in instream:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                # Nothing to correlate with: the id, if there was one, is in
                # the text that failed to parse.
                self._write(outstream, _error(None, PARSE_ERROR,
                                              f"invalid JSON: {exc}"))
                continue
            response = self.handle(message)
            if response is not None and not self._write(outstream, response):
                return 0
        return 0

    def _write(self, outstream: TextIO, payload: Dict[str, Any]) -> bool:
        try:
            outstream.write(_dumps(payload))
            outstream.write("\n")
            outstream.flush()
        except (BrokenPipeError, ValueError):
            # The client went away.  That is an ordinary end to a session, not
            # a failure to report to a client that is no longer listening.
            return False
        return True


def open_index(db_path, root=None, missing: str = "", log=None,
               cross_thread: bool = False) -> Server:
    """A server for an existing index, or one that explains its absence.

    `log` is where an unexpected exception goes on its way out.  It is passed
    through rather than defaulted here because the server never writes to
    stderr itself: stdout is the protocol, and a library that decided on its
    own where a diagnostic belongs would have made that choice for its host.

    `cross_thread` is for a transport that answers on more than one thread, and
    it is the caller's promise to serialise: it says the store may be reached
    from a thread other than this one, not that reaching it is safe.
    """
    path = Path(db_path)
    if not path.is_file():
        return Server(missing=missing or (
            f"there is no index at {path}; build one with "
            f"`astroclang index`"), log=log)
    store = Store(path, project_root=Path(root) if root else None,
                  cross_thread=cross_thread)
    return Server(query=Query(store), store=store, log=log)


def serve_stdio(server: Server) -> int:
    """Serve one connection on stdin and stdout.

    The encoding is set explicitly because Python takes it from the locale, and
    a POSIX locale means ASCII: a source file with one accented character in a
    comment would then break the stream it is quoted into.  JSON is UTF-8.
    """
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # pragma: no cover - not a tty
            pass
    return server.serve(sys.stdin, sys.stdout)
