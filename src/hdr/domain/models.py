"""Household state as plain data. Times are minutes since midnight on `date`."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from hdr.domain.timeutil import fmt

HOME = "home"

Role = Literal["parent", "caregiver", "child"]


class Member(BaseModel):
    id: str
    name: str
    role: Role
    age: int | None = None
    can_supervise: list[str] = Field(default_factory=list)
    can_escort: list[str] = Field(default_factory=list)
    available: bool = True
    available_from: int | None = None
    available_until: int | None = None
    note: str | None = None
    # children only
    needs_supervision: bool = False
    ready_at: int | None = None

    @property
    def is_adult(self) -> bool:
        return self.role != "child"


class Location(BaseModel):
    id: str
    name: str


class TravelEdge(BaseModel):
    a: str
    b: str
    minutes: int


class CalendarEvent(BaseModel):
    id: str
    member_id: str
    title: str
    start: int
    end: int
    location: str
    hard: bool = True
    kind: Literal["commitment", "duty"] = "commitment"
    status: Literal["confirmed", "cancelled"] = "confirmed"
    responsibility_id: str | None = None
    reminder_min: int | None = None


class Escort(BaseModel):
    """Take a child from home to a destination, arriving inside the window."""

    id: str
    child_id: str
    destination: str
    window_start: int
    window_end: int
    target_arrival: int
    max_late: int = 0
    required: bool = True
    assigned_member_id: str | None = None
    depart: int | None = None
    arrive: int | None = None


class SupervisionSegment(BaseModel):
    id: str
    child_id: str
    member_id: str
    start: int
    end: int


class HouseholdSnapshot(BaseModel):
    household_id: str
    name: str = ""
    date: str
    timezone: str
    state_version: int = 0
    day_start: int
    day_end: int
    members: list[Member]
    locations: list[Location]
    travel: list[TravelEdge]
    events: list[CalendarEvent] = Field(default_factory=list)
    escorts: list[Escort] = Field(default_factory=list)
    supervision: list[SupervisionSegment] = Field(default_factory=list)

    def member(self, member_id: str) -> Member:
        for m in self.members:
            if m.id == member_id:
                return m
        raise KeyError(member_id)

    def has_member(self, member_id: str) -> bool:
        return any(m.id == member_id for m in self.members)

    def adults(self) -> list[Member]:
        return [m for m in self.members if m.is_adult]

    def children(self) -> list[Member]:
        return [m for m in self.members if not m.is_adult]

    def escort_for_child(self, child_id: str) -> Escort | None:
        for e in self.escorts:
            if e.child_id == child_id:
                return e
        return None

    def location_name(self, location_id: str) -> str:
        for loc in self.locations:
            if loc.id == location_id:
                return loc.name
        return location_id

    def travel_minutes(self, a: str, b: str) -> int:
        if a == b:
            return 0
        for edge in self.travel:
            if {edge.a, edge.b} == {a, b}:
                return edge.minutes
        raise KeyError(f"no travel time between {a} and {b}")

    def availability_window(self, member: Member) -> tuple[int, int] | None:
        if not member.available:
            return None
        start = member.available_from if member.available_from is not None else self.day_start
        end = member.available_until if member.available_until is not None else self.day_end
        return (start, end)


def describe_escort(snapshot: HouseholdSnapshot, escort: Escort) -> str:
    child = snapshot.member(escort.child_id).name
    return f"{child} to {snapshot.location_name(escort.destination)}"


def time_str(minutes: int | None) -> str | None:
    return fmt(minutes)
