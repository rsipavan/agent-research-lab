"""Thin client for the TradingView MCP server.

validate.py talks to market data only through this. The client exposes one method —
`call(tool_name, args) -> dict` — with config-driven retries. On any failure it
raises McpError, which validate.py catches and turns into an `error` ValidationRun
(never a silent skip, never a hard crash of the run).

Transport: two options.
  1. HTTP/SSE — set TRADINGVIEW_MCP_URL in .env to the server's URL. Uses httpx.
  2. stdio — leave TRADINGVIEW_MCP_URL empty; requires the `mcp` Python SDK and a
     command to launch the server (configure in code below). Optional in v1.

If neither is available, call() raises McpError with a clear setup message — the
repo still installs and imports fine; you just can't run validations until the MCP
is wired up. See docs/validation_logic.md.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .config import Config

try:  # pragma: no cover
    import httpx

    _HAVE_HTTPX = True
except Exception:  # pragma: no cover
    _HAVE_HTTPX = False


class McpError(RuntimeError):
    """A TradingView MCP call failed (transport, timeout, server error, not configured)."""


class McpClient:
    """Use as a context manager so connections are cleaned up:

        with McpClient(config) as mcp:
            mcp.call("symbol_search", {"query": "SPX"})
    """

    def __init__(self, config: Config):
        self._config = config
        self._url = config.tradingview_mcp_url
        self._http: Any = None
        self._request_id = 0

    # -- context manager --

    def __enter__(self) -> "McpClient":
        if self._url:
            if not _HAVE_HTTPX:  # pragma: no cover
                raise McpError("httpx not installed — needed for the HTTP MCP transport")
            self._http = httpx.Client(base_url=self._url, timeout=self._config.mcp_timeout_seconds)
        # stdio transport intentionally left as a TODO for v1 — see module docstring.
        return self

    def __exit__(self, *exc) -> None:
        if self._http is not None:
            try:
                self._http.close()
            except Exception:  # pragma: no cover
                pass
            self._http = None

    # -- the one method everything uses --

    def call(self, tool_name: str, args: dict | None = None) -> dict:
        """Call an MCP tool. Returns the tool's result as a dict. Retries per
        config.mcp_retries on failure, then raises McpError."""
        args = args or {}
        last_err: Exception | None = None
        attempts = 1 + max(0, self._config.mcp_retries)
        for attempt in range(attempts):
            try:
                return self._call_once(tool_name, args)
            except McpError:
                raise  # configuration errors aren't worth retrying
            except Exception as e:  # noqa: BLE001 - transport-level; retry
                last_err = e
                if attempt < attempts - 1:
                    time.sleep(1.5)
        raise McpError(f"MCP call '{tool_name}' failed after {attempts} attempt(s): {last_err}")

    # -- transport --

    def _call_once(self, tool_name: str, args: dict) -> dict:
        if self._http is not None:
            return self._call_http(tool_name, args)
        raise McpError(
            "TradingView MCP not configured. Set TRADINGVIEW_MCP_URL in .env to the MCP "
            "server's HTTP/SSE endpoint (or implement the stdio transport in mcp_client.py). "
            "See docs/validation_logic.md."
        )

    def _call_http(self, tool_name: str, args: dict) -> dict:
        """JSON-RPC over HTTP, MCP `tools/call`. Tolerant of the common response shapes."""
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": args},
        }
        resp = self._http.post("/", json=payload)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body and body["error"]:
            raise McpError(f"MCP server error for '{tool_name}': {body['error']}")
        result = body.get("result", {})
        # MCP tool results come back as {"content": [{"type": "text", "text": "..."} | {"type": "json", ...}]}
        # — normalize to a dict.
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
                        return json.loads(txt)
                    except json.JSONDecodeError:
                        return {"text": txt}
        if isinstance(result, dict):
            return result
        return {"result": result}
