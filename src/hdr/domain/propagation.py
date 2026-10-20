"""Disruption propagation: what breaks if the family keeps its current plan."""

from __future__ import annotations

from pydantic import BaseModel, Field

from hdr.domain.models import HouseholdSnapshot
from hdr.domain.planner import EscortChoice, arrival_candidates, baseline_engaged, evaluate
from hdr.domain.timeutil import fmt, fmt_range


class Conflict(BaseModel):
    code: str  # OWNER_UNAVAILABLE | SUPERVISION_GAP | SCHEDULE_CONFLICT
    detail: str
    responsibility_id: str | None = None
    child_id: str | None = None
    member_id: str | None = None
    intervals: list[str] = Field(default_factory=list)


class Retime(BaseModel):
    responsibility_id: str
    member_id: str
    depart: str
    arrive: str


class Propagation(BaseModel):
    affected_responsibilities: list[str]
    conflicts: list[Conflict]
    retimed: list[Retime]


def _in_window(snapshot: HouseholdSnapshot, escort_id: str) -> bool:
    e = next(x for x in snapshot.escorts if x.id == escort_id)
    return e.arrive is not None and e.window_start <= e.arrive <= e.window_end + e.max_late


def propagate(snapshot: HouseholdSnapshot, affected: list[str]) -> Propagation:
    engaged = frozenset(baseline_engaged(snapshot))
    current = {e.id: EscortChoice(e.id, e.assigned_member_id, e.depart, e.arrive) for e in snapshot.escorts}
    conflicts: list[Conflict] = []
    retimed: list[Retime] = []

    for e in snapshot.escorts:
        if e.id not in affected and _in_window(snapshot, e.id):
            continue
        owner = e.assigned_member_id
        if owner is None:
            continue
        found = None
        first_problem = None
        for depart, arrive in arrival_candidates(snapshot, e):
            trial = dict(current)
            trial[e.id] = EscortChoice(e.id, owner, depart, arrive)
            ev = evaluate(snapshot, tuple(trial.values()), frozenset(), engaged)
            problems = ev.itineraries[owner].problems
            if not problems:
                found = (depart, arrive)
                break
            if first_problem is None:
                first_problem = problems[0].detail
        if found:
            current[e.id] = EscortChoice(e.id, owner, *found)
            retimed.append(Retime(responsibility_id=e.id, member_id=owner, depart=fmt(found[0]), arrive=fmt(found[1])))
        else:
            current[e.id] = EscortChoice(e.id, None, None, None)
            conflicts.append(
                Conflict(
                    code="OWNER_UNAVAILABLE",
                    detail=first_problem or f"{snapshot.member(owner).name} cannot take this trip any more",
                    responsibility_id=e.id,
                    child_id=e.child_id,
                    member_id=owner,
                )
            )

    ev = evaluate(snapshot, tuple(current.values()), frozenset(), engaged)
    gaps: dict[str, list[tuple[int, int]]] = {}
    for v in ev.violations:
        if v["code"] == "SUPERVISION_GAP":
            gaps.setdefault(v["child_id"], []).append(v["interval"])
        elif not any(c.member_id == v.get("member_id") for c in conflicts):
            conflicts.append(Conflict(code=v["code"], detail=v["detail"], member_id=v.get("member_id")))
    for child_id, intervals in gaps.items():
        child = snapshot.member(child_id)
        spans = [fmt_range(*i) for i in intervals]
        conflicts.append(
            Conflict(
                code="SUPERVISION_GAP",
                detail=f"{child.name} would be home without an adult {' and '.join(spans)}",
                child_id=child_id,
                intervals=spans,
            )
        )

    affected_all = list(dict.fromkeys(affected + [c.responsibility_id for c in conflicts if c.responsibility_id]))
    return Propagation(affected_responsibilities=affected_all, conflicts=conflicts, retimed=retimed)
