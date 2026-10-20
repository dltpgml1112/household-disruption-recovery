"""The reference agent drives the whole workflow over real Streamable HTTP MCP calls."""

from __future__ import annotations

import sqlite3

import anyio
import pytest

from hdr.orchestrator.agent_rules import RuleAgent, detect_intent, new_session

pytestmark = pytest.mark.integration

NOTICE = {
    "raw": "Due to severe weather, school will operate on a two-hour delay. Doors open at 10:05 AM and classes begin at 10:15 AM.",
    "source": "Maple Elementary text alert",
    "received_at": "2026-10-15T06:15:00-04:00",
}


def say(agent: RuleAgent, session, text: str, channel: str = "voice") -> str:
    return anyio.run(agent.handle, session, text, channel)


def escorts(db) -> dict:
    with sqlite3.connect(db) as conn:
        return {r[0]: r[1] for r in conn.execute("SELECT id, assigned_member_id FROM escorts")}


def count(db, sql: str) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute(sql).fetchone()[0]


def test_intents():
    assert detect_intent("Alexa, fix our morning.")[0] == "FIX"
    assert detect_intent("Yes, do it")[0] == "APPROVE"
    assert detect_intent("No, don't")[0] == "REJECT"
    assert detect_intent("Linda can't come today") == ("AVAILABILITY_OFF", {"name": "Linda"})
    assert detect_intent("try again")[0] == "RETRY"


def test_killer_scenario(stack):
    agent, s = RuleAgent(stack.mcp_url), new_session()
    s.pending_notice = dict(NOTICE)

    speech = say(agent, s, "Alexa, fix our morning.")
    assert s.screen == "C"
    assert "School starts two hours late, so 2 things break" in speech
    assert "Sarah takes Noah at 7:35." in speech
    assert "Mike stays with Emma until 8:30." in speech
    assert "Linda takes over at 8:30 and drives Emma at 9:55." in speech
    assert escorts(stack.db)["r-emma-school"] == "sarah"  # nothing written before approval

    wire = s.last_trace["wire"]
    methods = [w["method"] for w in wire]
    assert methods[0] == "initialize" and "tools/list" in methods
    calls = [w.get("tool") for w in wire if w["method"] == "tools/call"]
    assert calls == ["get_household_state", "ingest_school_notice", "get_household_state", "plan_disruption_recovery"]
    assert s.last_trace["protocol_version"] == "2025-11-25"
    assert all(w["protocol_header"] == "2025-11-25" for w in wire if w["method"] not in ("initialize", None))

    speech = say(agent, s, "Yes, do it.")
    assert s.screen == "D"
    assert s.verify["ready"] is True
    assert speech.startswith("Done. All 13 changes are in")
    assert escorts(stack.db) == {"r-emma-school": "linda", "r-noah-daycare": "sarah"}
    calls = [w.get("tool") for w in s.last_trace["wire"] if w["method"] == "tools/call"]
    assert calls == ["approve_recovery_plan", "apply_recovery_plan", "verify_recovery", "get_household_state"]


def test_partial_failure_is_not_reported_ready_then_retry_fixes_it(stack):
    agent, s = RuleAgent(stack.mcp_url), new_session()
    s.pending_notice = dict(NOTICE)
    say(agent, s, "fix our morning")
    stack.service.add_fault("hh-demo", "notify", "linda", times=1)

    speech = say(agent, s, "yes")
    assert s.apply["status"] == "partially_applied"
    assert s.verify["ready"] is False
    assert "12 of 13 changes went through" in speech and "Notify Linda" in speech

    speech = say(agent, s, "try again")
    assert s.verify["ready"] is True
    assert s.apply["skipped_already_applied"] == 12 and s.apply["applied"] == 1
    assert count(stack.db, "SELECT COUNT(*) FROM notifications") == 3  # no duplicate messages


def test_no_feasible_plan_never_writes(stack_linda_unavailable):
    stack = stack_linda_unavailable
    agent, s = RuleAgent(stack.mcp_url), new_session()
    s.pending_notice = dict(NOTICE)
    speech = say(agent, s, "fix our morning")
    assert s.plan["feasible"] is False
    assert "couldn't find a plan" in speech
    assert "8:30 to 10:00" in speech and "Linda is unavailable today" in speech

    speech = say(agent, s, "yes")
    assert speech == "There's nothing waiting for your approval."
    assert escorts(stack.db)["r-emma-school"] == "sarah"
    assert count(stack.db, "SELECT COUNT(*) FROM approvals") == 0
    assert count(stack.db, "SELECT COUNT(*) FROM notifications") == 0


def test_caregiver_drops_out_after_plan_is_shown(stack):
    agent, s = RuleAgent(stack.mcp_url), new_session()
    s.pending_notice = dict(NOTICE)
    say(agent, s, "fix our morning")
    first_plan = s.plan["plan_id"]

    speech = say(agent, s, "Linda can't come today")
    assert "Linda can't help today" in speech
    assert s.plan["plan_id"] != first_plan and s.plan["feasible"] is False

    # approving the old plan is impossible: it was invalidated by the state change
    stack_plan_status = count(stack.db, f"SELECT COUNT(*) FROM plans WHERE id='{first_plan}' AND status='stale'")
    assert stack_plan_status == 1
    assert escorts(stack.db)["r-emma-school"] == "sarah"


