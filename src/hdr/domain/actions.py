"""Turn a feasible plan into concrete writes against the current state."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel

from hdr.domain.models import HOME, CalendarEvent, HouseholdSnapshot, SupervisionSegment
from hdr.domain.planner import PlanResult
from hdr.domain.timeutil import fmt, hm

ActionType = Literal[
    "assign_escort",
    "set_supervision",
    "cancel_calendar_event",
    "create_calendar_event",
    "notify_member",
]


class ActionSpec(BaseModel):
    seq: int
    type: ActionType
    target: dict[str, Any]
    prev: dict[str, Any] | None
    proposed: dict[str, Any] | None
    summary: str


def escort_title(snapshot: HouseholdSnapshot, child_id: str, destination: str) -> str:
    return f"Drive {snapshot.member(child_id).name} to {snapshot.location_name(destination)}"


def supervision_title(snapshot: HouseholdSnapshot, child_id: str) -> str:
    return f"With {snapshot.member(child_id).name} at home"


IMPLIED_BY_DRIVE_MAX_MIN = 30


def duty_event_id(member_id: str, ref: str, start: int) -> str:
    return f"duty-{member_id}-{ref}-{fmt(start).replace(':', '')}"


def derive_duty_events(snapshot: HouseholdSnapshot) -> list[CalendarEvent]:
    """Calendar entries that mirror escort and supervision duties."""
    events: list[CalendarEvent] = []
    for e in snapshot.escorts:
        if e.assigned_member_id and e.depart is not None and e.arrive is not None:
            events.append(
                CalendarEvent(
                    id=duty_event_id(e.assigned_member_id, e.id, e.depart),
                    member_id=e.assigned_member_id,
                    title=escort_title(snapshot, e.child_id, e.destination),
                    start=e.depart,
                    end=e.arrive,
                    location=e.destination,
                    hard=True,
                    kind="duty",
                    responsibility_id=e.id,
                    reminder_min=10,
                )
            )
    for s in snapshot.supervision:
        escort = snapshot.escort_for_child(s.child_id)
        implied = (
            escort is not None
            and escort.assigned_member_id == s.member_id
            and escort.depart == s.end
            and s.end - s.start <= IMPLIED_BY_DRIVE_MAX_MIN
        )
        if implied:
            # a few minutes with the child right before driving them is part of the drive
            continue
        events.append(
            CalendarEvent(
                id=duty_event_id(s.member_id, f"sup-{s.child_id}", s.start),
                member_id=s.member_id,
                title=supervision_title(snapshot, s.child_id),
                start=s.start,
                end=s.end,
                location=HOME,
                hard=True,
                kind="duty",
                responsibility_id=f"supervise:{s.child_id}",
                reminder_min=None,
            )
        )
    return events


def _event_fields(ev: CalendarEvent) -> dict[str, Any]:
    return {
        "member_id": ev.member_id,
        "title": ev.title,
        "start": fmt(ev.start),
        "end": fmt(ev.end),
        "location": ev.location,
        "kind": ev.kind,
        "responsibility_id": ev.responsibility_id,
        "reminder_min": ev.reminder_min,
        "status": ev.status,
    }


def apply_plan_to_snapshot(snapshot: HouseholdSnapshot, plan: PlanResult) -> HouseholdSnapshot:
    """The state the household should be in once every action succeeds."""
    target = snapshot.model_copy(deep=True)
    by_id = {a.escort_id: a for a in plan.assignments}
    for e in target.escorts:
        a = by_id.get(e.id)
        if a is not None:
            e.assigned_member_id, e.depart, e.arrive = a.member_id, hm(a.depart), hm(a.arrive)
    target.supervision = []
    counters: dict[str, int] = {}
    for seg in plan.supervision:
        counters[seg.child_id] = counters.get(seg.child_id, 0) + 1
        target.supervision.append(
            SupervisionSegment(
                id=f"sup-{seg.child_id}-{counters[seg.child_id]}",
                child_id=seg.child_id,
                member_id=seg.member_id,
                start=hm(seg.start),
                end=hm(seg.end),
            )
        )
    for ev in target.events:
        if ev.id in plan.released_events:
            ev.status = "cancelled"
    target.events = [ev for ev in target.events if ev.kind != "duty"] + derive_duty_events(target)
    return target


def _notification(snapshot: HouseholdSnapshot, after: HouseholdSnapshot, member_id: str, reason: str) -> str:
    name = snapshot.member(member_id).name
    before_duties = {(ev.title, ev.start, ev.end) for ev in snapshot.events if ev.kind == "duty" and ev.member_id == member_id and ev.status == "confirmed"}
    after_duties = {(ev.title, ev.start, ev.end) for ev in after.events if ev.kind == "duty" and ev.member_id == member_id and ev.status == "confirmed"}
    lines = [f"Hi {name}, {reason}. Updated plan:"]
    for title, start, end in sorted(after_duties - before_duties, key=lambda x: x[1]):
        lines.append(f"- NEW {fmt(start)}-{fmt(end)} {title}")
    for title, start, end in sorted(before_duties - after_duties, key=lambda x: x[1]):
        lines.append(f"- DROPPED {fmt(start)}-{fmt(end)} {title}")
    released = [ev for ev in after.events if ev.member_id == member_id and ev.status == "cancelled" and ev.kind == "commitment"]
    for ev in released:
        lines.append(f"- MOVED {ev.title} ({fmt(ev.start)}-{fmt(ev.end)})")
    return "\n".join(lines)


REASONS = {
    "school_delay": "school is starting late today",
    "school_closure": "school is closed today",
}


def build_actions(snapshot: HouseholdSnapshot, plan: PlanResult, disruption: str = "school_delay") -> list[ActionSpec]:
    if not plan.feasible:
        return []
    after = apply_plan_to_snapshot(snapshot, plan)
    actions: list[ActionSpec] = []

    def add(type_: ActionType, target: dict, prev: dict | None, proposed: dict | None, summary: str) -> None:
        actions.append(ActionSpec(seq=len(actions) + 1, type=type_, target=target, prev=prev, proposed=proposed, summary=summary))

    before_escorts = {e.id: e for e in snapshot.escorts}
    for e in after.escorts:
        b = before_escorts[e.id]
        if (b.assigned_member_id, b.depart, b.arrive) != (e.assigned_member_id, e.depart, e.arrive):
            who = snapshot.member(e.assigned_member_id).name if e.assigned_member_id else "nobody"
            was = snapshot.member(b.assigned_member_id).name if b.assigned_member_id else "nobody"
            add(
                "assign_escort",
                {"escort_id": e.id},
                {"member_id": b.assigned_member_id, "depart": fmt(b.depart), "arrive": fmt(b.arrive)},
                {"member_id": e.assigned_member_id, "depart": fmt(e.depart), "arrive": fmt(e.arrive)},
                f"{escort_title(snapshot, e.child_id, e.destination)}: {was} {fmt(b.depart)} -> {who} {fmt(e.depart)}",
            )

    for child in snapshot.children():
        before = sorted(
            ((s.member_id, fmt(s.start), fmt(s.end)) for s in snapshot.supervision if s.child_id == child.id),
            key=lambda x: (x[1], x[0]),
        )
        now = sorted(
            ((s.member_id, fmt(s.start), fmt(s.end)) for s in after.supervision if s.child_id == child.id),
            key=lambda x: (x[1], x[0]),
        )
        if before != now:
            add(
                "set_supervision",
                {"child_id": child.id},
                {"segments": [list(x) for x in before]},
                {"segments": [list(x) for x in now]},
                f"{child.name} at home: " + ", ".join(f"{snapshot.member(m).name} {s}-{e}" for m, s, e in now),
            )

    before_events = {ev.id: ev for ev in snapshot.events}
    after_events = {ev.id: ev for ev in after.events}
    for ev_id, ev in before_events.items():
        if ev.status != "confirmed":
            continue
        new = after_events.get(ev_id)
        if new is None or new.status == "cancelled":
            add(
                "cancel_calendar_event",
                {"event_id": ev_id},
                _event_fields(ev),
                {"status": "cancelled"},
                f"Cancel {snapshot.member(ev.member_id).name}'s '{ev.title}' {fmt(ev.start)}",
            )
    for ev_id, ev in after_events.items():
        if ev.kind == "duty" and ev.status == "confirmed" and ev_id not in before_events:
            add(
                "create_calendar_event",
                {"event_id": ev_id},
                None,
                _event_fields(ev),
                f"Add '{ev.title}' {fmt(ev.start)}-{fmt(ev.end)} to {snapshot.member(ev.member_id).name}'s calendar",
            )

    for member_id in plan.changed_members:
        add(
            "notify_member",
            {"member_id": member_id},
            None,
            {"message": _notification(snapshot, after, member_id, REASONS.get(disruption, "plans changed today")),
             "channel": "push"},
            f"Notify {snapshot.member(member_id).name}",
        )
    return actions


def actions_hash(actions: list[ActionSpec]) -> str:
    payload = json.dumps([a.model_dump() for a in actions], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
