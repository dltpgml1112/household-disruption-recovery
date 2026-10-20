"""Constraint checker used for read-back verification.

Deliberately independent of the planner: it does not derive anything. It takes
the explicit state that was written (escort owners and times, supervision
segments, calendar commitments) and checks each rule directly. A bug in the
planner's itinerary logic therefore cannot hide itself during verification.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from hdr.domain.models import HOME, HouseholdSnapshot
from hdr.domain.timeutil import fmt, fmt_range, merge, subtract


class Violation(BaseModel):
    code: str
    detail: str
    refs: list[str] = Field(default_factory=list)
    critical: bool = True


class CheckReport(BaseModel):
    ok: bool
    violations: list[Violation]
    unassigned: list[str]
    checked_escorts: int
    checked_children: int


@dataclass(frozen=True)
class _Slot:
    start: int
    end: int
    start_loc: str
    end_loc: str
    ref: str
    label: str


def check(state: HouseholdSnapshot) -> CheckReport:
    violations: list[Violation] = []
    unassigned: list[str] = []
    slots: dict[str, list[_Slot]] = {m.id: [] for m in state.members}
    home_slots: dict[str, list[tuple[int, int]]] = {m.id: [] for m in state.members}

    for e in state.escorts:
        child = state.member(e.child_id) if state.has_member(e.child_id) else None
        if e.assigned_member_id is None:
            if e.required:
                unassigned.append(e.id)
                violations.append(Violation(code="UNASSIGNED", detail=f"{e.id} has no owner", refs=[e.id]))
            continue
        if not state.has_member(e.assigned_member_id):
            violations.append(Violation(code="UNKNOWN_MEMBER", detail=f"{e.id} owner {e.assigned_member_id} does not exist", refs=[e.id]))
            continue
        owner = state.member(e.assigned_member_id)
        if not owner.is_adult or e.child_id not in owner.can_escort:
            violations.append(Violation(code="NOT_PERMITTED", detail=f"{owner.name} is not approved to take {e.child_id}", refs=[e.id]))
        if e.depart is None or e.arrive is None:
            violations.append(Violation(code="MISSING_TIME", detail=f"{e.id} has no departure/arrival time", refs=[e.id]))
            continue
        expected = state.travel_minutes(HOME, e.destination)
        if e.arrive - e.depart != expected:
            violations.append(
                Violation(code="TRAVEL_TIME", detail=f"{e.id}: {fmt_range(e.depart, e.arrive)} but the trip takes {expected} min", refs=[e.id])
            )
        if not e.window_start <= e.arrive <= e.window_end + e.max_late:
            violations.append(
                Violation(
                    code="ARRIVAL_WINDOW",
                    detail=f"{e.id} arrives {fmt(e.arrive)}, allowed {fmt(e.window_start)}-{fmt(e.window_end + e.max_late)}",
                    refs=[e.id],
                )
            )
        if child is not None and child.ready_at is not None and e.depart < child.ready_at:
            violations.append(Violation(code="BEFORE_READY", detail=f"{e.id} leaves before {child.name} is ready", refs=[e.id]))
        slots[owner.id].append(_Slot(e.depart, e.arrive, HOME, e.destination, e.id, f"escort {e.id}"))

    for ev in state.events:
        if ev.status != "confirmed" or ev.kind != "commitment" or ev.member_id not in slots:
            continue
        slots[ev.member_id].append(_Slot(ev.start, ev.end, ev.location, ev.location, ev.id, ev.title))

    for seg in state.supervision:
        if not state.has_member(seg.member_id):
            violations.append(Violation(code="UNKNOWN_MEMBER", detail=f"{seg.id} supervisor {seg.member_id} does not exist", refs=[seg.id]))
            continue
        sup = state.member(seg.member_id)
        if not sup.is_adult or seg.child_id not in sup.can_supervise:
            violations.append(Violation(code="NOT_PERMITTED", detail=f"{sup.name} is not approved to supervise {seg.child_id}", refs=[seg.id]))
        if seg.start >= seg.end:
            violations.append(Violation(code="EMPTY_SEGMENT", detail=f"{seg.id} is empty", refs=[seg.id]))
            continue
        home_slots[sup.id].append((seg.start, seg.end))

    # one person, one place at a time (supervision of several children at home can overlap)
    for member in state.members:
        member_slots = list(slots[member.id])
        for start, end in merge(home_slots[member.id]):
            member_slots.append(_Slot(start, end, HOME, HOME, f"home:{member.id}", "supervising at home"))
        if not member_slots:
            continue
        window = state.availability_window(member)
        if window is None:
            violations.append(
                Violation(code="UNAVAILABLE", detail=f"{member.name} is unavailable but has duties", refs=[s.ref for s in member_slots])
            )
            continue
        member_slots.sort(key=lambda s: (s.start, s.end))
        for s in member_slots:
            if s.start < window[0] or s.end > window[1]:
                violations.append(
                    Violation(
                        code="OUTSIDE_AVAILABILITY",
                        detail=f"{member.name} has '{s.label}' {fmt_range(s.start, s.end)} outside {fmt_range(*window)}",
                        refs=[s.ref],
                    )
                )
        first = member_slots[0]
        if window[0] + state.travel_minutes(HOME, first.start_loc) > first.start:
            violations.append(Violation(code="UNREACHABLE", detail=f"{member.name} cannot reach '{first.label}' in time", refs=[first.ref]))
        for a, b in zip(member_slots, member_slots[1:]):
            if a.end + state.travel_minutes(a.end_loc, b.start_loc) > b.start:
                violations.append(
                    Violation(
                        code="OVERLAP",
                        detail=f"{member.name}: '{a.label}' {fmt_range(a.start, a.end)} and '{b.label}' {fmt_range(b.start, b.end)} cannot both happen",
                        refs=[a.ref, b.ref],
                    )
                )

    checked_children = 0
    for child in state.children():
        if not child.needs_supervision:
            continue
        escort = state.escort_for_child(child.id)
        if escort is not None and escort.depart is None:
            continue  # reported as UNASSIGNED above
        # a child with no trip today (school closed) stays home until the day ends
        leaves = escort.depart if escort is not None else state.day_end
        checked_children += 1
        start = child.ready_at if child.ready_at is not None else state.day_start
        covered = [(s.start, s.end) for s in state.supervision if s.child_id == child.id]
        for gap in subtract((start, leaves), covered):
            violations.append(
                Violation(
                    code="SUPERVISION_GAP",
                    detail=f"{child.name} has no adult {fmt_range(*gap)}",
                    refs=[child.id] + ([escort.id] if escort is not None else []),
                )
            )

    return CheckReport(
        ok=not violations,
        violations=violations,
        unassigned=unassigned,
        checked_escorts=len(state.escorts),
        checked_children=checked_children,
    )
