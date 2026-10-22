"""MCP 2025-11-25 over Streamable HTTP, checked on the wire with plain HTTP (no MCP SDK client).

initialize (2025-11-25) -> MCP-Protocol-Version on every later request -> tools/list ->
tools/call through the whole workflow -> real SQLite writes -> read-back verification.
"""

from __future__ import annotations

import sqlite3

import httpx
import pytest

from hdr.mcp_server.app import TOOL_NAMES, create_app
from hdr.service.recovery import RecoveryService, Settings

from tests.integration.live import ACCEPT, PROTOCOL, LiveServer, RawMcp, free_port, parse_rpc

pytestmark = pytest.mark.integration


@pytest.fixture
def server(tmp_path):
    db = tmp_path / "hdr.sqlite3"
    service = RecoveryService(Settings(db_path=str(db), approval_secret=b"compliance-secret-0123456789abcd"))
    service.reset()
    port = free_port()
    with LiveServer(create_app(service, port=port), port) as live:
        yield live, db, service


def test_streamable_http_2025_11_25_end_to_end(server):
    live, db, _ = server
    mcp = RawMcp(live.url)

    # 1. initialize: negotiate 2025-11-25
    init = mcp.http.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "raw-compliance-test", "version": "1.0"}},
        },
        headers={"Accept": ACCEPT, "Content-Type": "application/json"},
    )
    assert init.status_code == 200
    result = parse_rpc(init)["result"]
    assert result["protocolVersion"] == PROTOCOL
    assert result["serverInfo"]["name"] == "household-disruption-recovery"
    assert "tools" in result["capabilities"]
    mcp.session_id = init.headers.get("mcp-session-id")
    assert mcp.session_id, "server must assign a session id over Streamable HTTP"

    # 2. initialized notification carries the negotiated version header
    note = mcp.http.post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=mcp.headers()
    )
    assert note.status_code == 202

    # 3. tools/list
    listed = parse_rpc(mcp.request("tools/list"))["result"]["tools"]
    names = sorted(t["name"] for t in listed)
    assert names == sorted(TOOL_NAMES)
    for tool in listed:
        assert tool["inputSchema"]["type"] == "object"
        assert "outputSchema" in tool, f"{tool['name']} has no structured output schema"
    apply_tool = next(t for t in listed if t["name"] == "apply_recovery_plan")
    assert apply_tool["annotations"]["destructiveHint"] is True

    # 4. tools/call through the workflow
    state = mcp.call("get_household_state", {"household_id": "hh-demo"})["structuredContent"]
    assert state["state_version"] == 1

    notice = mcp.call("ingest_school_notice", {
        "household_id": "hh-demo",
        "raw_notice": "Due to severe weather, school will operate on a two-hour delay. "
                      "Doors open at 10:05 AM and classes begin at 10:15 AM.",
        "source": "school text alert",
        "received_at": "2026-10-15T06:15:00-04:00",
    })["structuredContent"]
    assert notice["structured"]["delay_minutes"] == 120
    assert [c["code"] for c in notice["conflicts"]] == ["OWNER_UNAVAILABLE", "SUPERVISION_GAP"]

    plan = mcp.call("plan_disruption_recovery", {
        "disruption_id": notice["disruption_id"], "household_state_version": notice["state_version"],
    })["structuredContent"]
    assert plan["feasible"] and plan["requires_approval"]

    # nothing was written by planning
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT assigned_member_id FROM escorts WHERE id='r-emma-school'").fetchone()[0] == "sarah"

    approval = mcp.call("approve_recovery_plan", {
        "plan_id": plan["plan_id"], "plan_version": plan["version"], "decision": "approve",
        "approver_member_id": "sarah", "channel": "voice",
    })["structuredContent"]
    applied = mcp.call("apply_recovery_plan", {
        "plan_id": plan["plan_id"], "approval_token": approval["approval_token"], "idempotency_key": "compliance-0001",
    })["structuredContent"]
    assert applied["status"] == "applied" and applied["failed"] == 0

    # 5. real state write, read straight from the database file
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT assigned_member_id, depart, arrive FROM escorts WHERE id='r-emma-school'").fetchone()
        assert row == ("linda", 9 * 60 + 55, 10 * 60 + 5)
        noah = conn.execute("SELECT assigned_member_id FROM escorts WHERE id='r-noah-daycare'").fetchone()[0]
        assert noah == "sarah"
        delivered = conn.execute("SELECT COUNT(*) FROM notifications WHERE status='delivered'").fetchone()[0]
        assert delivered == 3

    # 6. read-back verification through MCP
    verify = mcp.call("verify_recovery", {"plan_id": plan["plan_id"]})["structuredContent"]
    assert verify["ready"] is True
    assert verify["writes_verified"] == verify["writes_total"] == len(plan["actions"])
    assert verify["violations"] == [] and verify["unassigned"] == []

    # every request after initialize carried the negotiated protocol version
    assert all(h.get("MCP-Protocol-Version") == PROTOCOL for h in mcp.sent_headers)


