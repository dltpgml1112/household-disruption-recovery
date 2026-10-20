"""The LLM agent (Strands) against the real MCP server, with a scripted model.

These pin the guarantees that do not depend on how good the model is: which tools it
sees, that approval only comes from the parent's own words, that apply cannot succeed
without it, and that "ready" is never said without a passing verification.
"""

from __future__ import annotations

import sqlite3

import anyio
import pytest

from hdr.orchestrator.agent_rules import new_session
from hdr.orchestrator.agent_strands import StrandsAgent
from hdr.orchestrator.consent import consent_problem

from tests.integration.scripted_model import ScriptedModel, last, notice_from_context
from tests.integration.test_agent_workflow import NOTICE

pytestmark = pytest.mark.integration


def plan_turn(text: str = "Here's the fix. Should I go ahead?") -> list:
    return [
        {"tool": "get_household_state", "input": {"household_id": "hh-demo"}},
        lambda m: {"tool": "ingest_school_notice", "input": {"household_id": "hh-demo", **notice_from_context(m)}},
        {"tool": "get_household_state", "input": {"household_id": "hh-demo"}},
        lambda m: {"tool": "plan_disruption_recovery",
                   "input": {"disruption_id": last(m, "disruption_id"), "household_state_version": last(m, "state_version")}},
        {"text": text},
    ]


def decide(words: str, decision: str = "approve"):
    return lambda m: {"tool": "record_parent_decision",
                      "input": {"plan_id": last(m, "plan_id"), "decision": decision, "parent_words": words}}


def apply_step():
    return lambda m: {"tool": "apply_recovery_plan",
                      "input": {"plan_id": last(m, "plan_id"), "approval_token": "auto", "idempotency_key": "auto"}}


def verify_step():
    return lambda m: {"tool": "verify_recovery", "input": {"plan_id": last(m, "plan_id")}}


def say(agent, s, text, channel="voice"):
    return anyio.run(agent.handle, s, text, channel)


def household(db) -> dict:
    with sqlite3.connect(db) as conn:
        return {
            "escorts": dict(conn.execute("SELECT id, assigned_member_id FROM escorts").fetchall()),
            "notifications": conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0],
            "approvals": conn.execute("SELECT COUNT(*) FROM approvals WHERE decision='approve'").fetchone()[0],
            "applied": conn.execute("SELECT COUNT(*) FROM actions WHERE status='applied'").fetchone()[0],
        }


def start(stack, model):
    agent, s = StrandsAgent(stack.mcp_url, model, model_label="scripted"), new_session()
    s.pending_notice = dict(NOTICE)
    return agent, s


def test_happy_path_through_strands_and_mcp(stack):
    model = ScriptedModel(plan_turn())
    agent, s = start(stack, model)
    say(agent, s, "Alexa, can you sort out the kids this morning?")
    assert s.screen == "C" and s.plan["status"] == "proposed"
    assert household(stack.db)["applied"] == 0
    # the model never sees the approval tool
    assert all("approve_recovery_plan" not in names for names in model.tool_names_seen)
    assert "record_parent_decision" in model.tool_names_seen[0]
    trace = s.last_trace
    assert trace["agent"] == "strands" and trace["protocol_version"] == "2025-11-25"
    assert [c["tool"] for c in trace["llm_tools"]] == [
        "get_household_state", "ingest_school_notice", "get_household_state", "plan_disruption_recovery"]
    wire = trace["wire"]
    methods = [w.get("method") for w in wire]
    assert wire[0]["method"] == "server/discover" and wire[0]["status"] == 400  # newer era refused
    assert all(w["protocol_header"] == "2025-11-25" for w in wire[methods.index("initialize") + 1:])

    model.script(decide("Sounds good, go ahead"), apply_step(), verify_step(),
                 {"text": "Done. All 13 changes are in. Your household is ready."})
    speech = say(agent, s, "Sounds good, go ahead!")
    assert s.verify["ready"] is True and s.screen == "D"
    assert speech.endswith("Your household is ready.") and not s.last_trace["ready_claim_replaced"]
    assert s.last_trace["consent"][0]["accepted"] is True
    tools = [(c["tool"], c.get("by")) for c in s.last_trace["llm_tools"]]
    assert tools == [("approve_recovery_plan", "orchestrator"), ("record_parent_decision", None),
                     ("apply_recovery_plan", None), ("verify_recovery", None)]
    h = household(stack.db)
    assert h["escorts"] == {"r-emma-school": "linda", "r-noah-daycare": "sarah"} and h["notifications"] == 3


