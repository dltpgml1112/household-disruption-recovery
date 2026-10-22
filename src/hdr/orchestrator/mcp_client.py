"""MCP client used by the agent: the official SDK over Streamable HTTP, plus a wire trace.

Every HTTP exchange (initialize, tools/list, tools/call ...) is recorded from httpx
event hooks, so the simulator shows what actually went over the wire.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

ERROR_CODE = re.compile(r"\b([A-Z][A-Z_]{2,}): (.*)", re.S)

# Read-only tools are safe to retry. apply_recovery_plan is retried with the same
# idempotency key, so a retry can never run an action twice.
RETRYABLE = {"get_household_state", "verify_recovery", "get_audit_log", "apply_recovery_plan"}


class McpToolError(Exception):
    def __init__(self, tool: str, code: str, message: str):
        super().__init__(f"{tool}: {code}: {message}")
        self.tool, self.code, self.message = tool, code, message


class McpUnavailable(Exception):
    pass


@dataclass
class Trace:
    started: float = field(default_factory=time.perf_counter)
    wire: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def at(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)


class WireRecorder:
    """httpx event hooks that log every MCP HTTP exchange into a Trace."""

    def __init__(self, trace: Trace):
        self.trace = trace
        self._pending: dict[int, tuple[float, dict]] = {}

    def hooks(self) -> dict:
        return {"request": [self._on_request], "response": [self._on_response]}

    async def _on_request(self, request: httpx2.Request) -> None:
        info: dict[str, Any] = {"http": request.method}
        try:
            body = json.loads(request.content or b"{}")
            info["method"] = body.get("method")
            info["rpc_id"] = body.get("id")
            if body.get("method") == "tools/call":
                info["tool"] = (body.get("params") or {}).get("name")
            elif body.get("method") == "resources/read":
                info["tool"] = (body.get("params") or {}).get("uri")
        except Exception:
            info["method"] = None
        info["protocol_header"] = request.headers.get("mcp-protocol-version")
        info["session"] = (request.headers.get("mcp-session-id") or "")[:8] or None
        self._pending[id(request)] = (time.perf_counter(), info)

    async def _on_response(self, response: httpx2.Response) -> None:
        started, info = self._pending.pop(id(response.request), (time.perf_counter(), {}))
        if info.get("http") == "GET":
            info["method"] = "GET (server event stream)"
        elif info.get("http") == "DELETE":
            info["method"] = "DELETE (close session)"
        info.update(
            status=response.status_code,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            at_ms=self.trace.at(),
            session=info.get("session") or (response.headers.get("mcp-session-id") or "")[:8] or None,
        )
        self.trace.wire.append(info)


class McpSession:
    """One MCP session for one agent turn (initialize -> tools/list -> tool calls -> close)."""

    def __init__(self, url: str, trace: Trace, *, timeout_s: float = 8.0, retries: int = 2):
        self.url = url
        self.trace = trace
        self.timeout_s = timeout_s
        self.retries = retries
        self.protocol_version: str | None = None
        self.tools: list[str] = []
        self.tool_meta: dict[str, dict] = {}
        self._recorder = WireRecorder(trace)

    async def __aenter__(self) -> "McpSession":
        self._http = httpx2.AsyncClient(
            follow_redirects=True,
            timeout=httpx2.Timeout(10.0, read=self.timeout_s + 2),
            event_hooks=self._recorder.hooks(),
        )
        self._client = Client(
            streamable_http_client(self.url, http_client=self._http),
            mode="legacy",  # initialize handshake -> negotiates 2025-11-25
            read_timeout_seconds=self.timeout_s,
        )
        # The client opens long-lived task groups on enter, so it must not be wrapped in a
        # cancel scope; the SDK's read timeout bounds the initialize round trip instead.
        try:
            await self._client.__aenter__()
        except Exception as e:
            await self._http.aclose()
            raise McpUnavailable(f"cannot reach MCP server at {self.url}: {type(e).__name__}") from e
        try:
            self.protocol_version = self._client.session.protocol_version
            with anyio.fail_after(self.timeout_s):
                listed = await self._client.list_tools()
        except Exception as e:
            await self.__aexit__(None, None, None)
            raise McpUnavailable(f"MCP server at {self.url} did not list tools: {type(e).__name__}") from e
        self.tools = [t.name for t in listed.tools]
        self.tool_meta = {t.name: t.meta or {} for t in listed.tools}
        return self

    def ui_links(self) -> dict[str, str]:
        """Tool name -> MCP Apps view (`_meta.ui.resourceUri` on the tool definition)."""
        links = {}
        for name, meta in self.tool_meta.items():
            uri = (meta.get("ui") or {}).get("resourceUri")
            if uri:
                links[name] = uri
        return links

    async def read_resource(self, uri: str) -> str:
        with anyio.fail_after(self.timeout_s):
            result = await self._client.read_resource(uri)
        return "".join(getattr(c, "text", "") or "" for c in result.contents)

    async def __aexit__(self, *exc: Any) -> None:
        try:
            await self._client.__aexit__(*exc)
        finally:
            await self._http.aclose()

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        attempts = 1 + (self.retries if tool in RETRYABLE else 0)
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            try:
                with anyio.fail_after(self.timeout_s):
                    result = await self._client.call_tool(tool, arguments)
            except TimeoutError as e:
                last = e
                self.trace.calls.append({"tool": tool, "attempt": attempt, "ok": False, "error": "TIMEOUT",
                                         "ms": round((time.perf_counter() - started) * 1000)})
                continue
            ms = round((time.perf_counter() - started) * 1000)
            if result.is_error:
                text = " ".join(getattr(c, "text", "") for c in result.content)
                m = ERROR_CODE.search(text)
                code, message = (m.group(1), m.group(2)) if m else ("TOOL_ERROR", text)
                self.trace.calls.append({"tool": tool, "attempt": attempt, "ok": False, "error": code, "ms": ms})
                raise McpToolError(tool, code, message)
            self.trace.calls.append({"tool": tool, "attempt": attempt, "ok": True, "ms": ms})
            return result.structured_content or {}
        raise McpUnavailable(f"{tool} timed out after {attempts} attempt(s)") from last
