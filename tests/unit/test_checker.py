from hdr.domain.actions import apply_plan_to_snapshot, build_actions, actions_hash
from hdr.domain.checker import check
from hdr.domain.planner import plan

from tests.conftest import closed


def _codes(state):
    return sorted(v.code for v in check(state).violations)


def test_baseline_and_planned_states_are_clean(demo_baseline, demo_delayed):
    assert check(demo_baseline).ok
    target = apply_plan_to_snapshot(demo_delayed, plan(demo_delayed))
    report = check(target)
    assert report.ok, report.violations
    assert report.checked_children == 2


def test_delay_alone_makes_the_old_plan_invalid(demo_delayed):
    assert "ARRIVAL_WINDOW" in _codes(demo_delayed)


def _target(demo_delayed):
    return apply_plan_to_snapshot(demo_delayed, plan(demo_delayed))


def test_unassigned_escort_is_caught(demo_delayed):
    t = _target(demo_delayed)
    t.escorts[0].assigned_member_id = None
    report = check(t)
    assert report.unassigned == ["r-emma-school"]


def test_supervision_gap_is_caught(demo_delayed):
    t = _target(demo_delayed)
    t.supervision = [s for s in t.supervision if s.member_id != "linda"]
    assert "SUPERVISION_GAP" in _codes(t)


def test_permission_is_enforced(demo_delayed):
    t = _target(demo_delayed)
    noah = next(e for e in t.escorts if e.child_id == "noah")
    noah.assigned_member_id = "linda"  # Linda is approved for school drop-off only
    assert "NOT_PERMITTED" in _codes(t)


def test_double_booking_is_caught(demo_delayed):
    t = _target(demo_delayed)
    emma = next(e for e in t.escorts if e.child_id == "emma")
    emma.assigned_member_id = "sarah"  # Sarah is at work
    assert "OVERLAP" in _codes(t)


def test_unavailable_member_with_duties_is_caught(demo_delayed):
    t = _target(demo_delayed)
    t.member("linda").available = False
    assert "UNAVAILABLE" in _codes(t)


def test_travel_time_mismatch_is_caught(demo_delayed):
    t = _target(demo_delayed)
    t.escorts[0].arrive += 5
    assert "TRAVEL_TIME" in _codes(t)


def test_actions_for_target_plan(demo_delayed):
    r = plan(demo_delayed)
    acts = build_actions(demo_delayed, r)
    types = [a.type for a in acts]
    assert types.count("assign_escort") == 2
    assert types.count("set_supervision") == 2
    assert types.count("notify_member") == 3
    assert types[-3:] == ["notify_member"] * 3  # notifications go last
    created = [a.proposed["title"] + "@" + a.proposed["member_id"] for a in acts if a.type == "create_calendar_event"]
    assert sorted(created) == sorted(
        [
            "Drive Emma to Maple Elementary@linda",
            "Drive Noah to Little Oaks Daycare@sarah",
            "With Emma at home@mike",
            "With Emma at home@linda",
        ]
    )
    assert actions_hash(acts) == actions_hash(build_actions(demo_delayed, plan(demo_delayed)))


def test_infeasible_plan_has_no_actions():
    from tests.conftest import delayed

    snap, _ = delayed("linda-unavailable")
    assert build_actions(snap, plan(snap)) == []


def test_closed_school_needs_an_adult_until_the_day_ends():
    snap, _ = closed()
    # the morning-only coverage that was enough for a school day leaves Emma alone after 08:10
    assert "SUPERVISION_GAP" in _codes(snap)
    target = apply_plan_to_snapshot(snap, plan(snap))
    report = check(target)
    assert report.ok, report.violations
    assert report.checked_children == 2
    # drop Mike's afternoon at home and the checker sees the gap to 18:00
    target.supervision = [s for s in target.supervision if not (s.member_id == "mike" and s.start > 600)]
    assert any(v.code == "SUPERVISION_GAP" and "18:00" in v.detail for v in check(target).violations)


def test_closure_notifications_say_why(demo_school):
    snap, _ = closed()
    acts = build_actions(snap, plan(snap), "school_closure")
    messages = [a.proposed["message"] for a in acts if a.type == "notify_member"]
    assert len(messages) == 3
    assert all("school is closed today" in m for m in messages)
    assert any("MOVED Office work" in m for m in messages)
