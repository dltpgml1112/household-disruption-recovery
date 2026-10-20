"""Tool inputs/outputs. These are the MCP structured-output schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from hdr.domain.notice import StructuredNotice
from hdr.domain.planner import Decision, Objective, SegmentOut, AssignmentOut, Unresolved
from hdr.domain.propagation import Conflict


class MemberOut(BaseModel):
    id: str
    name: str
    role: str
    age: int | None = None
    available: bool
    available_window: str | None = None
    can_supervise: list[str]
    can_escort: list[str]
    needs_supervision: bool = False
    ready_at: str | None = None
    note: str | None = None


class EventOut(BaseModel):
    id: str
    member_id: str
    title: str
    start: str
    end: str
    location: str
    hard: bool
    kind: str
    status: str
    responsibility_id: str | None = None


class EscortOut(BaseModel):
    id: str
    child_id: str
    destination: str
    window: str
    target_arrival: str
    assigned_member_id: str | None
    depart: str | None
    arrive: str | None


class DisruptionRef(BaseModel):
    id: str
    kind: str
    status: str


class HouseholdStateOut(BaseModel):
    household_id: str
    name: str
    date: str
    timezone: str
    state_version: int
    members: list[MemberOut]
    calendar: list[EventOut]
    escorts: list[EscortOut]
    supervision: list[SegmentOut]
    travel_minutes: list[dict[str, Any]]
    rules: list[str]
    open_disruptions: list[DisruptionRef]


class NoticeOut(BaseModel):
    notice_id: str
    disruption_id: str | None
    structured: StructuredNotice
    applied: bool
    affected_responsibilities: list[str]
    conflicts: list[Conflict]
    state_version: int
    duplicate: bool = False
    message: str


class ActionOut(BaseModel):
    seq: int
    action_id: str
    type: str
    summary: str
    target: dict[str, Any]
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    status: str


class Card(BaseModel):
    """What a screen device shows for this result, in the shape the Alexa+ add-on design guide
    asks for: one title, one or two supporting fields, one clear action."""

    title: str
    fields: list[str] = Field(default_factory=list, max_length=2)
    action: str | None = None


class PlanOut(BaseModel):
    plan_id: str
    version: int
    disruption_id: str
    status: str
    base_state_version: int
    feasible: bool
    conflicts: list[Conflict]
    actions: list[ActionOut]
    assignments: list[AssignmentOut]
    supervision: list[SegmentOut]
    decisions: list[Decision]
    rationale: list[str]
    unresolved: list[Unresolved]
    options: list[str]
    objective: Objective
    combinations_evaluated: int
    requires_approval: bool
    superseded_plans: list[str] = Field(default_factory=list)
    # counts a voice reply can quote as they are, so a client never has to count list items
    changes_total: int = 0
    people_notified: list[str] = Field(default_factory=list)
    card: Card | None = None


class ApprovalOut(BaseModel):
    plan_id: str
    plan_version: int
    decision: Literal["approve", "reject"]
    status: str
    approval_token: str | None = None
    expires_at: str | None = None
    approved_actions: int = 0


class ActionResult(BaseModel):
    seq: int
    action_id: str
    type: str
    summary: str
    status: str
    error: str | None = None
    attempts: int


class ApplyOut(BaseModel):
    plan_id: str
    status: str
    results: list[ActionResult]
    applied: int
    failed: int
    skipped_already_applied: int
    state_version: int
    idempotent_replay: bool = False
    next_step: str


class CheckOut(BaseModel):
    seq: int
    type: str
    summary: str
    expected: Any
    actual: Any
    verified: bool


class VerifyOut(BaseModel):
    plan_id: str
    ready: bool
    checks: list[CheckOut]
    writes_verified: int
    writes_total: int
    affected_total: int
    affected_resolved: int
    unassigned: list[str]
    violations: list[str]
    failed_actions: list[str]
    summary: str
    card: Card | None = None


class AuditEventOut(BaseModel):
    ts: str
    actor: str
    tool: str
    result: str
    error_code: str | None = None
    plan_id: str | None = None
    input: dict[str, Any]
    output: dict[str, Any]
    duration_ms: int | None = None


class AuditOut(BaseModel):
    disruption_id: str
    events: list[AuditEventOut]
    summary: dict[str, Any]


class AvailabilityOut(BaseModel):
    household_id: str
    member_id: str
    available: bool
    state_version: int
    invalidated_plans: list[str]