def test_stale_plan_is_replanned_before_anything_is_written(stack):
    agent, s = RuleAgent(stack.mcp_url), new_session()
    s.pending_notice = dict(NOTICE)
    say(agent, s, "fix our morning")
    # someone changes the household behind the agent's back
    stack.service.report_availability_change("hh-demo", "linda", False, "changed elsewhere")
    speech = say(agent, s, "yes")
    assert "Something changed since I made that plan, so nothing was applied." in speech
    assert count(stack.db, "SELECT COUNT(*) FROM notifications") == 0


def test_read_timeout_is_retried(stack):
    agent, s = RuleAgent(stack.mcp_url, timeout_s=1.0), new_session()
    stack.service.add_fault("*", "tool_delay", "get_household_state", times=1, delay_ms=2500)
    say(agent, s, "are we good?")
    attempts = [c for c in s.last_trace["calls"] if c["tool"] == "get_household_state"]
    assert attempts[0]["error"] == "TIMEOUT" and attempts[-1]["ok"] is True
    assert not s.degraded


def test_mcp_down_is_reported_as_degraded(tmp_path):
    agent, s = RuleAgent("http://127.0.0.1:9/mcp", timeout_s=1.0), new_session()
    speech = say(agent, s, "fix our morning")
    assert s.degraded and "haven't changed anything" in speech


def test_low_confidence_notice_asks_instead_of_planning(stack):
    agent, s = RuleAgent(stack.mcp_url), new_session()
    s.pending_notice = {**NOTICE, "raw": "Two-hour delay today. Classes begin at 9:15 AM."}
    speech = say(agent, s, "fix our morning")
    assert s.screen == "B" and s.plan is None
    assert "not sure" in speech
    assert count(stack.db, "SELECT COUNT(*) FROM disruptions") == 0


CLOSURE = {
    "raw": "A water main break has left Maple Elementary without running water. School is closed today, "
           "Thursday, October 15. All other district schools are open.",
    "source": "Maple Elementary text alert",
    "received_at": "2026-10-15T06:05:00-04:00",
}


def test_school_closure_runs_through_the_same_workflow(stack):
    agent, s = RuleAgent(stack.mcp_url), new_session()
    s.pending_notice = dict(CLOSURE)
    speech = say(agent, s, "Alexa, fix our morning.")
    assert speech.startswith("School is closed today, so 1 thing breaks: Emma would be home without an adult")
    assert "Mike's office work from 9:45 comes off the calendar." in speech
    with sqlite3.connect(stack.db) as conn:
        assert conn.execute("SELECT status FROM escorts WHERE id='r-emma-school'").fetchone()[0] == "cancelled"

    speech = say(agent, s, "Yes, do it.")
    assert s.verify["ready"] is True, s.verify
    assert speech.startswith("Done. All 13 changes are in")
    assert count(stack.db, "SELECT COUNT(*) FROM notifications") == 3


def test_school_closure_without_linda_stops_without_writing(stack_linda_unavailable):
    stack = stack_linda_unavailable
    agent, s = RuleAgent(stack.mcp_url), new_session()
    s.pending_notice = dict(CLOSURE)
    speech = say(agent, s, "fix our morning")
    assert s.plan["feasible"] is False and "couldn't find a plan" in speech
    assert count(stack.db, "SELECT COUNT(*) FROM notifications") == 0
    assert count(stack.db, "SELECT COUNT(*) FROM approvals") == 0


def test_conditional_yes_is_not_applied(stack):
    # found by the scenario eval (S24): "^yes" alone used to approve and apply
    agent, s = RuleAgent(stack.mcp_url), new_session()
    s.pending_notice = dict(NOTICE)
    say(agent, s, "fix our morning")
    speech = say(agent, s, "Yes, but don't text Linda yet.")
    assert "haven't changed anything" in speech
    assert count(stack.db, "SELECT COUNT(*) FROM approvals") == 0
    assert escorts(stack.db)["r-emma-school"] == "sarah"


def test_non_parent_approval_is_refused_in_words_not_a_crash(stack):
    # found by the scenario eval (S26): the server's NOT_AUTHORIZED used to escape the turn
    agent, s = RuleAgent(stack.mcp_url), new_session()
    s.approver_member_id = "linda"
    s.pending_notice = dict(NOTICE)
    say(agent, s, "fix our morning")
    speech = say(agent, s, "Yes, do it.")
    assert speech == "Only a parent can approve this plan, so I haven't applied it."
    assert s.error["code"] == "NOT_AUTHORIZED"
    assert count(stack.db, "SELECT COUNT(*) FROM approvals WHERE decision='approve'") == 0
