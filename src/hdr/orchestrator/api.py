"""HTTP API for the web simulator. The simulator never talks to the MCP server directly;
every household read and write goes through the agent and its MCP session."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from hdr.envfile import load_env_file
from hdr.orchestrator.agent_rules import AgentSession, RuleAgent, new_session
from hdr.orchestrator.agent_strands import StrandsAgent
from hdr.orchestrator.llm import model_from_env
from hdr.orchestrator.mcp_client import McpSession, McpUnavailable, Trace


class TurnIn(BaseModel):
    utterance: str
    channel: str = "voice"
    speaker: str | None = None  # household member id; a real device would identify the voice


class ResetIn(BaseModel):
    variant: str | None = None


class FaultIn(BaseModel):
    target: str
    selector: str = "*"
    times: int = 1
    delay_ms: int = 0


def proposed_calendar(s: AgentSession) -> list[dict] | None:
    if not (s.plan and s.plan.get("feasible") and s.state) or s.apply:
        return None
    events = {e["id"]: dict(e) for e in s.state["calendar"]}
    for a in s.plan["actions"]:
        if a["type"] == "cancel_calendar_event" and a["target"]["event_id"] in events:
            events[a["target"]["event_id"]]["status"] = "cancelled"
        elif a["type"] == "create_calendar_event":
            after = a["after"]
            events[a["target"]["event_id"]] = {
                "id": a["target"]["event_id"], "member_id": after["member_id"], "title": after["title"],
                "start": after["start"], "end": after["end"], "location": after["location"], "hard": True,
                "kind": after["kind"], "status": "confirmed", "responsibility_id": after["responsibility_id"],
                "new": True,
            }
    return sorted(events.values(), key=lambda e: (e["start"], e["id"]))


def view(s: AgentSession) -> dict[str, Any]:
    conflicts = (s.plan or {}).get("conflicts") or (s.notice or {}).get("conflicts") or []
    return {
        "session_id": s.session_id,
        "agent": s.agent,
        "speaker": s.approver_member_id,
        "screen": s.screen,
        "speech": next((t["text"] for t in reversed(s.transcript) if t["role"] == "assistant"), None),
        "transcript": s.transcript[-8:],
        "pending_notice": s.pending_notice,
        "notice": s.notice,
        "conflicts": conflicts,
        "state": s.state,
        "plan": s.plan,
        "proposed_calendar": proposed_calendar(s),
        "approval": {k: v for k, v in (s.approval or {}).items() if k != "approval_token"} or None,
        "apply": s.apply,
        "verify": s.verify,
        "audit": s.audit,
        "trace": s.last_trace,
        "degraded": s.degraded,
        "error": s.error,
    }


def create_api(mcp_url: str, admin_url: str, admin_token: str | None,
               agent: RuleAgent | StrandsAgent | None = None, *,
               household_per_session: bool = False, max_sessions: int = 500) -> FastAPI:
    """`household_per_session` gives every browser session its own copy of the demo household,
    so people using a shared (hosted) simulator do not change each other's morning."""
    app = FastAPI(title="Household Disruption Recovery — orchestrator", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_methods=["GET", "POST"],
        allow_headers=["content-type"],
    )
    agent = agent or RuleAgent(mcp_url)
    sessions: dict[str, AgentSession] = {}

    def get(sid: str) -> AgentSession:
        if sid not in sessions:
            raise HTTPException(404, "unknown session")
        return sessions[sid]

    def remember(s: AgentSession) -> None:
        sessions[s.session_id] = s
        while len(sessions) > max_sessions:
            sessions.pop(next(iter(sessions)))

    def household_for(sid: str) -> str:
        return f"hh-{sid}" if household_per_session else "hh-demo"

    async def admin(method: str, path: str, json: dict | None = None) -> dict:
        if not admin_token:
            raise HTTPException(503, "demo controls are disabled (HDR_ADMIN_TOKEN not set)")
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.request(method, admin_url + path, json=json, headers={"x-admin-token": admin_token})
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.text)
        return r.json()

    async def refresh(s: AgentSession) -> None:
        trace = Trace()
        try:
            async with McpSession(mcp_url, trace) as mcp:
                s.state = await mcp.call("get_household_state", {"household_id": s.household_id})
            s.degraded, s.error = False, None
            s.last_trace = {"protocol_version": mcp.protocol_version, "tools": mcp.tools, "wire": trace.wire,
                            "calls": trace.calls, "intent": "REFRESH", "agent": agent.name}
        except McpUnavailable as e:
            s.degraded, s.error = True, {"code": "MCP_UNAVAILABLE", "message": str(e)}

    @app.get("/api/health")
    async def health() -> dict:
        info = {"status": "ok", "mcp_url": mcp_url, "agent": agent.name}
        if isinstance(agent, StrandsAgent):
            info.update(model=agent.model_label, model_ready=agent.model is not None, note=agent.no_model_reason)
        return info

    @app.post("/api/session")
    async def create_session() -> dict:
        s = new_session()
        s.household_id = household_for(s.session_id)
        s.agent = agent.name
        if household_per_session:
            await admin("POST", "/admin/reset", {"variant": None, "household_id": s.household_id})
        remember(s)
        await refresh(s)
        return view(s)

    @app.get("/api/session/{sid}")
    async def read_session(sid: str) -> dict:
        return view(get(sid))

    @app.post("/api/session/{sid}/turn")
    async def turn(sid: str, body: TurnIn) -> dict:
        s = get(sid)
        if not body.utterance.strip():
            raise HTTPException(422, "empty utterance")
        if body.channel not in ("voice", "screen"):
            raise HTTPException(422, "channel must be voice or screen")
        if body.speaker:
            members = {m["id"] for m in (s.state or {}).get("members", [])}
            if members and body.speaker not in members:
                raise HTTPException(422, "speaker must be a member of the household")
            s.approver_member_id = body.speaker
        await agent.handle(s, body.utterance[:500], body.channel)
        return view(s)

    @app.post("/api/session/{sid}/notice")
    async def notice(sid: str) -> dict:
        s = get(sid)
        s.pending_notice = await admin("GET", "/admin/demo-notice")
        s.screen = "B"
        s.transcript.append({"role": "assistant",
                             "text": "A new school alert just came in. I can check what it changes for this morning."})
        return view(s)

    @app.post("/api/session/{sid}/reset")
    async def reset(sid: str, body: ResetIn) -> dict:
        if household_per_session:
            get(sid)  # households are only created for sessions this server handed out
        hid = household_for(sid)
        await admin("DELETE", f"/admin/faults?household_id={hid}")
        await admin("POST", "/admin/reset", {"variant": body.variant,
                                             "household_id": hid if household_per_session else None})
        fresh = new_session(hid)
        fresh.session_id = sid
        fresh.agent = agent.name
        remember(fresh)
        await refresh(fresh)
        return view(fresh)

    @app.post("/api/session/{sid}/fault")
    async def fault(sid: str, body: FaultIn) -> dict:
        s = get(sid)
        await admin("POST", "/admin/faults", {**body.model_dump(),
                                             "household_id": s.household_id if body.target != "tool_delay" else "*"})
        return {"added": body.model_dump()}

    return app


def agent_from_env(mcp_url: str, env: Mapping[str, str] | None = None) -> RuleAgent | StrandsAgent:
    """HDR_AGENT=strands (default) runs the LLM agent, falling back to rules per turn
    when no model is configured or a call fails. HDR_AGENT=rules runs only the rule agent."""
    env = os.environ if env is None else env
    if env.get("HDR_AGENT", "strands").lower() == "rules":
        return RuleAgent(mcp_url)
    choice = model_from_env(env)
    return StrandsAgent(mcp_url, choice.model, model_label=f"{choice.provider}/{choice.model_id}",
                        no_model_reason=choice.reason, retry_attempts=int(env.get("HDR_LLM_RETRIES", "4")))


def api_from_env() -> FastAPI:
    load_env_file()
    mcp_url = os.environ.get("HDR_MCP_URL", "http://127.0.0.1:8765/mcp")
    admin_url = os.environ.get("HDR_ADMIN_URL", mcp_url.rsplit("/mcp", 1)[0])
    return create_api(mcp_url, admin_url, os.environ.get("HDR_ADMIN_TOKEN") or None, agent_from_env(mcp_url),
                      household_per_session=os.environ.get("HDR_HOUSEHOLD_PER_SESSION") == "1")
