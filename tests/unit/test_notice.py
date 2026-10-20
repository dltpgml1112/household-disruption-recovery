from hdr.domain.notice import StructuredNotice, apply_closure, apply_delay, parse_notice

DEMO = (
    "Due to severe weather, school will operate on a two-hour delay. "
    "Doors open at 10:05 AM and classes begin at 10:15 AM."
)


def test_demo_notice_is_a_confident_two_hour_delay(demo_school):
    n = parse_notice(DEMO, demo_school)
    assert n.kind == "delay"
    assert n.delay_minutes == 120
    assert (n.doors_open, n.classes_begin) == ("10:05", "10:15")
    assert n.confidence >= 0.9
    assert not n.needs_clarification


def test_output_round_trips_through_schema(demo_school):
    n = parse_notice(DEMO, demo_school)
    assert StructuredNotice.model_validate(n.model_dump()) == n
    assert StructuredNotice.model_json_schema()["properties"]["kind"]["enum"] == [
        "delay", "closure", "early_dismissal", "unknown"
    ]


def test_delay_without_times_derives_them_from_the_normal_schedule(demo_school):
    n = parse_notice("Two-hour delay today for all district schools.", demo_school)
    assert (n.doors_open, n.classes_begin) == ("10:05", "10:15")
    assert 0.8 <= n.confidence < 0.9
    assert not n.needs_clarification


def test_minutes_delay(demo_school):
    n = parse_notice("Schools will open on a 90-minute delay.", demo_school)
    assert n.delay_minutes == 90
    assert n.classes_begin == "09:45"


def test_contradicting_times_ask_for_clarification(demo_school):
    n = parse_notice("Two-hour delay. Classes begin at 9:15 AM.", demo_school)
    assert n.needs_clarification
    assert n.questions


def test_unrelated_text_is_not_guessed(demo_school):
    n = parse_notice("Reminder: picture day is next Tuesday.", demo_school)
    assert n.kind == "unknown"
    assert n.needs_clarification


def test_closure_and_early_dismissal_are_recognised(demo_school):
    closure = parse_notice("All schools are closed today due to snow.", demo_school)
    assert closure.kind == "closure" and not closure.needs_clarification
    assert parse_notice("Early dismissal at 12:30 PM today.", demo_school).kind == "early_dismissal"


def test_apply_closure_drops_only_school_trips(demo_baseline, demo_school):
    n = parse_notice("School is closed today because of a water main break.", demo_school)
    after, cancelled = apply_closure(demo_baseline, n)
    assert cancelled == ["r-emma-school"]
    assert [e.id for e in after.escorts] == ["r-noah-daycare"]
    assert [e.id for e in demo_baseline.escorts] == ["r-emma-school", "r-noah-daycare"]  # input untouched


def test_apply_delay_moves_only_school_windows(demo_baseline, demo_school):
    n = parse_notice(DEMO, demo_school)
    after, affected = apply_delay(demo_baseline, n)
    assert affected == ["r-emma-school"]
    emma = next(e for e in after.escorts if e.id == "r-emma-school")
    noah = next(e for e in after.escorts if e.id == "r-noah-daycare")
    assert (emma.window_start, emma.window_end) == (605, 615)
    assert emma.assigned_member_id == "sarah"  # assignments untouched
    assert noah == next(e for e in demo_baseline.escorts if e.id == "r-noah-daycare")