def test_mcp_apps_card_resource_and_tool_links(server):
    """MCP Apps (io.modelcontextprotocol/ui): the card is a ui:// HTML resource, linked from the
    plan and verify tools (definition and result), and the approval tool is app-only."""
    live, _, _ = server
    mcp = RawMcp(live.url)
    caps = mcp.initialize()["capabilities"]
    assert "resources" in caps

    tools = {t["name"]: t for t in parse_rpc(mcp.request("tools/list"))["result"]["tools"]}
    card_uri = "ui://household-disruption-recovery/card"
    for name in ("plan_disruption_recovery", "verify_recovery"):
        assert tools[name]["_meta"]["ui"]["resourceUri"] == card_uri
    assert tools["approve_recovery_plan"]["_meta"]["ui"]["visibility"] == ["app"]
    for name in ("ingest_school_notice", "report_availability_change", "apply_recovery_plan"):
        assert tools[name]["annotations"]["destructiveHint"] is True
    assert tools["verify_recovery"]["annotations"]["readOnlyHint"] is False

    listed = parse_rpc(mcp.request("resources/list"))["result"]["resources"]
    card = next(r for r in listed if r["uri"] == card_uri)
    assert card["mimeType"] == "text/html;profile=mcp-app"
    read = parse_rpc(mcp.request("resources/read", {"uri": card_uri}))["result"]["contents"][0]
    assert read["mimeType"] == "text/html;profile=mcp-app"
    assert read["text"].startswith("<!doctype html>")
    for method in ("ui/initialize", "ui/notifications/initialized", "ui/notifications/tool-result", "ui/message"):
        assert method in read["text"]

    notice = mcp.ok("ingest_school_notice", {
        "household_id": "hh-demo",
        "raw_notice": "Due to severe weather, school will operate on a two-hour delay. "
                      "Doors open at 10:05 AM and classes begin at 10:15 AM.",
        "source": "school text alert", "received_at": "2026-10-15T06:15:00-04:00",
    })
    result = mcp.call("plan_disruption_recovery", {
        "disruption_id": notice["disruption_id"], "household_state_version": notice["state_version"],
    })
    assert result["_meta"]["ui"]["resourceUri"] == card_uri
    assert result["_meta"]["ui"]["invoked"] == "Plan ready"
    assert result["structuredContent"]["card"]["action"] == "Apply 13 changes"
    assert result["content"][0]["type"] == "text"  # hosts without MCP Apps still get text


def test_unsupported_protocol_version_header_is_rejected(server):
    live, _, _ = server
    mcp = RawMcp(live.url)
    init = mcp.http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 0, "method": "initialize",
              "params": {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        headers={"Accept": ACCEPT, "Content-Type": "application/json"},
    )
    mcp.session_id = init.headers.get("mcp-session-id")
    mcp.http.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=mcp.headers())
    bad = mcp.request("tools/list", protocol="1999-01-01")
    assert bad.status_code == 400


def test_tool_errors_come_back_as_tool_results(server):
    live, _, _ = server
    mcp = RawMcp(live.url)
    init = mcp.http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 0, "method": "initialize",
              "params": {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        headers={"Accept": ACCEPT, "Content-Type": "application/json"},
    )
    mcp.session_id = init.headers.get("mcp-session-id")
    mcp.http.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=mcp.headers())
    result = mcp.call("apply_recovery_plan", {"plan_id": "plan-nope", "approval_token": "x.y", "idempotency_key": "k-0000001"})
    assert result["isError"] is True
    # the SDK prefixes "Error executing tool <name>: "; our stable error code follows
    assert "NOT_FOUND: plan 'plan-nope' not found" in result["content"][0]["text"]


def test_newer_protocol_era_is_refused_so_clients_fall_back_to_2025_11_25(server):
    live, _, _ = server
    http = httpx.Client(base_url=live.url, timeout=15)
    probe = http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}},
        headers={"Accept": ACCEPT, "Content-Type": "application/json", "MCP-Protocol-Version": "2026-07-28"},
    )
    assert probe.status_code == 400
    err = probe.json()["error"]
    assert err["code"] == -32022 and err["data"]["supported"] == [PROTOCOL]


def test_strands_mcp_client_negotiates_2025_11_25(server):
    """The LLM agent's MCP client (Strands) ends up on the same protocol as everything else."""
    import json

    import httpx2
    from mcp.client.streamable_http import streamable_http_client
    from strands.tools.mcp import MCPClient

    live, _, _ = server
    sent: list[tuple] = []

    async def on_request(request):
        body = json.loads(request.content or b"{}")
        sent.append((body.get("method"), request.headers.get("mcp-protocol-version"),
                     (body.get("params") or {}).get("protocolVersion")))

    def transport():
        return streamable_http_client(f"{live.url}/mcp",
                                      http_client=httpx2.AsyncClient(event_hooks={"request": [on_request]}))

    client = MCPClient(transport, tool_filters={"rejected": ["approve_recovery_plan"]})
    with client:
        names = [t.tool_name for t in client.list_tools_sync()]
        result = client.call_tool_sync("t-1", "get_household_state", {"household_id": "hh-demo"})
    assert "approve_recovery_plan" not in names and len(names) == len(TOOL_NAMES) - 1
    assert result["status"] == "success"
    methods = [m for m, _, _ in sent]
    assert methods[0] == "server/discover"           # refused (previous test)
    assert ("initialize", None, PROTOCOL) in sent      # then the 2025-11-25 handshake
    after = sent[methods.index("initialize") + 1:]
    assert after and all(h == PROTOCOL for _, h, _ in after)
