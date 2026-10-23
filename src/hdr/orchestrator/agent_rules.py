"""Deterministic reference agent: regex intents, fixed tool sequences over MCP.

It is the fallback for the LLM agent and the baseline it is evaluated against.
Everything it says is built from tool results; it never claims the morning is
fixed unless verify_recovery returned ready=true.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from hdr.orchestrator.consent import hedge
from hdr.orchestrator.mcp_client import McpSession, McpToolError, McpUnavailable, Trace

WAKE = re.compile(r"^\s*(hey\s+)?alexa[,!.\s]+", re.I)
INTENTS: list[tuple[str, re.Pattern]] = [
    ("AVAILABILITY_OFF", re.compile(r"\b(?P<name>[A-Z][a-z]+)\s+(can't|cannot|can not|won't|isn't able to|is not able to)\s+(come|make it|help|do it|be there)|\b(?P<name2>[A-Z][a-z]+)\s+is\s+(sick|unavailable|not available|out)\b")),
    ("AVAILABILITY_ON", re.compile(r"\b(?P<name>[A-Z][a-z]+)\s+(can come|can make it|is available|is back)\b")),
    ("REJECT", re.compile(r"^\s*(no|nope|cancel|don't|do not|stop|reject|not now)\b", re.I)),
    ("APPROVE", re.compile(r"^\s*(yes|yeah|yep|do it|go ahead|apply|approve|sounds good|ok(ay)?|confirm|make it so)\b", re.I)),
    ("RETRY", re.compile(r"\b(retry|try again)\b", re.I)),
    ("AUDIT", re.compile(r"\b(audit|log|what did you (do|change))\b", re.I)),
    ("STATUS", re.compile(r"\b(status|are we (good|ready|set)|is everything|check (it|again))\b", re.I)),
    ("FIX", re.compile(r"\b(fix|sort|handle|recover|figure out|deal with|re-?plan|what do we do)\b", re.I)),
]


def detect_intent(utterance: str) -> tuple[str, dict[str, str]]:
    text = WAKE.sub("", utterance).strip()
    for name, pattern in INTENTS:
        m = pattern.search(text)
        if m:
            groups = {k: v for k, v in m.groupdict().items() if v}
            return name, groups
    return "UNKNOWN", {}


def refusal(e: McpToolError) -> str:
    if e.code == "NOT_AUTHORIZED":
        return "Only a parent can approve this plan, so I haven't applied it."
    return f"The household service refused that ({e.code}), so I stopped there."


def say_time(hhmm: str | None) -> str:
    if not hhmm:
        return "?"
    h, m = hhmm.split(":")
    return f"{int(h)}:{m}"


def say_span(span: str) -> str:
    a, b = span.split("-")
    return f"{say_time(a)} to {say_time(b)}"


@dataclass
class AgentSession:
    session_id: str
    household_id: str = "hh-demo"
    approver_member_id: str = "sarah"
    screen: str = "A"
    pending_notice: dict | None = None
    notice: dict | None = None
    disruption_id: str | None = None
    state: dict | None = None
    plan: dict | None = None
    approval: dict | None = None
    apply: dict | None = None
    verify: dict | None = None
    audit: dict | None = None
    transcript: list[dict] = field(default_factory=list)
    apply_attempts: int = 0
    degraded: bool = False
    error: dict | None = None
    last_trace: dict | None = None
    agent: str = "rules"
    llm_messages: list = field(default_factory=list)
    # MCP Apps views read at session start: tool name -> {"uri", "html"}
    ui: dict = field(default_factory=dict)


class RuleAgent:
    name = "rules"

    def __init__(self, mcp_url: str, timeout_s: float = 8.0):
        self.mcp_url = mcp_url
        self.timeout_s = timeout_s

    # ---- helpers ------------------------------------------------------------

    def _names(self, s: AgentSession) -> dict[str, str]:
        return {m["id"]: m["name"] for m in (s.state or {}).get("members", [])}

    def _member_by_name(self, s: AgentSession, name: str) -> str | None:
        for m in (s.state or {}).get("members", []):
            if m["name"].lower() == name.lower():
                return m["id"]
        return None

    def describe_fix(self, s: AgentSession, plan: dict) -> list[str]:
        names = self._names(s)
        changed = {a["target"]["escort_id"]: a["after"] for a in plan["actions"] if a["type"] == "assign_escort"}
        drivers = {a["child_id"]: a for a in plan["assignments"]}
        gap_children = {c["child_id"] for c in plan["conflicts"] if c["code"] == "SUPERVISION_GAP"}
        sentences: list[str] = []
        for a in plan["assignments"]:
            if a["escort_id"] in changed and a["child_id"] not in gap_children:
                sentences.append(f"{names[a['member_id']]} takes {names[a['child_id']]} at {say_time(a['depart'])}.")
        for child in sorted(gap_children):
            segs = [x for x in plan["supervision"] if x["child_id"] == child]
            driver = drivers.get(child)
            for i, seg in enumerate(segs):
                who = names[seg["member_id"]]
                drives = driver and driver["member_id"] == seg["member_id"] and i == len(segs) - 1
                tail = f" and drives {names[child]} at {say_time(driver['depart'])}" if drives else ""
                if i == 0:
                    sentences.append(f"{who} stays with {names[child]} until {say_time(seg['end'])}{tail}.")
                else:
                    sentences.append(f"{who} takes over at {say_time(seg['start'])}{tail}.")
            if driver and segs and driver["member_id"] != segs[-1]["member_id"]:
                sentences.append(f"{names[driver['member_id']]} drives {names[child]} at {say_time(driver['depart'])}.")
        for a in plan["actions"]:
            prev = a.get("before") or {}
            if a["type"] == "cancel_calendar_event" and prev.get("kind") == "commitment":
                sentences.append(f"{names.get(prev['member_id'], prev['member_id'])}'s {prev['title'].lower()} "
                                 f"from {say_time(prev['start'])} comes off the calendar.")
        return sentences

    def describe_unresolved(self, s: AgentSession, plan: dict) -> str:
        names = self._names(s)
        parts = []
        for u in plan["unresolved"]:
            if u["code"] == "SUPERVISION_GAP":
                child = names.get(u["child_id"], u["child_id"])
                # spoken: keep reasons like "Linda is unavailable today"; the card lists the timed commitments
                why = [b for b in u["blockers"] if not re.search(r"\d\d:\d\d", b)][:2]
                parts.append(f"nobody approved can be with {child} from {say_span(u['interval'])}"
                             + "".join(f"; {w}" for w in why))
            else:
                parts.append(u["detail"])
        return "; ".join(parts)

    # ---- turn ---------------------------------------------------------------

    async def handle(self, s: AgentSession, utterance: str, channel: str = "voice") -> str:
        intent, groups = detect_intent(utterance)
        trace = Trace()
        s.error = None
        s.degraded = False
        try:
            async with McpSession(self.mcp_url, trace, timeout_s=self.timeout_s) as mcp:
                try:
                    speech = await self._dispatch(mcp, s, intent, groups, channel, utterance)
                except McpToolError as e:
                    s.error = {"code": e.code, "message": e.message}
                    speech = refusal(e)
            s.last_trace = {"protocol_version": mcp.protocol_version, "tools": mcp.tools,
                            "wire": trace.wire, "calls": trace.calls, "intent": intent, "agent": self.name}
        except McpUnavailable as e:
            s.degraded = True
            s.error = {"code": "MCP_UNAVAILABLE", "message": str(e)}
            s.last_trace = {"protocol_version": None, "tools": [], "wire": trace.wire, "calls": trace.calls,
                            "intent": intent, "agent": self.name}
            speech = "I can't reach the household service right now, so I haven't changed anything. Try again in a moment."
        s.transcript.append({"role": "user", "text": utterance, "channel": channel})
        s.transcript.append({"role": "assistant", "text": speech})
        return speech

    async def _dispatch(self, mcp: McpSession, s: AgentSession, intent: str, groups: dict, channel: str,
                        utterance: str = "") -> str:
        if intent == "FIX":
            return await self.fix(mcp, s)
        if intent == "APPROVE":
            word = hedge(utterance)
            if word and s.plan and s.plan.get("status") == "proposed":
                await self.refresh(mcp, s)
                return "I haven't changed anything yet. Do you want me to go ahead with the plan as it is? Say yes to apply it."
            return await self.approve(mcp, s, channel)
        if intent == "REJECT":
            return await self.reject(mcp, s, channel)
        if intent == "RETRY":
            return await self.retry(mcp, s)
        if intent == "STATUS":
            return await self.status(mcp, s)
        if intent == "AUDIT":
            return await self.audit(mcp, s)
        if intent in ("AVAILABILITY_OFF", "AVAILABILITY_ON"):
            return await self.availability(mcp, s, groups.get("name") or groups.get("name2", ""), intent == "AVAILABILITY_ON")
        await self.refresh(mcp, s)
        return "You can say \"fix our morning\" after a school alert, then \"yes\" to apply the plan."

    async def refresh(self, mcp: McpSession, s: AgentSession) -> dict:
        s.state = await mcp.call("get_household_state", {"household_id": s.household_id})
        return s.state

    async def fix(self, mcp: McpSession, s: AgentSession) -> str:
        await self.refresh(mcp, s)
        if s.pending_notice and not s.disruption_id:
            n = s.pending_notice
            s.notice = await mcp.call("ingest_school_notice", {
                "household_id": s.household_id, "raw_notice": n["raw"], "source": n["source"],
                "received_at": n["received_at"],
            })
            if s.notice["structured"]["needs_clarification"]:
                s.screen = "B"
                return "I'm not sure what the school notice means. " + " ".join(s.notice["structured"]["questions"])
            if not s.notice["disruption_id"]:
                s.screen = "B"
                return s.notice["message"]
            s.disruption_id = s.notice["disruption_id"]
            s.pending_notice = None
            await self.refresh(mcp, s)
        if not s.disruption_id:
            s.screen = "A"
            return "I don't see any changes to today's plans. Everyone is set."
        return await self._plan(mcp, s, preface=True)

    async def _plan(self, mcp: McpSession, s: AgentSession, preface: bool) -> str:
        try:
            plan = await mcp.call("plan_disruption_recovery", {
                "disruption_id": s.disruption_id, "household_state_version": s.state["state_version"],
            })
        except McpToolError as e:
            if e.code != "STALE_STATE":
                raise
            await self.refresh(mcp, s)
            plan = await mcp.call("plan_disruption_recovery", {
                "disruption_id": s.disruption_id, "household_state_version": s.state["state_version"],
            })
        s.plan, s.approval, s.apply, s.verify = plan, None, None, None
        s.apply_attempts = 0
        s.screen = "C"
        # Spoken replies stay short; the card and the screen carry the conflicts, counts and blockers.
        head = ""
        if preface and s.notice:
            n = len(plan["conflicts"])
            if s.notice["structured"]["kind"] == "closure":
                head = f"School is closed today, so {n} thing{'s' if n != 1 else ''} break{'' if n != 1 else 's'}. "
            else:
                delay = s.notice["structured"].get("delay_minutes") or 0
                hours = {60: "an hour", 120: "two hours", 180: "three hours"}.get(delay, f"{delay} minutes")
                head = f"School starts {hours} late, so {n} thing{'s' if n != 1 else ''} break{'' if n != 1 else 's'}. "
        if not plan["feasible"]:
            return head + ("I couldn't find a plan that covers everyone: " + self.describe_unresolved(s, plan)
                           + ". Can another approved adult help, or can one of you move a commitment?")
        fix = " ".join(self.describe_fix(s, plan))
        return head + "Here's the fix. " + fix + " Should I go ahead?"

    async def approve(self, mcp: McpSession, s: AgentSession, channel: str) -> str:
        if not s.plan or not s.plan.get("feasible") or s.plan.get("status") not in ("proposed",):
            await self.refresh(mcp, s)
            if s.apply and s.apply["status"] == "partially_applied":
                return await self.retry(mcp, s)
            return "There's nothing waiting for your approval."
        try:
            s.approval = await mcp.call("approve_recovery_plan", {
                "plan_id": s.plan["plan_id"], "plan_version": s.plan["version"], "decision": "approve",
                "approver_member_id": s.approver_member_id, "channel": channel,
            })
            s.plan["status"] = "approved"
            return await self._apply_and_verify(mcp, s)
        except McpToolError as e:
            if e.code in ("STALE_PLAN", "PLAN_NOT_APPROVABLE", "PLAN_NOT_APPROVED"):
                await self.refresh(mcp, s)
                speech = await self._plan(mcp, s, preface=False)
                return "Something changed since I made that plan, so nothing was applied. " + speech
            raise

    async def _apply_and_verify(self, mcp: McpSession, s: AgentSession) -> str:
        s.apply_attempts += 1
        key = f"{s.session_id}-{s.plan['plan_id']}-{s.apply_attempts}"
        try:
            s.apply = await mcp.call("apply_recovery_plan", {
                "plan_id": s.plan["plan_id"], "approval_token": s.approval["approval_token"], "idempotency_key": key,
            })
        except McpToolError as e:
            if e.code == "STALE_PLAN":
                await self.refresh(mcp, s)
                speech = await self._plan(mcp, s, preface=False)
                return "The household changed before I could apply that plan, so I stopped. " + speech
            raise
        s.verify = await mcp.call("verify_recovery", {"plan_id": s.plan["plan_id"]})
        await self.refresh(mcp, s)
        s.screen = "D"
        v = s.verify
        if v["ready"]:
            return (f"Done. All {v['writes_total']} changes are in and I read each one back. "
                    f"Every responsibility has an owner. Your household is ready.")
        failed = "; ".join(v["failed_actions"]) or "; ".join(v["violations"])
        return (f"{v['writes_verified']} of {v['writes_total']} changes went through, so I'm not calling it done. "
                f"Still open: {failed}. Say \"try again\" to retry only what failed.")

    async def retry(self, mcp: McpSession, s: AgentSession) -> str:
        if not (s.plan and s.approval and s.apply and s.apply["status"] == "partially_applied"):
            await self.refresh(mcp, s)
            return "There's nothing to retry."
        return await self._apply_and_verify(mcp, s)

    async def reject(self, mcp: McpSession, s: AgentSession, channel: str) -> str:
        await self.refresh(mcp, s)
        if not s.plan or s.plan.get("status") != "proposed" or not s.plan.get("feasible"):
            return "Okay."
        await mcp.call("approve_recovery_plan", {
            "plan_id": s.plan["plan_id"], "plan_version": s.plan["version"], "decision": "reject",
            "approver_member_id": s.approver_member_id, "channel": channel,
        })
        s.plan["status"] = "rejected"
        return "Okay, I won't change anything. The conflicts are still open."

    async def status(self, mcp: McpSession, s: AgentSession) -> str:
        await self.refresh(mcp, s)
        if s.plan and s.plan.get("feasible") and s.apply:
            s.verify = await mcp.call("verify_recovery", {"plan_id": s.plan["plan_id"]})
            return s.verify["summary"]
        if s.disruption_id:
            return "There's an open school change and no applied plan yet. Say \"fix our morning\"."
        return "Nothing has changed today. Everyone is set."

    async def audit(self, mcp: McpSession, s: AgentSession) -> str:
        if not s.disruption_id:
            return "There's no disruption to report on yet."
        s.audit = await mcp.call("get_audit_log", {"disruption_id": s.disruption_id})
        sm = s.audit["summary"]
        calls = sum(sm["tool_calls"].values())
        return (f"For this disruption I made {calls} tool calls, {sm['approvals']} approval, "
                f"{sm['writes_applied']} writes applied and {sm['writes_failed']} failed.")

    async def availability(self, mcp: McpSession, s: AgentSession, name: str, available: bool) -> str:
        await self.refresh(mcp, s)
        member_id = self._member_by_name(s, name)
        if member_id is None:
            return f"I don't know anyone called {name} in this household."
        await mcp.call("report_availability_change", {
            "household_id": s.household_id, "member_id": member_id, "available": available,
            "reason": "reported by a parent",
        })
        await self.refresh(mcp, s)
        noted = f"Got it, {name} {'can' if available else "can't"} help today. "
        if s.disruption_id and not (s.verify and s.verify.get("ready")):
            return noted + await self._plan(mcp, s, preface=False)
        return noted


def new_session(household_id: str = "hh-demo") -> AgentSession:
    return AgentSession(session_id=f"s-{uuid.uuid4().hex}", household_id=household_id)
