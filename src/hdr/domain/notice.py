"""Deterministic school-notice parser.

Recognises late starts ("two-hour delay"), closures and early dismissals. Late
starts and closures are planned for. Confidence is rule-based: an explicit new
start time that agrees with the stated delay scores highest; anything ambiguous
falls below the clarification threshold instead of being guessed.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from hdr.domain.models import HouseholdSnapshot
from hdr.domain.timeutil import fmt, hm

CLARIFY_BELOW = 0.8

_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "1": 1, "2": 2, "3": 3, "4": 4}
_DELAY = re.compile(r"\b(one|two|three|four|[1-4])[\s-]*(?:hour|hr)s?\s+(?:delay|late start|delayed start)", re.I)
_DELAY_MIN = re.compile(r"\b(\d{2,3})[\s-]*min(?:ute)?s?\s+(?:delay|late start)", re.I)
_CLOCK = r"(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)"
_DOORS = re.compile(r"doors?\s+(?:will\s+)?open(?:s)?\s+(?:at\s+)?" + _CLOCK, re.I)
_CLASSES = re.compile(r"(?:classes|school|instruction)\s+(?:will\s+)?(?:begin|begins|start|starts)\s+(?:at\s+)?" + _CLOCK, re.I)
_CLOSED = re.compile(r"\b(closed|closure|cancel(?:l)?ed|no school)\b", re.I)
_EARLY = re.compile(r"\bearly (?:dismissal|release)\b", re.I)


class SchoolInfo(BaseModel):
    id: str
    name: str
    doors_open: int
    classes_begin: int


class StructuredNotice(BaseModel):
    kind: Literal["delay", "closure", "early_dismissal", "unknown"]
    school_id: str
    delay_minutes: int | None = None
    doors_open: str | None = None
    classes_begin: str | None = None
    confidence: float
    needs_clarification: bool
    questions: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


def _clock(match: re.Match) -> int:
    hour, minute, ampm = int(match.group(1)), int(match.group(2) or 0), match.group(3).lower().replace(".", "")
    if hour == 12:
        hour = 0
    if ampm.startswith("p"):
        hour += 12
    return hour * 60 + minute


def parse_notice(raw: str, school: SchoolInfo) -> StructuredNotice:
    text = " ".join(raw.split())
    evidence: list[str] = []
    closed, early = _CLOSED.search(text), _EARLY.search(text)
    delay_m = _DELAY.search(text)
    delay_min_m = _DELAY_MIN.search(text)
    doors_m, classes_m = _DOORS.search(text), _CLASSES.search(text)

    if closed and not (delay_m or delay_min_m):
        return StructuredNotice(
            kind="closure", school_id=school.id, confidence=0.9, needs_clarification=False,
            evidence=[closed.group(0)],
        )
    if early and not (delay_m or delay_min_m):
        return StructuredNotice(
            kind="early_dismissal", school_id=school.id, confidence=0.9, needs_clarification=False,
            evidence=[early.group(0)],
            questions=["Early dismissals are not planned for yet in this version."],
        )

    delay: int | None = None
    if delay_m:
        delay = _WORDS[delay_m.group(1).lower()] * 60
        evidence.append(delay_m.group(0))
    elif delay_min_m:
        delay = int(delay_min_m.group(1))
        evidence.append(delay_min_m.group(0))

    doors = _clock(doors_m) if doors_m else None
    classes = _clock(classes_m) if classes_m else None
    if doors_m:
        evidence.append(doors_m.group(0))
    if classes_m:
        evidence.append(classes_m.group(0))

    questions: list[str] = []
    confidence = 0.0
    if delay is None and classes is None:
        return StructuredNotice(
            kind="unknown", school_id=school.id, confidence=0.2, needs_clarification=True,
            questions=["Is school starting late, closed, or dismissing early today?"], evidence=evidence,
        )
    if delay is not None and classes is not None:
        if classes - school.classes_begin == delay:
            confidence = 0.97
        else:
            confidence = 0.5
            questions.append(
                f"The notice says a {delay // 60}-hour delay but classes at {fmt(classes)}; "
                f"the usual start is {fmt(school.classes_begin)}. Which is right?"
            )
    elif delay is not None:
        classes = school.classes_begin + delay
        confidence = 0.85
    else:
        delay = classes - school.classes_begin
        confidence = 0.8 if delay > 0 else 0.4
        if delay <= 0:
            questions.append(f"Classes at {fmt(classes)} is not later than usual. Is this a delay?")
    if doors is None and classes is not None:
        doors = school.doors_open + (classes - school.classes_begin)
    if doors is not None and classes is not None and doors > classes:
        confidence = min(confidence, 0.5)
        questions.append("Doors are listed as opening after classes begin. Which time is right?")

    return StructuredNotice(
        kind="delay",
        school_id=school.id,
        delay_minutes=delay,
        doors_open=fmt(doors),
        classes_begin=fmt(classes),
        confidence=round(confidence, 2),
        needs_clarification=confidence < CLARIFY_BELOW,
        questions=questions,
        evidence=evidence,
    )


def apply_delay(snapshot: HouseholdSnapshot, notice: StructuredNotice) -> tuple[HouseholdSnapshot, list[str]]:
    """New arrival windows for every escort to the delayed school. Returns the
    updated copy and the affected escort ids. Assignments are left untouched."""
    if notice.kind != "delay" or notice.doors_open is None or notice.classes_begin is None:
        raise ValueError("only delays with known times can be applied")
    updated = snapshot.model_copy(deep=True)
    affected: list[str] = []
    for escort in updated.escorts:
        if escort.destination == notice.school_id:
            escort.window_start = hm(notice.doors_open)
            escort.window_end = hm(notice.classes_begin)
            escort.target_arrival = hm(notice.doors_open)
            affected.append(escort.id)
    return updated, affected


def apply_closure(snapshot: HouseholdSnapshot, notice: StructuredNotice) -> tuple[HouseholdSnapshot, list[str]]:
    """Drop every trip to the closed school. A child with no trip is home all day,
    so the planner and the checker then need an adult with them until the day ends."""
    if notice.kind != "closure":
        raise ValueError("not a closure")
    updated = snapshot.model_copy(deep=True)
    cancelled = [e.id for e in updated.escorts if e.destination == notice.school_id]
    updated.escorts = [e for e in updated.escorts if e.id not in cancelled]
    return updated, cancelled
