"""Run the real ASGI apps under uvicorn on a free localhost port for integration tests."""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

import httpx
import uvicorn


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    def __init__(self, app: Any, port: int):
        self.port = port
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "LiveServer":
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def parse_rpc(response: httpx.Response) -> dict:
    """A Streamable HTTP response is either one JSON object or an SSE stream."""
    ctype = response.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                msg = json.loads(payload)
                if "result" in msg or "error" in msg:
                    return msg
    raise AssertionError(f"no JSON-RPC response in {ctype}: {response.text[:200]}")


PROTOCOL = "2025-11-25"
ACCEPT = "application/json, text/event-stream"


class RawMcp:
    """Plain-HTTP MCP client: every header is visible to the test."""

    def __init__(self, base: str):
        self.http = httpx.Client(base_url=base, timeout=15)
        self.session_id: str | None = None
        self.next_id = 1
        self.sent_headers: list[dict] = []

    def headers(self, protocol: str | None = PROTOCOL) -> dict:
        h = {"Accept": ACCEPT, "Content-Type": "application/json"}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if protocol:
            h["MCP-Protocol-Version"] = protocol
        return h

    def initialize(self) -> dict:
        init = self.http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 0, "method": "initialize",
                  "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                             "clientInfo": {"name": "raw-test-client", "version": "1.0"}}},
            headers={"Accept": ACCEPT, "Content-Type": "application/json"},
        )
        assert init.status_code == 200, init.text
        self.session_id = init.headers.get("mcp-session-id")
        note = self.http.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                              headers=self.headers())
        assert note.status_code == 202
        return parse_rpc(init)["result"]

    def request(self, method: str, params: dict | None = None, protocol: str | None = PROTOCOL) -> httpx.Response:
        body = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        self.next_id += 1
        headers = self.headers(protocol)
        self.sent_headers.append(headers)
        return self.http.post("/mcp", json=body, headers=headers)

    def call(self, tool: str, arguments: dict) -> dict:
        resp = self.request("tools/call", {"name": tool, "arguments": arguments})
        assert resp.status_code == 200, resp.text
        msg = parse_rpc(resp)
        assert "result" in msg, msg
        return msg["result"]

    def ok(self, tool: str, arguments: dict) -> dict:
        result = self.call(tool, arguments)
        assert not result.get("isError"), result["content"]
        return result["structuredContent"]

    def error(self, tool: str, arguments: dict) -> str:
        result = self.call(tool, arguments)
        assert result.get("isError"), f"{tool} unexpectedly succeeded"
        text = result["content"][0]["text"]
        return text.split(": ", 2)[1] if text.startswith("Error executing tool") else text.split(":", 1)[0]
