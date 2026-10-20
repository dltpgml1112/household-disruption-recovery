"""Fault injection for tests, evals and the failure demo. Never exposed as an MCP tool."""

from __future__ import annotations

import sqlite3

TARGETS = {"notify", "calendar", "escort", "supervision", "tool_delay"}


class FaultInjected(RuntimeError):
    pass


def add_fault(conn: sqlite3.Connection, household_id: str, target: str, selector: str = "*",
              times: int = 1, delay_ms: int = 0) -> None:
    if target not in TARGETS:
        raise ValueError(f"unknown fault target {target!r}")
    conn.execute(
        "INSERT INTO faults (household_id, target, selector, mode, remaining, delay_ms) VALUES (?,?,?,?,?,?)",
        (household_id, target, selector, "fail" if target != "tool_delay" else "delay", times, delay_ms),
    )


def clear_faults(conn: sqlite3.Connection, household_id: str) -> None:
    conn.execute("DELETE FROM faults WHERE household_id=?", (household_id,))


def take(conn: sqlite3.Connection, household_id: str, target: str, selector: str) -> sqlite3.Row | None:
    """Consume one matching fault. `times=-1` never runs out."""
    row = conn.execute(
        "SELECT * FROM faults WHERE household_id=? AND target=? AND (selector=? OR selector='*') AND remaining != 0"
        " ORDER BY id LIMIT 1",
        (household_id, target, selector),
    ).fetchone()
    if row is not None and row["remaining"] > 0:
        conn.execute("UPDATE faults SET remaining = remaining - 1 WHERE id=?", (row["id"],))
    return row


def check(conn: sqlite3.Connection, household_id: str, target: str, selector: str) -> None:
    if take(conn, household_id, target, selector) is not None:
        raise FaultInjected(f"injected {target} failure for {selector}")
