"""Read and write household state. Reads always rebuild a fresh snapshot from rows."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from hdr.domain.actions import derive_duty_events
from hdr.domain.fixtures import snapshot_from_fixture
from hdr.domain.models import (
    CalendarEvent,
    Escort,
    HouseholdSnapshot,
    Location,
    Member,
    SupervisionSegment,
    TravelEdge,
)
from hdr.domain.notice import SchoolInfo
from hdr.domain.timeutil import hm
from hdr.store.db import transaction


class NotFound(LookupError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")




def reset_household(conn: sqlite3.Connection, fixture: dict[str, Any]) -> HouseholdSnapshot:
    """Replace a household with the fixture's normal-day state."""
    snap = snapshot_from_fixture(fixture)
    hid = snap.household_id
    school = fixture["school"]
    with transaction(conn):
        plan_ids = [r["id"] for r in conn.execute("SELECT id FROM plans WHERE household_id=?", (hid,))]
        for pid in plan_ids:
            conn.execute("DELETE FROM actions WHERE plan_id=?", (pid,))
            conn.execute("DELETE FROM approvals WHERE plan_id=?", (pid,))
            conn.execute("DELETE FROM idempotency WHERE plan_id=?", (pid,))
            conn.execute("DELETE FROM verifications WHERE plan_id=?", (pid,))
        for table in ["audit_events", "notifications", "plans", "disruptions", "notices", "faults",
                      "supervision", "escorts", "calendar_events", "travel", "locations", "members"]:
            conn.execute(f"DELETE FROM {table} WHERE household_id=?", (hid,))
        conn.execute("DELETE FROM households WHERE id=?", (hid,))
        conn.execute(
            "INSERT INTO households (id, name, timezone, date, day_start, day_end, state_version, school_json)"
            " VALUES (?,?,?,?,?,?,1,?)",
            (hid, snap.name, snap.timezone, snap.date, snap.day_start, snap.day_end, json.dumps(school)),
        )
        for m in snap.members:
            conn.execute("INSERT INTO members VALUES (?,?,?)", (hid, m.id, m.model_dump_json()))
        for loc in snap.locations:
            conn.execute("INSERT INTO locations VALUES (?,?,?)", (hid, loc.id, loc.name))
        for t in snap.travel:
            conn.execute("INSERT INTO travel VALUES (?,?,?,?)", (hid, t.a, t.b, t.minutes))
        for ev in snap.events + derive_duty_events(snap):
            _insert_event(conn, hid, ev, source="household" if ev.kind == "commitment" else "hdr")
        for e in snap.escorts:
            conn.execute(
                "INSERT INTO escorts (household_id, id, child_id, destination, window_start, window_end, target_arrival,"
                " max_late, required, assigned_member_id, depart, arrive) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (hid, e.id, e.child_id, e.destination, e.window_start, e.window_end, e.target_arrival,
                 e.max_late, int(e.required), e.assigned_member_id, e.depart, e.arrive),
            )
        for s in snap.supervision:
            conn.execute(
                "INSERT INTO supervision (household_id, id, child_id, member_id, start_min, end_min) VALUES (?,?,?,?,?,?)",
                (hid, s.id, s.child_id, s.member_id, s.start, s.end),
            )
    return load_snapshot(conn, hid)


def _insert_event(conn: sqlite3.Connection, hid: str, ev: CalendarEvent, source: str, plan_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO calendar_events (household_id, id, member_id, title, start_min, end_min, location, hard, kind,"
        " status, responsibility_id, reminder_min, source, updated_by_plan) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (hid, ev.id, ev.member_id, ev.title, ev.start, ev.end, ev.location, int(ev.hard), ev.kind, ev.status,
         ev.responsibility_id, ev.reminder_min, source, plan_id),
    )