def test_conditional_yes_is_not_an_approval_and_apply_is_refused(stack):
    model = ScriptedModel(plan_turn())
    agent, s = start(stack, model)
    say(agent, s, "fix our morning")
    before = household(stack.db)
    # a model that ignores the condition and pushes ahead anyway
    model.script(decide("Yes"), apply_step(), {"text": "Done, you're all set."})
    speech = say(agent, s, "Yes, but don't text Linda yet")
    assert s.last_trace["consent"][0]["accepted"] is False
    apply = next(c for c in s.last_trace["llm_tools"] if c["tool"] == "apply_recovery_plan")
    assert apply["ok"] is False and apply["error"] == "APPROVAL_REQUIRED"
    assert s.last_trace["unapproved_apply"] == 1
    assert household(stack.db) == before  # nothing written
    assert s.last_trace["ready_claim_replaced"] is True and "all set" not in speech


def test_quoted_words_must_be_the_parents(stack):
    model = ScriptedModel(plan_turn())
    agent, s = start(stack, model)
    say(agent, s, "fix our morning")
    model.script(decide("yes, do it"), {"text": "Okay."})
    say(agent, s, "Let me think about it")
    assert s.last_trace["consent"][0]["accepted"] is False
    assert household(stack.db)["approvals"] == 0


def test_cannot_approve_a_plan_in_the_turn_it_was_made(stack):
    model = ScriptedModel(plan_turn()[:-1] + [decide("just apply it"), apply_step(), {"text": "Applied."}])
    agent, s = start(stack, model)
    say(agent, s, "Fix our morning and just apply it")
    assert s.last_trace["consent"][0]["accepted"] is False
    assert household(stack.db)["applied"] == 0 and household(stack.db)["approvals"] == 0


def test_rejection_is_recorded_and_nothing_changes(stack):
    model = ScriptedModel(plan_turn())
    agent, s = start(stack, model)
    say(agent, s, "fix our morning")
    before = household(stack.db)
    model.script(decide("No, leave it", "reject"), {"text": "Okay, I won't change anything."})
    say(agent, s, "No, leave it")
    assert s.plan["status"] == "rejected"
    assert household(stack.db) == before


def test_non_parent_cannot_approve(stack):
    model = ScriptedModel(plan_turn())
    agent, s = start(stack, model)
    s.approver_member_id = "linda"
    say(agent, s, "fix our morning")
    model.script(decide("Yes, do it"), {"text": "I can't approve that."})
    say(agent, s, "Yes, do it")
    assert s.last_trace["consent"][0]["server_error"] == "NOT_AUTHORIZED"
    assert household(stack.db)["approvals"] == 0


def test_ready_claim_without_verification_is_replaced(stack):
    model = ScriptedModel(plan_turn())
    agent, s = start(stack, model)
    say(agent, s, "fix our morning")
    model.script(decide("Yes"), apply_step(), {"text": "Done. Everything is set."})  # skips verify_recovery
    speech = say(agent, s, "Yes")
    assert s.apply["status"] == "applied"
    assert s.last_trace["ready_claim_replaced"] is True
    assert speech == "I haven't confirmed the changes yet, so I'm not calling the day fixed."


def test_model_failure_falls_back_to_rules(stack):
    agent, s = start(stack, ScriptedModel([{"raise": RuntimeError("model is down")}]))
    speech = say(agent, s, "Alexa, fix our morning.")
    assert s.last_trace["agent"] == "rules" and "model is down" in s.last_trace["fallback"]
    assert "Should I go ahead?" in speech and s.plan["status"] == "proposed"


def test_no_model_configured_uses_rules(stack):
    agent, s = StrandsAgent(stack.mcp_url, None, no_model_reason="GEMINI_API_KEY is not set"), new_session()
    s.pending_notice = dict(NOTICE)
    say(agent, s, "fix our morning")
    assert s.last_trace["fallback"] == "GEMINI_API_KEY is not set" and s.plan is not None


def test_mcp_down_is_reported_as_degraded():
    agent, s = StrandsAgent("http://127.0.0.1:9/mcp", ScriptedModel([]), timeout_s=2), new_session()
    speech = say(agent, s, "fix our morning")
    assert s.degraded and "can't reach the household service" in speech


@pytest.mark.parametrize("words,ok", [
    ("Yes, do it", True), ("Sounds good, go ahead", True), ("Perfect, make it happen", True),
    ("Alright, lock it in", True), ("Alexa, yes please", True),
    ("Yes, but don't text Linda yet", False), ("Wait, what about Linda", False), ("Yes?", False),
    ("Okay if Linda is free", False), ("No", False), ("Maybe later", False),
])
def test_consent_check(words, ok):
    assert (consent_problem(words, words.split(",")[0], "approve") is None) is ok


