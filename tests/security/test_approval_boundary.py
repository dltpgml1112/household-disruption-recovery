"""Attacks on the approval boundary, made over real MCP HTTP calls.

After every rejected attempt the household must be unchanged: no escort moved,
no calendar entry touched, no message sent.
"""

from __future__ import annotations

import base64
import json
import sqlite3

import pytest

from tests.integration.conftest import start_stack
from tests.integration.live import RawMcp

pytestmark = pytest.mark.integration

RAW = (
    "Due to severe weather, school will operate on a two-hour delay. "
    "Doors open at 10:05 AM and classes begin at 10:15 AM."
)


def fingerprint(db) -> tuple:
    with sqlite3.connect(db) as conn:
        return (
            tuple(conn.execute("SELECT id, assigned_member_id, depart FROM escorts ORDER BY id")),
            tuple(conn.execute("SELECT id, member_id, status FROM calendar_events ORDER BY id")),
            conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM actions WHERE status='applied'").fetchone()[0],
        )


@pytest.fixture
def env(tmp_path):
    service, db, live = start_stack(tmp_path)
    with live:
        mcp = RawMcp(live.url)
        mcp.initialize()
        notice = mcp.ok("ingest_school_notice", {"household_id": "hh-demo", "raw_notice": RAW,
                                                 "source": "sms", "received_at": "2026-10-15T06:15:00-04:00"})
        plan = mcp.ok("plan_disruption_recovery", {"disruption_id": notice["disruption_id"],
                                                   "household_state_version": notice["state_version"]})
        yield mcp, db, service, notice, plan


def approve(mcp, plan, who="sarah", version=None):
    return mcp.ok("approve_recovery_plan", {"plan_id": plan["plan_id"], "plan_version": version or plan["version"],
                                            "decision": "approve", "approver_member_id": who, "channel": "screen"})


def test_planning_writes_nothing(env):
    mcp, db, _, notice, plan = env
    state = mcp.ok("get_household_state", {"household_id": "hh-demo"})
    assert state["state_version"] == notice["state_version"]
    assert {e["id"]: e["assigned_member_id"] for e in state["escorts"]}["r-emma-school"] == "sarah"


def test_apply_without_or_with_forged_token_is_refused(env):
    mcp, db, _, _, plan = env
    before = fingerprint(db)
    args = {"plan_id": plan["plan_id"], "idempotency_key": "attack-0001"}
    assert mcp.error("apply_recovery_plan", {**args, "approval_token": ""}) == "APPROVAL_REQUIRED"
    assert mcp.error("apply_recovery_plan", {**args, "approval_token": "not-a-token"}) == "APPROVAL_INVALID"
    forged_body = base64.urlsafe_b64encode(json.dumps(
        {"p": plan["plan_id"], "v": plan["version"], "s": plan["base_state_version"], "h": "0" * 64, "e": 4102444800}
    ).encode()).rstrip(b"=").decode()
    assert mcp.error("apply_recovery_plan", {**args, "approval_token": forged_body + ".AAAA"}) == "APPROVAL_INVALID"
    assert fingerprint(db) == before


def test_tampered_token_payload_is_refused(env):
    mcp, db, _, _, plan = env
    token = approve(mcp, plan)["approval_token"]
    body, sig = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims["v"] += 1  # pretend it approved a later version
    tampered = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode() + "." + sig
    before = fingerprint(db)
    assert mcp.error("apply_recovery_plan", {"plan_id": plan["plan_id"], "approval_token": tampered,
                                             "idempotency_key": "attack-0002"}) == "APPROVAL_INVALID"
    assert fingerprint(db) == before


def test_token_from_an_older_plan_version_is_refused(env):
    mcp, db, _, notice, plan_v1 = env
    token_v1 = approve(mcp, plan_v1)["approval_token"]
    plan_v2 = mcp.ok("plan_disruption_recovery", {"disruption_id": notice["disruption_id"],
                                                  "household_state_version": notice["state_version"]})
    assert plan_v2["version"] == plan_v1["version"] + 1
    assert plan_v1["plan_id"] in plan_v2["superseded_plans"]
    before = fingerprint(db)
    # old token on the new plan
    assert mcp.error("apply_recovery_plan", {"plan_id": plan_v2["plan_id"], "approval_token": token_v1,
                                             "idempotency_key": "attack-0003"}) == "APPROVAL_INVALID"
    # old token on its own, now superseded, plan
    assert mcp.error("apply_recovery_plan", {"plan_id": plan_v1["plan_id"], "approval_token": token_v1,
                                             "idempotency_key": "attack-0004"}) == "APPROVAL_INVALID"
    assert fingerprint(db) == before


