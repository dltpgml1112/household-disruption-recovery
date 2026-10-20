"""The device cards the MCP tools return: one title, at most two supporting fields, one action
(the shape the Alexa+ add-on design guide asks for)."""

from pathlib import Path

import pytest

from hdr.service.recovery import RecoveryService, Settings


@pytest.fixture
def service(tmp_path: Path) -> RecoveryService:
    return RecoveryService(Settings(db_path=str(tmp_path / "hdr.sqlite3"), approval_secret=b"card-test-secret-0123456789abcd"))


def plan(service: RecoveryService, variant: str | None = None):
    service.reset(variant)
    notice = service.demo_notice()
    ingested = service.ingest_school_notice("hh-demo", notice["raw"], notice["source"], notice["received_at"])
    state = service.get_household_state("hh-demo")
    return service.plan_disruption_recovery(ingested.disruption_id, state.state_version)


def test_plan_card(service):
    card = plan(service).card
    assert card.title == "Sarah takes Noah at 7:35; Linda takes Emma at 9:55"
    assert card.fields == ["Emma at home: Mike 7:30–8:30, Linda 8:30–9:55", "13 changes · messages to Sarah, Mike, Linda"]
    assert card.action == "Apply 13 changes"


def test_no_plan_card(service):
    card = plan(service, "linda-unavailable").card
    assert card.title == "No plan covers everyone"
    assert card.fields[0] == "No approved adult can be with Emma 8:30–10:00"
    assert len(card.fields) <= 2 and card.action


def test_verify_cards(service):
    p = plan(service)
    service.add_fault("hh-demo", "notify", "linda", times=1)
    token = service.approve_recovery_plan(p.plan_id, p.version, "approve", "sarah").approval_token
    service.apply_recovery_plan(p.plan_id, token, "card-test-key-1")
    failed = service.verify_recovery(p.plan_id).card
    assert failed.title == "Not ready: 1 change did not go through"
    assert failed.fields == ["Failed: Notify Linda", "12/13 changes read back"] and failed.action == "Try again"
    service.apply_recovery_plan(p.plan_id, token, "card-test-key-2")
    ready = service.verify_recovery(p.plan_id).card
    assert ready.title == "Household ready" and ready.fields[0] == "13/13 changes read back" and ready.action is None
