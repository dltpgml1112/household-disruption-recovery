"""The single-port hosted server (python -m hdr.hosted): simulator files, agent API and MCP
server on one port, and one household per browser session."""

from __future__ import annotations

import httpx
import pytest

from hdr.hosted import build
from tests.integration.live import LiveServer, free_port, parse_rpc
from tests.integration.test_mcp_compliance import ACCEPT, PROTOCOL, RawMcp

PUBLIC_HOST = "demo.example.test"


@pytest.fixture
def hosted(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>simulator</title>", encoding="utf-8")
    env = {
        "HDR_DB_PATH": str(tmp_path / "hdr.sqlite3"),
        "HDR_STATIC_DIR": str(static),
        "HDR_AGENT": "rules",  # no model calls in tests
        "HDR_PUBLIC_HOSTS": PUBLIC_HOST,
    }
    port = free_port()
    with LiveServer(build(env, port), port) as live:
        yield live


def turn(http: httpx.Client, sid: str, text: str) -> dict:
    r = http.post(f"/api/session/{sid}/turn", json={"utterance": text, "channel": "voice"})
    assert r.status_code == 200, r.text
    return r.json()


def test_one_port_serves_simulator_api_and_mcp(hosted):
    http = httpx.Client(base_url=hosted.url, timeout=30)
    page = http.get("/")
    assert page.status_code == 200 and "<title>simulator</title>" in page.text
    assert http.get("/api/health").json()["status"] == "ok"

    mcp = RawMcp(hosted.url)
    assert mcp.initialize()["protocolVersion"] == PROTOCOL
    tools = parse_rpc(mcp.request("tools/list"))["result"]["tools"]
    assert len(tools) == 8
    assert mcp.ok("get_household_state", {"household_id": "hh-demo"})["state_version"] == 1


def test_each_browser_session_gets_its_own_household(hosted):
    http = httpx.Client(base_url=hosted.url, timeout=60)
    first, second = http.post("/api/session", json={}).json(), http.post("/api/session", json={}).json()
    a, b = first["state"]["household_id"], second["state"]["household_id"]
    assert a != b and "hh-demo" not in (a, b)

    sid = first["session_id"]
    assert http.post(f"/api/session/{sid}/notice", json={}).status_code == 200
    assert turn(http, sid, "Alexa, fix our morning.")["plan"]["feasible"]
    done = turn(http, sid, "Yes, do it.")
    assert done["verify"]["ready"] is True

    mcp = RawMcp(hosted.url)
    mcp.initialize()
    assert mcp.ok("get_household_state", {"household_id": a})["state_version"] > 1
    assert mcp.ok("get_household_state", {"household_id": b})["state_version"] == 1
    assert mcp.ok("get_household_state", {"household_id": "hh-demo"})["state_version"] == 1

    # resetting one session's morning leaves the other alone, and unknown sessions get nothing
    assert http.post(f"/api/session/{second['session_id']}/reset", json={"variant": None}).status_code == 200
    assert mcp.ok("get_household_state", {"household_id": a})["state_version"] > 1
    assert http.post("/api/session/s-not-issued/reset", json={"variant": None}).status_code == 404


def test_public_host_is_accepted_and_others_are_refused(hosted):
    body = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "host-check", "version": "1.0"}}}
    http = httpx.Client(base_url=hosted.url, timeout=15)
    ok = http.post("/mcp", json=body, headers={"Accept": ACCEPT, "Content-Type": "application/json",
                                               "Host": PUBLIC_HOST, "Origin": f"https://{PUBLIC_HOST}"})
    assert ok.status_code == 200, ok.text
    bad_host = http.post("/mcp", json=body, headers={"Accept": ACCEPT, "Content-Type": "application/json",
                                                     "Host": "attacker.example"})
    assert bad_host.status_code == 421
    bad_origin = http.post("/mcp", json=body, headers={"Accept": ACCEPT, "Content-Type": "application/json",
                                                       "Origin": "https://attacker.example"})
    assert bad_origin.status_code == 403


def test_speaker_decides_who_can_approve(hosted):
    http = httpx.Client(base_url=hosted.url, timeout=60)
    v = http.post("/api/session", json={}).json()
    sid = v["session_id"]
    assert v["speaker"] == "sarah"
    http.post(f"/api/session/{sid}/notice", json={})
    assert turn(http, sid, "Alexa, fix our morning.")["plan"]["feasible"]

    r = http.post(f"/api/session/{sid}/turn", json={"utterance": "Yes, do it.", "speaker": "linda"})
    assert r.status_code == 200
    refused = r.json()
    assert refused["speaker"] == "linda" and refused["apply"] is None and refused["verify"] is None

    bad = http.post(f"/api/session/{sid}/turn", json={"utterance": "Yes, do it.", "speaker": "someone-else"})
    assert bad.status_code == 422

    done = http.post(f"/api/session/{sid}/turn", json={"utterance": "Yes, do it.", "speaker": "sarah"}).json()
    assert done["verify"]["ready"] is True


def test_page_can_only_fail_one_message_in_its_own_household(hosted):
    """The fault route behind "Fail Linda's message" takes that and nothing else: a tool delay
    set through it would slow every visitor's household."""
    http = httpx.Client(base_url=hosted.url, timeout=60)
    sid = http.post("/api/session", json={}).json()["session_id"]
    ok = http.post(f"/api/session/{sid}/fault", json={"target": "notify", "selector": "linda", "times": 1})
    assert ok.status_code == 200, ok.text
    for body in ({"target": "tool_delay", "selector": "get_household_state", "delay_ms": 60000},
                 {"target": "notify", "selector": "linda", "times": -1},
                 {"target": "notify", "selector": "linda", "delay_ms": 5000},
                 {"target": "calendar", "selector": "*"}):
        assert http.post(f"/api/session/{sid}/fault", json=body).status_code == 422, body


def test_card_arrives_as_an_mcp_app(hosted):
    """The session reads the card view over MCP (resources/read) and the plan and result screens
    carry it with the tool result it renders; other screens carry no app."""
    http = httpx.Client(base_url=hosted.url, timeout=60)
    v = http.post("/api/session", json={}).json()
    sid = v["session_id"]
    assert v["app"] is None
    reads = [w for w in v["trace"]["wire"] if w.get("method") == "resources/read"]
    assert [w["tool"] for w in reads] == ["ui://household-disruption-recovery/card"]

    http.post(f"/api/session/{sid}/notice", json={})
    plan = turn(http, sid, "Alexa, fix our morning.")
    app = plan["app"]
    assert app["uri"] == "ui://household-disruption-recovery/card"
    assert app["tool"] == "plan_disruption_recovery"
    assert app["html"].startswith("<!doctype html>") and "ui/initialize" in app["html"]
    assert app["result"]["structuredContent"]["card"]["action"] == "Apply 13 changes"

    done = turn(http, sid, "Yes, do it.")
    assert done["app"]["tool"] == "verify_recovery"
    assert done["app"]["result"]["structuredContent"]["card"]["title"] == "Household ready"
