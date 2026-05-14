"""Thin client for the TradingView MCP server.

`validate.py` talks to market data only through this. The client exposes one method —
`call(tool_name, args) -> dict` — with config-driven retries. On any failure it raises
McpError, which `validate.py` catches and turns into an `error` ValidationRun (never a
silent skip, never a hard crash of the run).

Two transports, picked from config:
  1. **stdio** (the common case) — set TRADINGVIEW_MCP_CMD to the command that launches
     the MCP server, e.g. `node C:/path/to/tradingview-mcp/src/server.js`. We spawn it,
     do the MCP `initialize` handshake over stdin/stdout (line-delimited JSON-RPC), then
     `tools/call`. No extra Python dependency. Note: the TradingView MCP drives the
     TradingView Desktop app — that app must be running (with CDP enabled) for data calls
     to succeed; if it isn't, the MCP returns an error and that becomes an `untestable`
     verdict, handled, not crashed.
  2. **HTTP/SSE** — set TRADINGVIEW_MCP_URL to the server's HTTP endpoint. Uses httpx.

If neither is set, `call()` raises McpError with a clear setup message. The repo still
installs and imports fine; you just can't run validations until the MCP is wired up.
See docs/validation_logic.md.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import threading
import time
from typing import Any

from .config import Config

try:  # pragma: no cover
    import httpx

    _HAVE_HTTPX = True
except Exception:  # pragma: no cover
    _HAVE_HTTPX = False

_INIT_TIMEOUT = 30   # seconds — the MCP server may take a moment to connect to TV Desktop
_CALL_TIMEOUT = 60   # seconds per tool call


class McpError(RuntimeError):
    """A TradingView MCP call failed (transport, timeout, server error, not configured)."""


# ---------------------------------------------------------------------------
# public client
# ---------------------------------------------------------------------------


class McpClient:
    """Use as a context manager so the transport is cleaned up:

        with McpClient(config) as mcp:
            mcp.call("symbol_search", {"query": "SPX"})
    """

    def __init__(self, config: Config):
        self._config = config
        self._transport: _Transport | None = None

    def __enter__(self) -> "McpClient":
        if self._config.tradingview_mcp_url:
            if not _HAVE_HTTPX:  # pragma: no cover
                raise McpError("httpx not installed — needed for the HTTP MCP transport")
            self._transport = _HttpTransport(self._config.tradingview_mcp_url, self._config.mcp_timeout_seconds)
        elif self._config.tradingview_mcp_cmd:
            self._transport = _StdioTransport(self._config.tradingview_mcp_cmd, self._config.mcp_timeout_seconds)
        # else: no transport — call() will raise the setup error.
        if self._transport is not None:
            self._transport.open()
        return self

    def __exit__(self, *exc) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def call(self, tool_name: str, args: dict | None = None) -> dict:
        """Call an MCP tool. Returns the tool's result as a dict. Retries per
        config.mcp_retries on transport-level failure, then raises McpError."""
        args = args or {}
        if self._transport is None:
            raise McpError(
                "TradingView MCP not configured. Set TRADINGVIEW_MCP_CMD in .env to the command that "
                "launches the MCP server (e.g. `node /path/to/tradingview-mcp/src/server.js`), or "
                "TRADINGVIEW_MCP_URL for an HTTP endpoint. See docs/validation_logic.md."
            )
        last_err: Exception | None = None
        attempts = 1 + max(0, self._config.mcp_retries)
        for attempt in range(attempts):
            try:
                return self._transport.call(tool_name, args)
            except McpError:
                raise  # configuration / server errors aren't worth retrying
            except Exception as e:  # noqa: BLE001 - transport-level; retry
                last_err = e
                if attempt < attempts - 1:
                    time.sleep(1.5)
        raise McpError(f"MCP call '{tool_name}' failed after {attempts} attempt(s): {last_err}")


# ---------------------------------------------------------------------------
# transports
# ---------------------------------------------------------------------------


class _Transport:
    def open(self) -> None: ...
    def close(self) -> None: ...
    def call(self, tool_name: str, args: dict) -> dict: raise NotImplementedError


class _StdioTransport(_Transport):
    """Spawn the MCP server and speak line-delimited JSON-RPC over its stdin/stdout."""

    def __init__(self, cmd: str, timeout: int):
        self._cmd = cmd
        self._timeout = timeout or _CALL_TIMEOUT
        self._proc: subprocess.Popen | None = None
        self._next_id = 0
        self._lock = threading.Lock()

    def open(self) -> None:
        try:
            self._proc = subprocess.Popen(
                shlex.split(self._cmd, posix=False),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", bufsize=1,  # line-buffered
            )
        except FileNotFoundError as e:
            raise McpError(f"could not launch the MCP server (`{self._cmd}`): {e}") from e
        # MCP handshake: initialize -> wait for result -> notifications/initialized
        self._send({
            "jsonrpc": "2.0", "id": self._mint_id(), "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "trading-hypothesis-lab", "version": "0.1.0"},
            },
        })
        init = self._read_until_response(self._last_id, timeout=_INIT_TIMEOUT)
        if "error" in init and init["error"]:
            raise McpError(f"MCP initialize failed: {init['error']}")
        self._send_notification("notifications/initialized")

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        except Exception:  # pragma: no cover
            pass
        self._proc = None

    def call(self, tool_name: str, args: dict) -> dict:
        if self._proc is None or self._proc.poll() is not None:
            raise McpError("MCP server process is not running")
        with self._lock:
            self._send({
                "jsonrpc": "2.0", "id": self._mint_id(), "method": "tools/call",
                "params": {"name": tool_name, "arguments": args},
            })
            resp = self._read_until_response(self._last_id, timeout=self._timeout)
        if "error" in resp and resp["error"]:
            raise McpError(f"MCP server error for '{tool_name}': {resp['error']}")
        return _normalize_tool_result(resp.get("result", {}))

    # -- low-level --

    def _mint_id(self) -> int:
        self._next_id += 1
        self._last_id = self._next_id
        return self._next_id

    def _send(self, obj: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(obj) + "\n")
        self._proc.stdin.flush()

    def _send_notification(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _read_until_response(self, request_id: int, *, timeout: int) -> dict:
        """Read stdout lines until we see the JSON-RPC response with the given id.
        Non-JSON lines (server logging noise) and unrelated messages are skipped."""
        assert self._proc and self._proc.stdout
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self._proc.stdout.readline()
            if line == "":  # EOF — server died
                raise McpError("MCP server closed the connection (process exited)")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # logging line on stdout — ignore
            if isinstance(msg, dict) and msg.get("id") == request_id and ("result" in msg or "error" in msg):
                return msg
            # else: a notification, or a response to a different id — keep reading
        raise McpError(f"timed out after {timeout}s waiting for MCP response to request {request_id}")


class _HttpTransport(_Transport):
    """JSON-RPC over HTTP, MCP `tools/call`."""

    def __init__(self, url: str, timeout: int):
        self._url = url
        self._timeout = timeout or _CALL_TIMEOUT
        self._http: Any = None
        self._next_id = 0

    def open(self) -> None:
        self._http = httpx.Client(base_url=self._url, timeout=self._timeout)

    def close(self) -> None:
        if self._http is not None:
            try:
                self._http.close()
            except Exception:  # pragma: no cover
                pass
            self._http = None

    def call(self, tool_name: str, args: dict) -> dict:
        self._next_id += 1
        resp = self._http.post("/", json={
            "jsonrpc": "2.0", "id": self._next_id, "method": "tools/call",
            "params": {"name": tool_name, "arguments": args},
        })
        resp.raise_for_status()
        body = resp.json()
        if "error" in body and body["error"]:
            raise McpError(f"MCP server error for '{tool_name}': {body['error']}")
        return _normalize_tool_result(body.get("result", {}))


# ---------------------------------------------------------------------------
# response normalization (tolerant of the common MCP tool-result shapes)
# ---------------------------------------------------------------------------


def _normalize_tool_result(result) -> dict:
    """MCP tool results come back as {"content": [{"type":"text","text":"..."} | {"type":"json",...}], ...}.
    Normalize to a dict — parse JSON-in-text where present, fall back to {"text": ...} or the raw result."""
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "json" and "json" in block:
                return block["json"]
            if block.get("type") == "text" and "text" in block:
                txt = block["text"].strip()
                try:
                    parsed = json.loads(txt)
                    return parsed if isinstance(parsed, dict) else {"result": parsed}
                except json.JSONDecodeError:
                    return {"text": txt}
    if isinstance(result, dict):
        return result
    return {"result": result}