def test_manipulated_plan_version_on_approve(env):
    mcp, _, _, _, plan = env
    code = mcp.error("approve_recovery_plan", {"plan_id": plan["plan_id"], "plan_version": plan["version"] + 7,
                                               "decision": "approve", "approver_member_id": "sarah"})
    assert code == "PLAN_VERSION_MISMATCH"


@pytest.mark.parametrize("who,code", [("linda", "NOT_AUTHORIZED"), ("emma", "NOT_AUTHORIZED"), ("eve", "INVALID_MEMBER")])
def test_only_parents_can_approve(env, who, code):
    mcp, db, _, _, plan = env
    before = fingerprint(db)
    assert mcp.error("approve_recovery_plan", {"plan_id": plan["plan_id"], "plan_version": plan["version"],
                                               "decision": "approve", "approver_member_id": who}) == code
    assert fingerprint(db) == before


def test_invalid_member_in_availability_report(env):
    mcp, _, _, _, _ = env
    assert mcp.error("report_availability_change", {"household_id": "hh-demo", "member_id": "eve",
                                                    "available": False}) == "INVALID_MEMBER"
    assert mcp.error("report_availability_change", {"household_id": "hh-demo", "member_id": "emma",
                                                    "available": False}) == "INVALID_MEMBER"


def test_duplicate_execution_is_prevented(env):
    mcp, db, _, _, plan = env
    token = approve(mcp, plan)["approval_token"]
    args = {"plan_id": plan["plan_id"], "approval_token": token, "idempotency_key": "apply-once-0001"}
    first = mcp.ok("apply_recovery_plan", args)
    after_first = fingerprint(db)
    replay = mcp.ok("apply_recovery_plan", args)
    assert replay["idempotent_replay"] is True and replay["results"] == first["results"]
    assert fingerprint(db) == after_first
    assert mcp.error("apply_recovery_plan", {**args, "idempotency_key": "apply-once-0002"}) == "PLAN_ALREADY_APPLIED"
    assert mcp.error("apply_recovery_plan", {**args, "approval_token": token + "x"}) in ("IDEMPOTENCY_CONFLICT", "APPROVAL_INVALID")
    assert fingerprint(db) == after_first
    assert after_first[2] == 3  # three messages, sent once


def test_state_change_after_approval_blocks_apply(env):
    mcp, db, _, _, plan = env
    token = approve(mcp, plan)["approval_token"]
    mcp.ok("report_availability_change", {"household_id": "hh-demo", "member_id": "linda", "available": False})
    before = fingerprint(db)
    code = mcp.error("apply_recovery_plan", {"plan_id": plan["plan_id"], "approval_token": token,
                                             "idempotency_key": "stale-0001"})
    assert code in ("APPROVAL_INVALID", "STALE_PLAN")
    assert fingerprint(db) == before


def test_expired_token_is_refused(tmp_path):
    service, db, live = start_stack(tmp_path, ttl=-1)
    with live:
        mcp = RawMcp(live.url)
        mcp.initialize()
        n = mcp.ok("ingest_school_notice", {"household_id": "hh-demo", "raw_notice": RAW, "source": "sms",
                                            "received_at": "2026-10-15T06:15:00-04:00"})
        p = mcp.ok("plan_disruption_recovery", {"disruption_id": n["disruption_id"],
                                                "household_state_version": n["state_version"]})
        token = approve(mcp, p)["approval_token"]
        before = fingerprint(db)
        assert mcp.error("apply_recovery_plan", {"plan_id": p["plan_id"], "approval_token": token,
                                                 "idempotency_key": "expired-0001"}) == "APPROVAL_EXPIRED"
        assert fingerprint(db) == before


def test_every_attempt_is_audited(env):
    mcp, _, _, notice, plan = env
    mcp.error("apply_recovery_plan", {"plan_id": plan["plan_id"], "approval_token": "x.y", "idempotency_key": "audit-0001"})
    log = mcp.ok("get_audit_log", {"disruption_id": notice["disruption_id"]})
    assert "apply_recovery_plan:APPROVAL_INVALID" in log["summary"]["errors"]
    raw_inputs = json.dumps([e["input"] for e in log["events"]])
    assert "severe weather" not in raw_inputs  # raw notice text is not stored in the audit log
