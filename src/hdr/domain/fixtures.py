"""Load synthetic household fixtures (HH:MM strings) into a HouseholdSnapshot."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from hdr.domain.models import (
    CalendarEvent,
    Escort,
    HouseholdSnapshot,
    Location,
    Member,
    SupervisionSegment,
    TravelEdge,
)
from hdr.domain.timeutil import hm

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "households"
DEMO_FIXTURE = FIXTURE_DIR / "school_delay_demo.json"

_TIME_FIELDS = {
    "available_from", "available_until", "ready_at", "start", "end", "window_start", "window_end",
    "target_arrival", "depart", "arrive", "day_start", "day_end",
}


def _times(obj: dict[str, Any]) -> dict[str, Any]:
    return {k: (hm(v) if k in _TIME_FIELDS else v) for k, v in obj.items()}


def load_fixture(path: Path | str = DEMO_FIXTURE, variant: str | None = None) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if variant:
        patch = data.get("variants", {}).get(variant)
        if patch is None:
            raise KeyError(f"unknown variant {variant!r}")
        data = apply_variant(data, patch)
    return data


def apply_variant(data: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(data)
    for member_id, fields in patch.get("members", {}).items():
        for m in data["members"]:
            if m["id"] == member_id:
                m.update(fields)
    for event_id, fields in patch.get("commitments", {}).items():
        for ev in data["commitments"]:
            if ev["id"] == event_id:
                ev.update(fields)
    return data


def snapshot_from_fixture(data: dict[str, Any]) -> HouseholdSnapshot:
    hh = data["household"]
    events = [
        CalendarEvent(kind="commitment", **_times(ev)) for ev in data.get("commitments", [])
    ]
    escorts = [Escort(**_times(e)) for e in data.get("escorts", [])]
    segments = [
        SupervisionSegment(id=f"sup-{s['child_id']}-{i}", **_times(s))
        for i, s in enumerate(data.get("supervision", []), start=1)
    ]
    return HouseholdSnapshot(
        household_id=hh["id"],
        name=hh.get("name", ""),
        date=hh["date"],
        timezone=hh["timezone"],
        day_start=hm(hh["day_start"]),
        day_end=hm(hh["day_end"]),
        members=[Member(**_times(m)) for m in data["members"]],
        locations=[Location(**loc) for loc in data["locations"]],
        travel=[TravelEdge(a=a, b=b, minutes=n) for a, b, n in data["travel"]],
        events=events,
        escorts=escorts,
        supervision=segments,
    )
