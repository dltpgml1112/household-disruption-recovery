"""Itineraries and supervision coverage, as seen by the planner.

A member's day is a sorted list of blocks (calendar commitments and escort trips).
Between blocks the member goes home whenever the gap allows it; time spent at home
and not travelling is when they can supervise children. Children wait at home until
their own escort trip leaves (ride-alongs are not modelled).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hdr.domain.models import HOME, CalendarEvent, HouseholdSnapshot, Member
from hdr.domain.timeutil import Interval, fmt, fmt_range, merge, subtract


@dataclass(frozen=True)
class Block:
    start: int
    end: int
    start_loc: str
    end_loc: str
    kind: str  # "event" | "escort"
    ref: str
    label: str
    hard: bool = True


@dataclass(frozen=True)
class Problem:
    code: str
    member_id: str
    detail: str
    refs: tuple[str, ...] = ()


@dataclass
class Itinerary:
    member_id: str
    blocks: list[Block]
    problems: list[Problem] = field(default_factory=list)
    home_free: list[Interval] = field(default_factory=list)

    @property
    def feasible(self) -> bool:
        return not self.problems


def event_block(event: CalendarEvent) -> Block:
    return Block(event.start, event.end, event.location, event.location, "event", event.id, event.title, event.hard)


def escort_block(snapshot: HouseholdSnapshot, escort_id: str, depart: int, arrive: int) -> Block:
    escort = next(e for e in snapshot.escorts if e.id == escort_id)
    child = snapshot.member(escort.child_id).name
    label = f"drive {child} to {snapshot.location_name(escort.destination)}"
    return Block(depart, arrive, HOME, escort.destination, "escort", escort_id, label)


def build_itinerary(snapshot: HouseholdSnapshot, member: Member, blocks: list[Block]) -> Itinerary:
    blocks = sorted(blocks, key=lambda b: (b.start, b.end, b.ref))
    itin = Itinerary(member.id, blocks)
    window = snapshot.availability_window(member)
    if window is None:
        if blocks:
            itin.problems.append(
                Problem("MEMBER_UNAVAILABLE", member.id, f"{member.name} is unavailable today", tuple(b.ref for b in blocks))
            )
        return itin

    win_start, win_end = window
    cursor_time, cursor_loc = win_start, HOME
    prev: Block | None = None
    for block in blocks:
        needed = snapshot.travel_minutes(cursor_loc, block.start_loc)
        if block.start < win_start or block.end > win_end:
            itin.problems.append(
                Problem(
                    "OUTSIDE_AVAILABILITY",
                    member.id,
                    f"{member.name} is available {fmt_range(win_start, win_end)}, '{block.label}' is {fmt_range(block.start, block.end)}",
                    (block.ref,),
                )
            )
        elif cursor_time + needed > block.start:
            if prev is not None and prev.end > block.start:
                kind = "hard" if prev.hard else "soft"
                detail = (
                    f"{member.name} cannot {block.label} at {fmt(block.start)}: "
                    f"overlaps '{prev.label}' ({kind}) {fmt_range(prev.start, prev.end)}"
                )
            else:
                detail = (
                    f"{member.name} cannot {block.label} at {fmt(block.start)}: "
                    f"free at {fmt(cursor_time)} and needs {needed} min to get there"
                )
            refs = (block.ref,) + ((prev.ref,) if prev is not None else ())
            itin.problems.append(Problem("SCHEDULE_CONFLICT", member.id, detail, refs))
        # free time at home before this block
        to_home = snapshot.travel_minutes(cursor_loc, HOME)
        from_home = snapshot.travel_minutes(HOME, block.start_loc)
        home_start, home_end = cursor_time + to_home, block.start - from_home
        if home_start < home_end:
            itin.home_free.append((home_start, home_end))
        cursor_time, cursor_loc = max(cursor_time, block.end), block.end_loc
        prev = block if prev is None or block.end >= prev.end else prev

    home_start = cursor_time + snapshot.travel_minutes(cursor_loc, HOME)
    if home_start < win_end:
        itin.home_free.append((home_start, win_end))
    itin.home_free = merge(itin.home_free)
    return itin


@dataclass
class Coverage:
    child_id: str
    window: Interval
    segments: list[tuple[str, int, int]]
    gaps: list[Interval]


def supervision_window(snapshot: HouseholdSnapshot, child: Member, depart: int | None) -> Interval:
    start = child.ready_at if child.ready_at is not None else snapshot.day_start
    end = depart if depart is not None else start
    return (start, max(start, end))


def cover_child(
    snapshot: HouseholdSnapshot,
    child: Member,
    depart: int | None,
    escort_member: str | None,
    free_by_member: dict[str, list[Interval]],
    order: list[str],
) -> Coverage:
    """Pick supervisors for a child's time at home. Deterministic greedy:
    the adult who will drive the child takes the last stretch, otherwise the
    adult who can stay longest, then member order."""
    window = supervision_window(snapshot, child, depart)
    segments: list[tuple[str, int, int]] = []
    t, end = window
    while t < end:
        options: list[tuple[tuple, str, int]] = []
        for member_id, intervals in free_by_member.items():
            for start, stop in intervals:
                if start <= t < stop:
                    reach = min(stop, end)
                    continuity = 0 if (member_id == escort_member and reach >= end) else 1
                    options.append(((continuity, -reach, order.index(member_id)), member_id, reach))
        if not options:
            upcoming = [s for ivs in free_by_member.values() for s, _ in ivs if s > t]
            t = min([s for s in upcoming if s < end] or [end])
            continue
        _, member_id, reach = min(options)
        if segments and segments[-1][0] == member_id and segments[-1][2] == t:
            segments[-1] = (member_id, segments[-1][1], reach)
        else:
            segments.append((member_id, t, reach))
        t = reach
    covered = [(s, e) for _, s, e in segments]
    return Coverage(child.id, window, segments, subtract(window, covered))