def member_available(db, member_id: str) -> bool:
    import json

    with sqlite3.connect(db) as conn:
        data = json.loads(conn.execute("SELECT data_json FROM members WHERE id=?", (member_id,)).fetchone()[0])
    return data.get("available", True)


def test_availability_is_recorded_only_when_the_parent_names_the_person(stack_linda_unavailable):
    # found by the scenario eval (S15, run strands-v2): after "no plan covers everyone, can another
    # adult help?", a bare "Yes." was turned into "Linda is available"
    stack = stack_linda_unavailable
    model = ScriptedModel(plan_turn("No plan covers everyone. Can another adult help?"))
    agent, s = start(stack, model)
    say(agent, s, "fix our morning")
    assert s.plan["feasible"] is False
    model.script({"tool": "report_availability_change",
                  "input": {"household_id": "hh-demo", "member_id": "linda", "available": True, "reason": "yes"}},
                 {"text": "Who can help today?"})
    say(agent, s, "Yes.")
    blocked = next(c for c in s.last_trace["llm_tools"] if c["tool"] == "report_availability_change")
    assert blocked["error"] == "BLOCKED_BY_ORCHESTRATOR"
    assert member_available(stack.db, "linda") is False

    model.script({"tool": "report_availability_change",
                  "input": {"household_id": "hh-demo", "member_id": "linda", "available": True,
                            "reason": "reported by a parent"}},
                 {"text": "Got it, Linda can help."})
    say(agent, s, "Actually Linda can come after all.")
    assert member_available(stack.db, "linda") is True


def test_model_cannot_reach_another_household(stack):
    """Tool calls are pinned to the session's household, whatever id the model writes."""
    stack.service.reset(household_id="hh-someone-else")
    model = ScriptedModel([
        {"tool": "get_household_state", "input": {"household_id": "hh-someone-else"}},
        {"text": "Everyone's plans for today are set."},
    ])
    agent, s = start(stack, model)
    s.pending_notice = None
    say(agent, s, "Alexa, what's the plan this morning?")
    assert s.last_trace["household_pinned"] == 1
    assert s.state["household_id"] == "hh-demo"


def test_stale_plan_is_refused_with_a_replan_instruction(stack):
    """If the household changes after the plan was shown, the parent's yes cannot approve it,
    and the model is told to plan again rather than to apply."""
    model = ScriptedModel(plan_turn())
    agent, s = start(stack, model)
    say(agent, s, "Alexa, fix our morning.")
    stack.service.report_availability_change("hh-demo", "linda", False, "changed outside the conversation",
                                             actor="test")
    model.script(decide("Yes, do it"), {"text": "Something changed since I made that plan, so I'll plan again."})
    say(agent, s, "Yes, do it.")
    consent = s.last_trace["consent"]
    assert consent and consent[0]["accepted"] is False
    assert consent[0]["reason"].startswith("STALE_PLAN") and "plan_disruption_recovery" in consent[0]["reason"]
    assert household(stack.db)["applied"] == 0


def test_apply_of_a_stale_plan_is_blocked_in_the_orchestrator(stack):
    model = ScriptedModel(plan_turn())
    agent, s = start(stack, model)
    say(agent, s, "Alexa, fix our morning.")
    stack.service.report_availability_change("hh-demo", "linda", False, "changed outside the conversation",
                                             actor="test")
    model.script(decide("Yes, do it"), apply_step(), {"text": "Nothing was changed."})
    say(agent, s, "Yes, do it.")
    blocked = s.last_trace["blocked"]
    assert blocked and "stale" in blocked[0]
    assert s.last_trace["unapproved_apply"] == 0  # stopped before it reached the server
    assert household(stack.db)["applied"] == 0


def test_screen_shows_the_household_as_stored_after_apply(stack):
    """After a turn that wrote, the orchestrator reads the household back, so the result
    screen shows the new assignments even if the model did not ask for them."""
    model = ScriptedModel(plan_turn())
    agent, s = start(stack, model)
    say(agent, s, "Alexa, fix our morning.")
    before = {e["id"]: e["assigned_member_id"] for e in s.state["escorts"]}
    model.script(decide("Yes, do it"), apply_step(), verify_step(), {"text": "Done. Everyone's set."})
    say(agent, s, "Yes, do it.")
    assert s.verify["ready"] is True
    after = {e["id"]: e["assigned_member_id"] for e in s.state["escorts"]}
    assert after != before and after == household(stack.db)["escorts"]
    assert any(c.get("by") == "orchestrator" and c["tool"] == "get_household_state" for c in s.last_trace["calls"])
