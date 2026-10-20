"""SQLite storage. One file per deployment; every request opens its own connection."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS households (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    timezone TEXT NOT NULL,
    date TEXT NOT NULL,
    day_start INTEGER NOT NULL,
    day_end INTEGER NOT NULL,
    state_version INTEGER NOT NULL DEFAULT 1,
    school_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    household_id TEXT NOT NULL REFERENCES households(id),
    id TEXT NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY (household_id, id)
);
CREATE TABLE IF NOT EXISTS locations (
    household_id TEXT NOT NULL REFERENCES households(id),
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (household_id, id)
);
CREATE TABLE IF NOT EXISTS travel (
    household_id TEXT NOT NULL REFERENCES households(id),
    a TEXT NOT NULL,
    b TEXT NOT NULL,
    minutes INTEGER NOT NULL,
    PRIMARY KEY (household_id, a, b)
);
CREATE TABLE IF NOT EXISTS calendar_events (
    household_id TEXT NOT NULL REFERENCES households(id),
    id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    title TEXT NOT NULL,
    start_min INTEGER NOT NULL,
    end_min INTEGER NOT NULL,
    location TEXT NOT NULL,
    hard INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('commitment', 'duty')),
    status TEXT NOT NULL CHECK (status IN ('confirmed', 'cancelled')),
    responsibility_id TEXT,
    reminder_min INTEGER,
    source TEXT NOT NULL DEFAULT 'household',
    updated_by_plan TEXT,
    PRIMARY KEY (household_id, id)
);
CREATE TABLE IF NOT EXISTS escorts (
    household_id TEXT NOT NULL REFERENCES households(id),
    id TEXT NOT NULL,
    child_id TEXT NOT NULL,
    destination TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    window_end INTEGER NOT NULL,
    target_arrival INTEGER NOT NULL,
    max_late INTEGER NOT NULL DEFAULT 0,
    required INTEGER NOT NULL DEFAULT 1,
    assigned_member_id TEXT,
    depart INTEGER,
    arrive INTEGER,
    updated_by_plan TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    PRIMARY KEY (household_id, id)
);
CREATE TABLE IF NOT EXISTS supervision (
    household_id TEXT NOT NULL REFERENCES households(id),
    id TEXT NOT NULL,
    child_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    start_min INTEGER NOT NULL,
    end_min INTEGER NOT NULL,
    updated_by_plan TEXT,
    PRIMARY KEY (household_id, id)
);
CREATE TABLE IF NOT EXISTS notices (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id),
    source TEXT NOT NULL,
    received_at TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    raw_length INTEGER NOT NULL,
    structured_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    needs_clarification INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS disruptions (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id),
    notice_id TEXT NOT NULL REFERENCES notices(id),
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    affected_json TEXT NOT NULL,
    propagation_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id),
    disruption_id TEXT NOT NULL REFERENCES disruptions(id),
    version INTEGER NOT NULL,
    base_state_version INTEGER NOT NULL,
    expected_state_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    feasible INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    actions_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(id),
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    target_json TEXT NOT NULL,
    prev_json TEXT,
    proposed_json TEXT,
    summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    applied_at TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(id),
    plan_version INTEGER NOT NULL,
    decision TEXT NOT NULL,
    approver_member_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    token_sha256 TEXT,
    expires_at TEXT,
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS faults (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id TEXT NOT NULL,
    target TEXT NOT NULL,
    selector TEXT NOT NULL,
    mode TEXT NOT NULL,
    remaining INTEGER NOT NULL,
    delay_ms INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS verifications (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    ready INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    household_id TEXT,
    disruption_id TEXT,
    plan_id TEXT,
    actor TEXT NOT NULL,
    tool TEXT NOT NULL,
    input_json TEXT NOT NULL,
    output_json TEXT NOT NULL,
    result TEXT NOT NULL,
    error_code TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS ix_audit_disruption ON audit_events(disruption_id);
CREATE INDEX IF NOT EXISTS ix_actions_plan ON actions(plan_id, seq);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db(path: str | Path) -> None:
    with closing(path) as conn:
        conn.executescript(SCHEMA)
        # databases created before escorts had a status (a closed school cancels a trip)
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(escorts)")}
        if "status" not in columns:
            conn.execute("ALTER TABLE escorts ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")


@contextmanager
def closing(path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
