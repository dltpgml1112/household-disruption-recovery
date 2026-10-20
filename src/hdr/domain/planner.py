"""Deterministic recovery planner.

Exhaustive search over escort assignments, arrival times, releasing soft calendar
events and calling in caregivers. Plans are ranked lexicographically:

  1. required responsibilities without an owner
  2. hard violations (schedule conflicts, availability, supervision gaps)
  3. changed commitments (reassignments, retimes, released events, caregivers called in)
  4. minutes late
  5. people who need to be notified
  6. distance from target arrival times
  7. stable tie-break on member ids and times

The household sizes this targets (a handful of adults and children) keep the space
in the hundreds to low thousands of combinations, so exhaustive search is cheap and
every choice can be explained by comparing it with the alternatives.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from hdr.domain.models import HOME, Escort, HouseholdSnapshot, Member
from hdr.domain.schedule import Block, Coverage, Itinerary, build_itinerary, cover_child, escort_block, event_block
from hdr.domain.timeutil import Interval, fmt, fmt_range

MAX_COMBINATIONS = 100_000
STEP = 5


class PlannerLimitExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class EscortChoice:
    escort_id: str
    member_id: str | None
    depart: int | None
    arrive: int | None


@dataclass
class Evaluation:
    choices: tuple[EscortChoice, ...]
    released: frozenset[str]
    engaged: frozenset[str]
    itineraries: dict[str, Itinerary]
    unassigned: list[str]
    violations: list[dict]
    coverage: dict[str, Coverage]
    changed: int
    lateness: int
    notifications: int
    deviation: int
    changed_members: list[str] = field(default_factory=list)

    @property
    def feasible(self) -> bool:
        return not self.unassigned and not self.violations

    @property
    def key(self) -> tuple:
        tiebreak = tuple((c.escort_id, c.member_id or "", c.arrive or 0) for c in self.choices)
        return (
            len(self.unassigned),
            len(self.violations),
            self.changed,
            self.lateness,
            self.notifications,
            self.deviation,
            tuple(sorted(self.released)),
            tuple(sorted(self.engaged)),
            tiebreak,
        )


# ---- output schema -------------------------------------------------------


class AssignmentOut(BaseModel):
    escort_id: str
    child_id: str
    member_id: str | None
    depart: str | None
    arrive: str | None
    late_min: int = 0


class SegmentOut(BaseModel):
    child_id: str
    member_id: str
    start: str
    end: str


class Alternative(BaseModel):
    member_id: str | None
    outcome: str  # "infeasible" | "worse"
    detail: str


class Decision(BaseModel):
    escort_id: str
    chosen_member_id: str | None
    reason: str
    alternatives: list[Alternative] = Field(default_factory=list)


class Unresolved(BaseModel):
    code: str
    detail: str
    child_id: str | None = None
    responsibility_id: str | None = None
    interval: str | None = None
    blockers: list[str] = Field(default_factory=list)


class Objective(BaseModel):
    unassigned: int
    violations: int
    changed_commitments: int
    lateness_min: int
    notifications: int
    target_deviation_min: int


class PlanResult(BaseModel):
    feasible: bool
    assignments: list[AssignmentOut]
    supervision: list[SegmentOut]
    released_events: list[str]
    engaged_caregivers: list[str]
    changed_members: list[str]
    objective: Objective
    decisions: list[Decision]
    unresolved: list[Unresolved]
    options: list[str]
    combinations_evaluated: int


# ---- search --------------------------------------------------------------


def eligible_escorts(snapshot: HouseholdSnapshot, escort: Escort) -> list[Member]:
    return [
        m for m in snapshot.adults() if escort.child_id in m.can_escort and m.available
    ]


def arrival_candidates(snapshot: HouseholdSnapshot, escort: Escort) -> list[tuple[int, int]]:
    """(depart, arrive) pairs on a 5-minute grid, closest to target first."""
    child = snapshot.member(escort.child_id)
    travel = snapshot.travel_minutes(HOME, escort.destination)
    earliest_depart = max(snapshot.day_start, child.ready_at if child.ready_at is not None else snapshot.day_start)
    arrivals = set(range(escort.window_start, escort.window_end + escort.max_late + 1, STEP))
    arrivals.add(escort.target_arrival)
    pairs = [(a - travel, a) for a in arrivals if a - travel >= earliest_depart]
    return sorted(pairs, key=lambda p: (abs(p[1] - escort.target_arrival), p[1]))


def _baseline_duties(snapshot: HouseholdSnapshot) -> dict[str, set]:
    duties: dict[str, set] = {m.id: set() for m in snapshot.adults()}
    for e in snapshot.escorts:
        if e.assigned_member_id in duties:
            duties[e.assigned_member_id].add(("escort", e.id, e.depart, e.arrive))
    for s in snapshot.supervision:
        if s.member_id in duties:
            duties[s.member_id].add(("supervise", s.child_id, s.start, s.end))
    return duties


def baseline_engaged(snapshot: HouseholdSnapshot) -> set[str]:
    caregivers = {m.id for m in snapshot.adults() if m.role == "caregiver"}
    engaged = {e.assigned_member_id for e in snapshot.escorts if e.assigned_member_id in caregivers}
    engaged |= {s.member_id for s in snapshot.supervision if s.member_id in caregivers}
    return engaged


def _arrival_in_window(escort: Escort) -> bool:
    return escort.arrive is not None and escort.window_start <= escort.arrive <= escort.window_end + escort.max_late


def evaluate(
    snapshot: HouseholdSnapshot,
    choices: tuple[EscortChoice, ...],
    released: frozenset[str],
    engaged: frozenset[str],
) -> Evaluation:
    escorts = {e.id: e for e in snapshot.escorts}
    adults = snapshot.adults()
    order = [m.id for m in adults]

    blocks: dict[str, list[Block]] = {m.id: [] for m in adults}
    for ev in snapshot.events:
        if ev.status == "confirmed" and ev.kind == "commitment" and ev.id not in released and ev.member_id in blocks:
            blocks[ev.member_id].append(event_block(ev))
    unassigned: list[str] = []
    for c in choices:
        if c.member_id is None:
            if escorts[c.escort_id].required:
                unassigned.append(c.escort_id)
            continue
        blocks[c.member_id].append(escort_block(snapshot, c.escort_id, c.depart, c.arrive))

    itineraries = {m.id: build_itinerary(snapshot, m, blocks[m.id]) for m in adults}
    violations: list[dict] = []
    for itin in itineraries.values():
        for p in itin.problems:
            violations.append({"code": p.code, "member_id": p.member_id, "detail": p.detail, "refs": list(p.refs)})

    by_child = {escorts[c.escort_id].child_id: c for c in choices}
    coverage: dict[str, Coverage] = {}
    for child in snapshot.children():
        if not child.needs_supervision:
            continue
        choice = by_child.get(child.id)
        escort = snapshot.escort_for_child(child.id)
        if choice is not None and choice.depart is not None:
            depart, escort_member = choice.depart, choice.member_id
        elif escort is not None:
            depart = escort.target_arrival - snapshot.travel_minutes(HOME, escort.destination)
            escort_member = None
        else:
            depart, escort_member = snapshot.day_end, None
        free = {
            m.id: itineraries[m.id].home_free
            for m in adults
            if child.id in m.can_supervise and (m.role != "caregiver" or m.id in engaged)
        }
        cov = cover_child(snapshot, child, depart, escort_member, free, order)
        coverage[child.id] = cov
        for gap in cov.gaps:
            violations.append(
                {
                    "code": "SUPERVISION_GAP",
                    "member_id": None,
                    "child_id": child.id,
                    "detail": f"{child.name} would be home without an adult {fmt_range(*gap)}",
                    "refs": [child.id],
                    "interval": gap,
                }
            )

    # changed commitments relative to the current state
    changed = 0
    lateness = 0
    deviation = 0
    for c in choices:
        e = escorts[c.escort_id]
        if c.member_id != e.assigned_member_id:
            changed += 1
        elif _arrival_in_window(e) and c.depart != e.depart:
            changed += 1
        if c.arrive is not None:
            lateness += max(0, c.arrive - e.window_end)
            deviation += abs(c.arrive - e.target_arrival)
    changed += len(released)
    changed += len(engaged - baseline_engaged(snapshot))

    duties = _baseline_duties(snapshot)
    new_duties: dict[str, set] = {m.id: set() for m in adults}
    for c in choices:
        if c.member_id is not None:
            new_duties[c.member_id].add(("escort", c.escort_id, c.depart, c.arrive))
    for cov in coverage.values():
        for member_id, start, end in cov.segments:
            new_duties[member_id].add(("supervise", cov.child_id, start, end))
    released_owner = {ev.member_id for ev in snapshot.events if ev.id in released}
    changed_members = [m for m in order if new_duties[m] != duties.get(m, set()) or m in released_owner]

    return Evaluation(
        choices=choices,
        released=released,
        engaged=engaged,
        itineraries=itineraries,
        unassigned=unassigned,
        violations=violations,
        coverage=coverage,
        changed=changed,
        lateness=lateness,
        notifications=len(changed_members),
        deviation=deviation,
        changed_members=changed_members,
    )


def _search_space(snapshot: HouseholdSnapshot):
    per_escort: list[list[EscortChoice]] = []
    for e in snapshot.escorts:
        options: list[EscortChoice] = []
        times = arrival_candidates(snapshot, e)
        for m in eligible_escorts(snapshot, e):
            options += [EscortChoice(e.id, m.id, d, a) for d, a in times]
        if not e.required or not options:
            options.append(EscortChoice(e.id, None, None, None))
        per_escort.append(options)
    soft = [
        ev.id
        for ev in snapshot.events
        if ev.kind == "commitment" and not ev.hard and ev.status == "confirmed"
    ]
    caregivers = [m.id for m in snapshot.adults() if m.role == "caregiver" and m.available]
    return per_escort, soft, caregivers


def _powerset(items: list[str]):
    for r in range(len(items) + 1):
        yield from itertools.combinations(items, r)


def search(snapshot: HouseholdSnapshot, max_combinations: int = MAX_COMBINATIONS) -> tuple[Evaluation, list[Evaluation]]:
    per_escort, soft, caregivers = _search_space(snapshot)
    total = 1
    for opts in per_escort:
        total *= len(opts)
    total *= 2 ** len(soft) * 2 ** len(caregivers)
    if total > max_combinations:
        raise PlannerLimitExceeded(f"{total} combinations exceeds limit {max_combinations}")

    evaluations: list[Evaluation] = []
    for choices in itertools.product(*per_escort):
        assigned_caregivers = {c.member_id for c in choices if c.member_id in caregivers}
        for released in _powerset(soft):
            for engaged in _powerset(caregivers):
                # a caregiver who drives is necessarily called in
                if not assigned_caregivers <= set(engaged):
                    continue
                evaluations.append(evaluate(snapshot, tuple(choices), frozenset(released), frozenset(engaged)))
    best = min(evaluations, key=lambda ev: ev.key)
    return best, evaluations


# ---- explanation ---------------------------------------------------------

_COMPONENTS = [
    ("unassigned", "leaves a required trip without a driver"),
    ("violations", "breaks a hard constraint"),
    ("changed", "changes more commitments"),
    ("lateness", "arrives later"),
    ("notifications", "needs more people to change plans"),
    ("deviation", "is further from the preferred arrival time"),
]


def _first_violation_for(ev: Evaluation, member_id: str | None, escort_id: str) -> str:
    for v in ev.violations:
        if escort_id in v.get("refs", []) or (member_id and v.get("member_id") == member_id):
            return v["detail"]
    return ev.violations[0]["detail"] if ev.violations else "no valid owner"


def _compare(best: Evaluation, other: Evaluation) -> str:
    values = {
        "unassigned": (len(best.unassigned), len(other.unassigned)),
        "violations": (len(best.violations), len(other.violations)),
        "changed": (best.changed, other.changed),
        "lateness": (best.lateness, other.lateness),
        "notifications": (best.notifications, other.notifications),
        "deviation": (best.deviation, other.deviation),
    }
    for name, text in _COMPONENTS:
        a, b = values[name]
        if b != a:
            return f"{text} ({b} vs {a})"
    return "ties on every objective; kept the earlier option"


def explain(snapshot: HouseholdSnapshot, best: Evaluation, evaluations: list[Evaluation]) -> list[Decision]:
    decisions: list[Decision] = []
    for idx, escort in enumerate(snapshot.escorts):
        chosen = best.choices[idx]
        best_by_member: dict[str | None, Evaluation] = {}
        for ev in evaluations:
            m = ev.choices[idx].member_id
            if m not in best_by_member or ev.key < best_by_member[m].key:
                best_by_member[m] = ev
        alternatives = []
        for m in [x.id for x in eligible_escorts(snapshot, escort)] + ([None] if None in best_by_member else []):
            if m == chosen.member_id or m not in best_by_member:
                continue
            alt = best_by_member[m]
            if alt.feasible or not best.feasible:
                outcome = "worse" if alt.feasible else "infeasible"
                detail = _compare(best, alt) if alt.feasible else _first_violation_for(alt, m, escort.id)
            else:
                outcome, detail = "infeasible", _first_violation_for(alt, m, escort.id)
            alternatives.append(Alternative(member_id=m, outcome=outcome, detail=detail))
        if chosen.member_id is None:
            reason = "no adult can take this trip"
        elif chosen.member_id == escort.assigned_member_id and chosen.depart == escort.depart:
            reason = "unchanged"
        else:
            name = snapshot.member(chosen.member_id).name
            reason = f"{name} leaves {fmt(chosen.depart)}, arrives {fmt(chosen.arrive)}"
        decisions.append(Decision(escort_id=escort.id, chosen_member_id=chosen.member_id, reason=reason, alternatives=alternatives))
    return decisions


def _blockers(snapshot: HouseholdSnapshot, best: Evaluation, child_id: str, gap: Interval) -> list[str]:
    out: list[str] = []
    for m in snapshot.adults():
        if child_id not in m.can_supervise:
            continue
        window = snapshot.availability_window(m)
        if window is None:
            out.append(f"{m.name} is unavailable today")
            continue
        if window[1] <= gap[0] or window[0] >= gap[1]:
            out.append(f"{m.name} is only available {fmt_range(*window)}")
            continue
        for block in best.itineraries[m.id].blocks:
            if block.start < gap[1] and block.end > gap[0] - 30:
                kind = "hard" if block.hard else "soft"
                out.append(f"{m.name}: '{block.label}' ({kind}) {fmt_range(block.start, block.end)}")
    return out


def unresolved_from(snapshot: HouseholdSnapshot, best: Evaluation) -> tuple[list[Unresolved], list[str]]:
    items: list[Unresolved] = []
    options: list[str] = []
    for escort_id in best.unassigned:
        escort = next(e for e in snapshot.escorts if e.id == escort_id)
        child = snapshot.member(escort.child_id).name
        items.append(
            Unresolved(
                code="NO_ELIGIBLE_ADULT",
                detail=f"No approved adult can take {child} to {snapshot.location_name(escort.destination)} "
                f"between {fmt(escort.window_start)} and {fmt(escort.window_end)}",
                child_id=escort.child_id,
                responsibility_id=escort_id,
            )
        )
    for v in best.violations:
        if v["code"] == "SUPERVISION_GAP":
            gap = v["interval"]
            child = snapshot.member(v["child_id"])
            blockers = _blockers(snapshot, best, child.id, gap)
            items.append(
                Unresolved(
                    code="SUPERVISION_GAP",
                    detail=f"No approved adult can be with {child.name} {fmt_range(*gap)}",
                    child_id=child.id,
                    interval=fmt_range(*gap),
                    blockers=blockers,
                )
            )
            options.append(f"Find another approved adult for {child.name} {fmt_range(*gap)}")
            for b in blockers:
                if "(hard)" in b:
                    options.append(f"Ask whether this can move: {b}")
        else:
            items.append(Unresolved(code=v["code"], detail=v["detail"], blockers=[v.get("member_id") or ""]))
    deduped: list[str] = []
    for o in options:
        if o not in deduped:
            deduped.append(o)
    return items, deduped


def plan(snapshot: HouseholdSnapshot, max_combinations: int = MAX_COMBINATIONS) -> PlanResult:
    best, evaluations = search(snapshot, max_combinations)
    escorts = {e.id: e for e in snapshot.escorts}
    assignments = [
        AssignmentOut(
            escort_id=c.escort_id,
            child_id=escorts[c.escort_id].child_id,
            member_id=c.member_id,
            depart=fmt(c.depart),
            arrive=fmt(c.arrive),
            late_min=max(0, (c.arrive or 0) - escorts[c.escort_id].window_end) if c.arrive is not None else 0,
        )
        for c in best.choices
    ]
    supervision = [
        SegmentOut(child_id=cov.child_id, member_id=m, start=fmt(s), end=fmt(e))
        for cov in best.coverage.values()
        for m, s, e in cov.segments
    ]
    unresolved, options = ([], []) if best.feasible else unresolved_from(snapshot, best)
    return PlanResult(
        feasible=best.feasible,
        assignments=assignments,
        supervision=supervision,
        released_events=sorted(best.released),
        engaged_caregivers=sorted(best.engaged),
        changed_members=best.changed_members,
        objective=Objective(
            unassigned=len(best.unassigned),
            violations=len(best.violations),
            changed_commitments=best.changed,
            lateness_min=best.lateness,
            notifications=best.notifications,
            target_deviation_min=best.deviation,
        ),
        decisions=explain(snapshot, best, evaluations),
        unresolved=unresolved,
        options=options,
        combinations_evaluated=len(evaluations),
    )
