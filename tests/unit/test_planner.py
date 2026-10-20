import pytest

from hdr.domain.planner import PlannerLimitExceeded, plan
from hdr.domain.propagation import propagate

from tests.conftest import closed, delayed


def _by_escort(result):
    return {a.escort_id: (a.member_id, a.depart, a.arrive) for a in result.assignments}


def test_killer_scenario_target_plan(demo_delayed):
    r = plan(demo_delayed)
    assert r.feasible
    assert _by_escort(r) == {
        "r-emma-school": ("linda", "09:55", "10:05"),
        "r-noah-daycare": ("sarah", "07:35", "08:00"),
    }
    emma = [(s.member_id, s.start, s.end) for s in r.supervision if s.child_id == "emma"]
    assert emma == [("mike", "07:30", "08:30"), ("linda", "08:30", "09:55")]
    assert r.engaged_caregivers == ["linda"]
    assert r.released_events == []
    assert r.objective.violations == 0
    assert r.objective.lateness_min == 0
    assert r.objective.changed_commitments == 3
    assert r.changed_members == ["sarah", "mike", "linda"]


def test_rejected_alternatives_are_explained(demo_delayed):
    r = plan(demo_delayed)
    emma = next(d for d in r.decisions if d.escort_id == "r-emma-school")
    alts = {a.member_id: a for a in emma.alternatives}
    assert alts["sarah"].outcome == "infeasible"
    assert "Work" in alts["sarah"].detail
    # Mike could drive after his meeting only by moving his soft office block
    assert alts["mike"].outcome == "worse"
    assert "changes more commitments" in alts["mike"].detail

    noah = next(d for d in r.decisions if d.escort_id == "r-noah-daycare")
    mike = next(a for a in noah.alternatives if a.member_id == "mike")
    assert mike.outcome == "infeasible"
    assert "Emma would be home without an adult 08:10-08:25" in mike.detail


def test_linda_unavailable_has_no_feasible_plan():
    snap, _ = delayed("linda-unavailable")
    r = plan(snap)
    assert not r.feasible
    gaps = [u for u in r.unresolved if u.code == "SUPERVISION_GAP"]
    assert len(gaps) == 1 and gaps[0].child_id == "emma"
    assert gaps[0].interval == "08:30-10:00"
    assert "Linda is unavailable today" in gaps[0].blockers
    assert any("Client meeting" in b for b in gaps[0].blockers)
    assert r.options[0].startswith("Find another approved adult for Emma")


def test_planner_is_deterministic(demo_delayed):
    assert plan(demo_delayed) == plan(demo_delayed)


def test_normal_day_needs_no_changes(demo_baseline):
    r = plan(demo_baseline)
    assert r.feasible
    assert r.objective.changed_commitments == 0
    assert r.changed_members == []
    assert _by_escort(r) == {
        "r-emma-school": ("sarah", "07:55", "08:05"),
        "r-noah-daycare": ("mike", "07:35", "08:00"),
    }


def test_search_space_limit_is_enforced(demo_delayed):
    with pytest.raises(PlannerLimitExceeded):
        plan(demo_delayed, max_combinations=10)


def test_propagation_finds_the_two_conflicts():
    snap, affected = delayed()
    p = propagate(snap, affected)
    codes = [(c.code, c.responsibility_id or c.child_id) for c in p.conflicts]
    assert codes == [("OWNER_UNAVAILABLE", "r-emma-school"), ("SUPERVISION_GAP", "emma")]
    owner = p.conflicts[0]
    assert owner.member_id == "sarah" and "Work" in owner.detail
    assert p.conflicts[1].intervals == ["08:10-08:25", "08:30-09:55"]


def test_hard_commitments_are_never_released(demo_delayed):
    r = plan(demo_delayed)
    hard = {e.id for e in demo_delayed.events if e.hard}
    assert not hard & set(r.released_events)


def test_school_closure_uses_the_same_engine():
    # generalisation check: a different disruption, same planner and objective
    snap, cancelled = closed()
    r = plan(snap)
    assert cancelled == ["r-emma-school"]
    assert r.feasible
    assert _by_escort(r) == {"r-noah-daycare": ("sarah", "07:35", "08:00")}
    emma = [(s.member_id, s.start, s.end) for s in r.supervision if s.child_id == "emma"]
    # Emma is home all day: Mike until his meeting, Linda, then Mike working from home
    assert emma == [("mike", "07:30", "08:30"), ("linda", "08:30", "10:30"), ("mike", "10:30", "18:00")]
    assert r.released_events == ["ev-mike-work"]
    assert r.engaged_caregivers == ["linda"]
    assert r.objective.violations == 0


def test_school_closure_without_linda_is_infeasible():
    snap, _ = closed("linda-unavailable")
    r = plan(snap)
    assert not r.feasible
    assert [u.code for u in r.unresolved] == ["SUPERVISION_GAP"]
    assert r.unresolved[0].child_id == "emma"