def household_row(conn: sqlite3.Connection, household_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM households WHERE id=?", (household_id,)).fetchone()
    if row is None:
        raise NotFound(f"household {household_id!r} not found")
    return row


def state_version(conn: sqlite3.Connection, household_id: str) -> int:
    return household_row(conn, household_id)["state_version"]


def bump_version(conn: sqlite3.Connection, household_id: str) -> int:
    conn.execute("UPDATE households SET state_version = state_version + 1 WHERE id=?", (household_id,))
    return state_version(conn, household_id)


def school_info(conn: sqlite3.Connection, household_id: str) -> SchoolInfo:
    s = json.loads(household_row(conn, household_id)["school_json"])
    return SchoolInfo(id=s["id"], name=s["name"], doors_open=hm(s["doors_open"]), classes_begin=hm(s["classes_begin"]))


def load_snapshot(conn: sqlite3.Connection, household_id: str) -> HouseholdSnapshot:
    hh = household_row(conn, household_id)
    members = [Member.model_validate_json(r["data_json"]) for r in conn.execute(
        "SELECT data_json FROM members WHERE household_id=? ORDER BY rowid", (household_id,))]
    locations = [Location(id=r["id"], name=r["name"]) for r in conn.execute(
        "SELECT id, name FROM locations WHERE household_id=? ORDER BY rowid", (household_id,))]
    travel = [TravelEdge(a=r["a"], b=r["b"], minutes=r["minutes"]) for r in conn.execute(
        "SELECT a, b, minutes FROM travel WHERE household_id=? ORDER BY rowid", (household_id,))]
    events = [
        CalendarEvent(
            id=r["id"], member_id=r["member_id"], title=r["title"], start=r["start_min"], end=r["end_min"],
            location=r["location"], hard=bool(r["hard"]), kind=r["kind"], status=r["status"],
            responsibility_id=r["responsibility_id"], reminder_min=r["reminder_min"],
        )
        for r in conn.execute("SELECT * FROM calendar_events WHERE household_id=? ORDER BY start_min, id", (household_id,))
    ]
    escorts = [
        Escort(
            id=r["id"], child_id=r["child_id"], destination=r["destination"], window_start=r["window_start"],
            window_end=r["window_end"], target_arrival=r["target_arrival"], max_late=r["max_late"],
            required=bool(r["required"]), assigned_member_id=r["assigned_member_id"], depart=r["depart"], arrive=r["arrive"],
        )
        for r in conn.execute(
            "SELECT * FROM escorts WHERE household_id=? AND status='active' ORDER BY rowid", (household_id,))
    ]
    supervision = [
        SupervisionSegment(id=r["id"], child_id=r["child_id"], member_id=r["member_id"], start=r["start_min"], end=r["end_min"])
        for r in conn.execute("SELECT * FROM supervision WHERE household_id=? ORDER BY child_id, start_min", (household_id,))
    ]
    return HouseholdSnapshot(
        household_id=hh["id"], name=hh["name"], date=hh["date"], timezone=hh["timezone"],
        state_version=hh["state_version"], day_start=hh["day_start"], day_end=hh["day_end"],
        members=members, locations=locations, travel=travel, events=events, escorts=escorts, supervision=supervision,
    )


def update_member(conn: sqlite3.Connection, household_id: str, member: Member) -> None:
    conn.execute(
        "UPDATE members SET data_json=? WHERE household_id=? AND id=?",
        (member.model_dump_json(), household_id, member.id),
    )


def update_escort_window(conn: sqlite3.Connection, household_id: str, escort: Escort) -> None:
    conn.execute(
        "UPDATE escorts SET window_start=?, window_end=?, target_arrival=? WHERE household_id=? AND id=?",
        (escort.window_start, escort.window_end, escort.target_arrival, household_id, escort.id),
    )


def cancel_escort(conn: sqlite3.Connection, household_id: str, escort_id: str) -> None:
    conn.execute("UPDATE escorts SET status='cancelled' WHERE household_id=? AND id=?", (household_id, escort_id))


def write_escort(conn: sqlite3.Connection, household_id: str, escort_id: str, member_id: str | None,
                 depart: int | None, arrive: int | None, plan_id: str) -> None:
    cur = conn.execute(
        "UPDATE escorts SET assigned_member_id=?, depart=?, arrive=?, updated_by_plan=? WHERE household_id=? AND id=?",
        (member_id, depart, arrive, plan_id, household_id, escort_id),
    )
    if cur.rowcount != 1:
        raise NotFound(f"escort {escort_id!r} not found")


def write_supervision(conn: sqlite3.Connection, household_id: str, child_id: str,
                      segments: list[tuple[str, int, int]], plan_id: str) -> None:
    conn.execute("DELETE FROM supervision WHERE household_id=? AND child_id=?", (household_id, child_id))
    for i, (member_id, start, end) in enumerate(segments, start=1):
        conn.execute(
            "INSERT INTO supervision (household_id, id, child_id, member_id, start_min, end_min, updated_by_plan)"
            " VALUES (?,?,?,?,?,?,?)",
            (household_id, f"sup-{child_id}-{i}", child_id, member_id, start, end, plan_id),
        )


def cancel_event(conn: sqlite3.Connection, household_id: str, event_id: str, plan_id: str) -> None:
    cur = conn.execute(
        "UPDATE calendar_events SET status='cancelled', updated_by_plan=? WHERE household_id=? AND id=?",
        (plan_id, household_id, event_id),
    )
    if cur.rowcount != 1:
        raise NotFound(f"calendar event {event_id!r} not found")


def create_event(conn: sqlite3.Connection, household_id: str, ev: CalendarEvent, plan_id: str) -> None:
    existing = conn.execute(
        "SELECT 1 FROM calendar_events WHERE household_id=? AND id=?", (household_id, ev.id)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE calendar_events SET member_id=?, title=?, start_min=?, end_min=?, location=?, hard=?, kind=?,"
            " status=?, responsibility_id=?, reminder_min=?, source='hdr', updated_by_plan=? WHERE household_id=? AND id=?",
            (ev.member_id, ev.title, ev.start, ev.end, ev.location, int(ev.hard), ev.kind, ev.status,
             ev.responsibility_id, ev.reminder_min, plan_id, household_id, ev.id),
        )
    else:
        _insert_event(conn, household_id, ev, source="hdr", plan_id=plan_id)


def read_event(conn: sqlite3.Connection, household_id: str, event_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM calendar_events WHERE household_id=? AND id=?", (household_id, event_id)
    ).fetchone()
