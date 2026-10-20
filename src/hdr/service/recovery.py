"""The recovery workflow behind the MCP tools.

ingest notice -> plan -> approve -> apply -> verify, with an audit row for every call.
Every write bumps the household's state_version; a plan remembers the version it
expects, so any change made outside the plan (or a newer plan) makes it stale.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from hdr.domain.actions import ActionSpec, actions_hash, build_actions
from hdr.domain.checker import check
from hdr.domain.fixtures import DEMO_FIXTURE, load_fixture
from hdr.domain.models import CalendarEvent, HouseholdSnapshot
from hdr.domain.notice import apply_closure, apply_delay, parse_notice
from hdr.domain.planner import PlannerLimitExceeded, PlanResult, SegmentOut, plan
from hdr.domain.propagation import Conflict, propagate
from hdr.domain.timeutil import fmt, fmt_range, hm
from hdr.service import approval, faults
from hdr.service.schemas import (
    Card,
    ActionOut,
    ActionResult,
    ApplyOut,
    ApprovalOut,
    AuditEventOut,
    AuditOut,
    AvailabilityOut,
    CheckOut,
    DisruptionRef,
    EscortOut,
    EventOut,
    HouseholdStateOut,
    MemberOut,
    NoticeOut,
    PlanOut,
    VerifyOut,
)
from hdr.store import repo
from hdr.store.db import closing, connect, init_db, transaction

MAX_NOTICE_CHARS = 2000
HOUSEHOLD_ID = re.compile(r"[A-Za-z0-9-]{3,40}")
OPEN_PLAN_STATES = ("proposed", "approved")
# notice kinds the planner handles, and the disruption type each one opens
PLANNED_KINDS = {"delay": "school_delay", "closure": "school_closure"}
APPROVAL_RULES = [
    "Creating a plan never changes household state.",
    "Calendar writes, reassignments, cancellations and messages run only after a parent approves one specific plan version.",
    "An approval is void once the plan is replaced or household state changes outside the plan.",
    "A plan is reported ready only after every write is read back and the whole household passes the constraint check.",
]


class HdrError(Exception):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details


@dataclass
class Settings:
    db_path: str
    approval_secret: bytes
    approval_ttl_seconds: int = 900
    fixture_path: str = str(DEMO_FIXTURE)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _clock(hhmm: str | None) -> str:
    """'09:55' -> '9:55' (how the time is read out and shown on a card)."""
    if not hhmm:
        return ""
    hour, minute = hhmm.split(":")
    return f"{int(hour)}:{minute}"


def _clock_ranges(text: str) -> str:
    """'08:30-10:00' -> '8:30–10:00' inside a sentence."""
    return re.sub(r"\b(\d\d:\d\d)-(\d\d:\d\d)\b", lambda m: f"{_clock(m.group(1))}–{_clock(m.group(2))}", text)


class RecoveryService:
    def __init__(self, settings: Settings):
        self.settings = settings
        init_db(settings.db_path)

    # ---- plumbing ---------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        return connect(self.settings.db_path)

    def _audit(self, tool: str, actor: str, inputs: dict, outputs: dict, result: str,
               error_code: str | None, ids: dict, duration_ms: int) -> None:
        with closing(self.settings.db_path) as conn:
            conn.execute(
                "INSERT INTO audit_events (ts, household_id, disruption_id, plan_id, actor, tool, input_json,"
                " output_json, result, error_code, duration_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (repo.now_iso(), ids.get("household_id"), ids.get("disruption_id"), ids.get("plan_id"), actor, tool,
                 json.dumps(inputs, default=str), json.dumps(outputs, default=str), result, error_code, duration_ms),
            )

    @contextmanager
    def _audited(self, tool: str, actor: str, inputs: dict, **ids: Any) -> Iterator[dict]:
        rec: dict[str, Any] = {"ids": {k: v for k, v in ids.items() if v}, "output": {}}
        started = time.perf_counter()
        try:
            yield rec
        except HdrError as e:
            self._audit(tool, actor, inputs, {"error": e.message}, "error", e.code, rec["ids"],
                        int((time.perf_counter() - started) * 1000))
            raise
        except Exception as e:  # unexpected: still leave a trace, then surface it
            self._audit(tool, actor, inputs, {"error": type(e).__name__}, "error", "INTERNAL_ERROR", rec["ids"],
                        int((time.perf_counter() - started) * 1000))
            raise
        else:
            self._audit(tool, actor, inputs, rec["output"], "ok", None, rec["ids"],
                        int((time.perf_counter() - started) * 1000))

    def _plan_row(self, conn: sqlite3.Connection, plan_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise HdrError("NOT_FOUND", f"plan {plan_id!r} not found")
        return row

    def _disruption_row(self, conn: sqlite3.Connection, disruption_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM disruptions WHERE id=?", (disruption_id,)).fetchone()
        if row is None:
            raise HdrError("NOT_FOUND", f"disruption {disruption_id!r} not found")
        return row

    def _snapshot(self, conn: sqlite3.Connection, household_id: str) -> HouseholdSnapshot:
        try:
            return repo.load_snapshot(conn, household_id)
        except repo.NotFound as e:
            raise HdrError("NOT_FOUND", str(e)) from None

    def _invalidate_open_plans(self, conn: sqlite3.Connection, household_id: str, status: str) -> list[str]:
        rows = conn.execute(
            f"SELECT id FROM plans WHERE household_id=? AND status IN ({','.join('?' * len(OPEN_PLAN_STATES))})",
            (household_id, *OPEN_PLAN_STATES),
        ).fetchall()
        ids = [r["id"] for r in rows]
        for pid in ids:
            conn.execute("UPDATE plans SET status=? WHERE id=?", (status, pid))
            conn.execute("UPDATE approvals SET revoked=1 WHERE plan_id=?", (pid,))
        return ids

    # ---- admin (not exposed over MCP) ------------------------------------

    def reset(self, variant: str | None = None, household_id: str | None = None) -> HouseholdSnapshot:
        """Load the demo family's normal day. `household_id` loads a separate copy under that id
        (the hosted demo gives each browser session its own household)."""
        fixture = load_fixture(self.settings.fixture_path, variant=variant)
        if household_id:
            if not HOUSEHOLD_ID.fullmatch(household_id):
                raise HdrError("INVALID_INPUT", "household_id must be 3-40 letters, digits or dashes")
            fixture["household"] = {**fixture["household"], "id": household_id}
        with closing(self.settings.db_path) as conn:
            return repo.reset_household(conn, fixture)

    def add_fault(self, household_id: str, target: str, selector: str = "*", times: int = 1, delay_ms: int = 0) -> None:
        with closing(self.settings.db_path) as conn, transaction(conn):
            faults.add_fault(conn, household_id, target, selector, times, delay_ms)

    def clear_faults(self, household_id: str) -> None:
        with closing(self.settings.db_path) as conn, transaction(conn):
            faults.clear_faults(conn, household_id)

    def tool_delay_ms(self, tool: str) -> int:
        with closing(self.settings.db_path) as conn, transaction(conn):
            row = faults.take(conn, "*", "tool_delay", tool)
        return row["delay_ms"] if row is not None else 0

    def demo_notice(self, kind: str | None = None) -> dict:
        data = load_fixture(self.settings.fixture_path)
        if kind:
            if kind not in data.get("other_notices", {}):
                raise HdrError("INVALID_INPUT", f"no demo notice of kind {kind!r}")
            return data["other_notices"][kind]
        return data["notice"]

    # ---- tools ------------------------------------------------------------

    def get_household_state(self, household_id: str, window_start: str | None = None,
                            window_end: str | None = None, actor: str = "agent") -> HouseholdStateOut:
        inputs = {"household_id": household_id, "window": [window_start, window_end]}
        with self._audited("get_household_state", actor, inputs, household_id=household_id) as rec:
            with closing(self.settings.db_path) as conn:
                snap = self._snapshot(conn, household_id)
                disruptions = conn.execute(
                    "SELECT id, kind, status FROM disruptions WHERE household_id=? AND status != 'resolved' ORDER BY created_at",
                    (household_id,),
                ).fetchall()
            lo = hm(window_start) if window_start else snap.day_start
            hi = hm(window_end) if window_end else snap.day_end
            out = HouseholdStateOut(
                household_id=snap.household_id,
                name=snap.name,
                date=snap.date,
                timezone=snap.timezone,
                state_version=snap.state_version,
                members=[
                    MemberOut(
                        id=m.id, name=m.name, role=m.role, age=m.age, available=m.available,
                        available_window=fmt_range(*w) if (w := snap.availability_window(m)) and m.role == "caregiver" else None,
                        can_supervise=m.can_supervise, can_escort=m.can_escort,
                        needs_supervision=m.needs_supervision, ready_at=fmt(m.ready_at), note=m.note,
                    )
                    for m in snap.members
                ],
                calendar=[
                    EventOut(
                        id=e.id, member_id=e.member_id, title=e.title, start=fmt(e.start), end=fmt(e.end),
                        location=e.location, hard=e.hard, kind=e.kind, status=e.status, responsibility_id=e.responsibility_id,
                    )
                    for e in snap.events
                    if e.end > lo and e.start < hi
                ],
                escorts=[
                    EscortOut(
                        id=e.id, child_id=e.child_id, destination=e.destination,
                        window=fmt_range(e.window_start, e.window_end), target_arrival=fmt(e.target_arrival),
                        assigned_member_id=e.assigned_member_id, depart=fmt(e.depart), arrive=fmt(e.arrive),
                    )
                    for e in snap.escorts
                ],
                supervision=[
                    SegmentOut(child_id=s.child_id, member_id=s.member_id, start=fmt(s.start), end=fmt(s.end))
                    for s in snap.supervision
                ],
                travel_minutes=[{"between": [t.a, t.b], "minutes": t.minutes} for t in snap.travel],
                rules=[
                    f"{c.name} must be with an approved adult from {fmt(c.ready_at)} until drop-off."
                    for c in snap.children() if c.needs_supervision
                ] + APPROVAL_RULES,
                open_disruptions=[DisruptionRef(id=r["id"], kind=r["kind"], status=r["status"]) for r in disruptions],
            )
            rec["output"] = {"state_version": out.state_version, "events": len(out.calendar)}
            return out

    def ingest_school_notice(self, household_id: str, raw_notice: str, source: str, received_at: str,
                             clarification: dict | None = None, actor: str = "agent") -> NoticeOut:
        raw_notice = raw_notice or ""
        inputs = {"household_id": household_id, "source": source, "received_at": received_at,
                  "raw_sha256": _sha(raw_notice)[:16], "raw_length": len(raw_notice), "clarification": clarification}
        with self._audited("ingest_school_notice", actor, inputs, household_id=household_id) as rec:
            if not raw_notice.strip():
                raise HdrError("INVALID_INPUT", "raw_notice is empty")
            if len(raw_notice) > MAX_NOTICE_CHARS:
                raise HdrError("INVALID_INPUT", f"raw_notice longer than {MAX_NOTICE_CHARS} characters")
            with closing(self.settings.db_path) as conn:
                self._snapshot(conn, household_id)  # existence check
                school = repo.school_info(conn, household_id)
                parsed = parse_notice(raw_notice, school)
                if clarification:
                    try:
                        doors, classes = hm(clarification["doors_open"]), hm(clarification["classes_begin"])
                    except (KeyError, ValueError, AttributeError):
                        raise HdrError("INVALID_INPUT", "clarification needs doors_open and classes_begin as HH:MM") from None
                    parsed = parsed.model_copy(update={
                        "kind": "delay", "doors_open": fmt(doors), "classes_begin": fmt(classes),
                        "delay_minutes": classes - school.classes_begin, "confidence": 1.0,
                        "needs_clarification": False, "questions": [],
                        "evidence": parsed.evidence + ["times confirmed by a parent"],
                    })
                raw_sha = _sha(raw_notice + json.dumps(clarification or {}, sort_keys=True))
                dup = conn.execute(
                    "SELECT n.id AS notice_id, d.id AS disruption_id, d.affected_json, d.propagation_json"
                    " FROM notices n JOIN disruptions d ON d.notice_id = n.id"
                    " WHERE n.household_id=? AND n.raw_sha256=? AND d.status != 'resolved'",
                    (household_id, raw_sha),
                ).fetchone()
                if dup is not None:
                    prop = json.loads(dup["propagation_json"])
                    rec["ids"]["disruption_id"] = dup["disruption_id"]
                    rec["output"] = {"duplicate": True}
                    return NoticeOut(
                        notice_id=dup["notice_id"], disruption_id=dup["disruption_id"], structured=parsed, applied=True,
                        affected_responsibilities=json.loads(dup["affected_json"]),
                        conflicts=[Conflict(**c) for c in prop["conflicts"]],
                        state_version=repo.state_version(conn, household_id), duplicate=True,
                        message="This notice was already processed.",
                    )
                notice_id = _new_id("notice")
                disruption_id = None
                affected: list[str] = []
                conflicts: list[Conflict] = []
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO notices VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (notice_id, household_id, source, received_at, raw_sha, len(raw_notice),
                         parsed.model_dump_json(), parsed.confidence, int(parsed.needs_clarification), repo.now_iso()),
                    )
                    if parsed.kind in PLANNED_KINDS and not parsed.needs_clarification:
                        snap = self._snapshot(conn, household_id)
                        if parsed.kind == "delay":
                            after, affected = apply_delay(snap, parsed)
                            for e in after.escorts:
                                if e.id in affected:
                                    repo.update_escort_window(conn, household_id, e)
                        else:
                            after, affected = apply_closure(snap, parsed)
                            for escort_id in affected:
                                repo.cancel_escort(conn, household_id, escort_id)
                        repo.bump_version(conn, household_id)
                        after = self._snapshot(conn, household_id)
                        prop = propagate(after, affected)
                        conflicts = prop.conflicts
                        affected = prop.affected_responsibilities
                        disruption_id = _new_id("disr")
                        conn.execute(
                            "INSERT INTO disruptions VALUES (?,?,?,?,?,?,?,?)",
                            (disruption_id, household_id, notice_id, PLANNED_KINDS[parsed.kind], "open",
                             json.dumps(affected), prop.model_dump_json(), repo.now_iso()),
                        )
                version = repo.state_version(conn, household_id)
            if parsed.kind in PLANNED_KINDS and not parsed.needs_clarification:
                message = f"{len(conflicts)} conflict(s) found after the school change."
            elif parsed.needs_clarification:
                message = "Need a parent to confirm the notice before planning: " + " ".join(parsed.questions)
            else:
                message = f"Recognised a {parsed.kind.replace('_', ' ')}, which this version does not plan for yet."
            rec["ids"]["disruption_id"] = disruption_id
            rec["output"] = {"kind": parsed.kind, "confidence": parsed.confidence, "conflicts": len(conflicts),
                             "needs_clarification": parsed.needs_clarification}
            return NoticeOut(
                notice_id=notice_id, disruption_id=disruption_id, structured=parsed,
                applied=disruption_id is not None, affected_responsibilities=affected,
                conflicts=conflicts, state_version=version, message=message,
            )

    def plan_disruption_recovery(self, disruption_id: str, household_state_version: int,
                                 actor: str = "agent") -> PlanOut:
        inputs = {"disruption_id": disruption_id, "household_state_version": household_state_version}
        with self._audited("plan_disruption_recovery", actor, inputs, disruption_id=disruption_id) as rec:
            with closing(self.settings.db_path) as conn:
                d = self._disruption_row(conn, disruption_id)
                hid = d["household_id"]
                rec["ids"]["household_id"] = hid
                current = repo.state_version(conn, hid)
                if household_state_version != current:
                    raise HdrError("STALE_STATE", f"household is at version {current}, not {household_state_version}; re-read state",
                                   current_version=current)
                snap = self._snapshot(conn, hid)
                try:
                    result = plan(snap)
                except PlannerLimitExceeded as e:
                    raise HdrError("PLANNER_LIMIT", str(e)) from None
                specs = build_actions(snap, result, d["kind"])
                ahash = actions_hash(specs)
                prop = propagate(snap, json.loads(d["affected_json"]))
                plan_id = _new_id("plan")
                with transaction(conn):
                    superseded = self._invalidate_open_plans(conn, hid, "superseded")
                    version = (conn.execute("SELECT COALESCE(MAX(version), 0) FROM plans WHERE disruption_id=?",
                                            (disruption_id,)).fetchone()[0]) + 1
                    status = "proposed" if result.feasible else "infeasible"
                    conn.execute(
                        "INSERT INTO plans VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (plan_id, hid, disruption_id, version, current, current, status, int(result.feasible),
                         result.model_dump_json(), ahash, repo.now_iso()),
                    )
                    for spec in specs:
                        conn.execute(
                            "INSERT INTO actions (id, plan_id, seq, type, target_json, prev_json, proposed_json, summary)"
                            " VALUES (?,?,?,?,?,?,?,?)",
                            (f"{plan_id}-a{spec.seq:02d}", plan_id, spec.seq, spec.type, json.dumps(spec.target),
                             json.dumps(spec.prev), json.dumps(spec.proposed), spec.summary),
                        )
                    conn.execute("UPDATE disruptions SET status=? WHERE id=?",
                                 ("planned" if result.feasible else "needs_decision", disruption_id))
            rec["ids"]["plan_id"] = plan_id
            rec["output"] = {"plan_id": plan_id, "version": version, "feasible": result.feasible,
                             "actions": len(specs), "conflicts": len(prop.conflicts)}
            notified = [snap.member(sp.target["member_id"]).name for sp in specs if sp.type == "notify_member"]
            return PlanOut(
                plan_id=plan_id, version=version, disruption_id=disruption_id, status=status,
                base_state_version=current, feasible=result.feasible, conflicts=prop.conflicts,
                actions=[self._action_out(plan_id, s, "pending") for s in specs],
                assignments=result.assignments, supervision=result.supervision, decisions=result.decisions,
                rationale=self._rationale(snap, result), unresolved=result.unresolved, options=result.options,
                objective=result.objective, combinations_evaluated=result.combinations_evaluated,
                requires_approval=bool(specs), superseded_plans=superseded,
                changes_total=len(specs),
                people_notified=notified,
                card=self._plan_card(snap, result, specs, notified),
            )

    @staticmethod
    def _plan_card(snap: HouseholdSnapshot, result: PlanResult, specs: list[ActionSpec], notified: list[str]) -> Card:
        """The plan as a device card: who drives whom, who is home with whom, one action."""
        if not result.feasible:
            detail = result.unresolved[0].detail if result.unresolved else "Some responsibilities have no adult."
            return Card(title="No plan covers everyone", fields=[_clock_ranges(detail), "Nothing was written."],
                        action="Find another adult or move a commitment")
        changed = {d.escort_id for d in result.decisions}
        drives = sorted((a for a in result.assignments if a.escort_id in changed and a.member_id and a.depart),
                        key=lambda a: a.depart or "")
        title = "; ".join(f"{snap.member(a.member_id).name} takes {snap.member(a.child_id).name} at {_clock(a.depart)}"
                          for a in drives) or "Plan ready"
        fields: list[str] = []
        by_child: dict[str, list] = {}
        for seg in result.supervision:
            by_child.setdefault(seg.child_id, []).append(seg)
        if by_child:
            child, segs = max(by_child.items(), key=lambda kv: len(kv[1]))
            if len(segs) > 1:
                parts = [f"{snap.member(s.member_id).name} {_clock(s.start)}–{_clock(s.end)}"
                         for s in sorted(segs, key=lambda s: s.start)]
                fields.append(f"{snap.member(child).name} at home: " + ", ".join(parts))
        fields.append(f"{len(specs)} changes · messages to {', '.join(notified)}" if notified else f"{len(specs)} changes")
        return Card(title=title, fields=fields[:2], action=f"Apply {len(specs)} changes")

    @staticmethod
    def _action_out(plan_id: str, spec: ActionSpec, status: str) -> ActionOut:
        return ActionOut(seq=spec.seq, action_id=f"{plan_id}-a{spec.seq:02d}", type=spec.type, summary=spec.summary,
                         target=spec.target, before=spec.prev, after=spec.proposed, status=status)

    @staticmethod
    def _rationale(snap: HouseholdSnapshot, result: PlanResult) -> list[str]:
        lines: list[str] = []
        for d in result.decisions:
            escort = next(e for e in snap.escorts if e.id == d.escort_id)
            child = snap.member(escort.child_id).name
            if d.chosen_member_id:
                who = snap.member(d.chosen_member_id).name
                lines.append(f"{child}: {who} drives ({d.reason}).")
            for alt in d.alternatives:
                name = snap.member(alt.member_id).name if alt.member_id else "Leaving it unassigned"
                verb = "can't" if alt.outcome == "infeasible" else "could, but"
                lines.append(f"  {name} {verb}: {alt.detail}")
        by_child: dict[str, list[str]] = {}
        for seg in result.supervision:
            by_child.setdefault(seg.child_id, []).append(f"{snap.member(seg.member_id).name} {seg.start}-{seg.end}")
        for child_id, parts in by_child.items():
            lines.append(f"{snap.member(child_id).name} at home: " + ", ".join(parts) + ".")
        if result.engaged_caregivers:
            names = ", ".join(snap.member(m).name for m in result.engaged_caregivers)
            lines.append(f"Caregiver called in: {names}.")
        if result.released_events:
            lines.append("Soft commitments moved: " + ", ".join(result.released_events) + ".")
        if not result.feasible:
            lines.append("No plan satisfies every hard constraint. A parent has to decide.")
        return lines

    def approve_recovery_plan(self, plan_id: str, plan_version: int, decision: str, approver_member_id: str,
                              channel: str = "voice", actor: str | None = None) -> ApprovalOut:
        inputs = {"plan_id": plan_id, "plan_version": plan_version, "decision": decision,
                  "approver_member_id": approver_member_id, "channel": channel}
        actor = actor or f"parent:{approver_member_id}"
        with self._audited("approve_recovery_plan", actor, inputs, plan_id=plan_id) as rec:
            if decision not in ("approve", "reject"):
                raise HdrError("INVALID_INPUT", "decision must be 'approve' or 'reject'")
            if channel not in ("voice", "screen", "api"):
                raise HdrError("INVALID_INPUT", "channel must be voice, screen or api")
            with closing(self.settings.db_path) as conn:
                p = self._plan_row(conn, plan_id)
                hid = p["household_id"]
                rec["ids"].update(household_id=hid, disruption_id=p["disruption_id"])
                snap = self._snapshot(conn, hid)
                if not snap.has_member(approver_member_id):
                    raise HdrError("INVALID_MEMBER", f"{approver_member_id!r} is not a household member")
                if snap.member(approver_member_id).role != "parent":
                    raise HdrError("NOT_AUTHORIZED", "only a parent can approve a recovery plan")
                if p["version"] != plan_version:
                    raise HdrError("PLAN_VERSION_MISMATCH", f"plan is version {p['version']}, not {plan_version}")
                if p["status"] == "stale":
                    raise HdrError("STALE_PLAN", "the household changed after this plan was made (for example someone's "
                                                 "availability); make a new plan with plan_disruption_recovery")
                if p["status"] != "proposed":
                    raise HdrError("PLAN_NOT_APPROVABLE", f"plan is {p['status']}")
                with transaction(conn):
                    if repo.state_version(conn, hid) != p["expected_state_version"]:
                        conn.execute("UPDATE plans SET status='stale' WHERE id=?", (plan_id,))
                        raise HdrError("STALE_PLAN", "the household changed after this plan was made; make a new plan "
                                                     "with plan_disruption_recovery")
                    token = expires = None
                    if decision == "approve":
                        token, exp = approval.issue(
                            self.settings.approval_secret, plan_id=plan_id, plan_version=p["version"],
                            base_state_version=p["base_state_version"], actions_hash=p["actions_hash"],
                            ttl_seconds=self.settings.approval_ttl_seconds,
                        )
                        expires = datetime.fromtimestamp(exp, timezone.utc).isoformat()
                    conn.execute(
                        "INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,0,?)",
                        (_new_id("appr"), plan_id, p["version"], decision, approver_member_id, channel,
                         approval.token_sha256(token) if token else None, expires, repo.now_iso()),
                    )
                    status = "approved" if decision == "approve" else "rejected"
                    conn.execute("UPDATE plans SET status=? WHERE id=?", (status, plan_id))
                    n_actions = conn.execute("SELECT COUNT(*) FROM actions WHERE plan_id=?", (plan_id,)).fetchone()[0]
            rec["output"] = {"status": status, "token_issued": token is not None}
            return ApprovalOut(plan_id=plan_id, plan_version=p["version"], decision=decision, status=status,
                               approval_token=token, expires_at=expires, approved_actions=n_actions if token else 0)

    def apply_recovery_plan(self, plan_id: str, approval_token: str, idempotency_key: str,
                            actor: str = "agent") -> ApplyOut:
        inputs = {"plan_id": plan_id, "idempotency_key": idempotency_key,
                  "approval_token_sha256": approval.token_sha256(approval_token or "")[:16]}
        with self._audited("apply_recovery_plan", actor, inputs, plan_id=plan_id) as rec:
            if not idempotency_key or not 8 <= len(idempotency_key) <= 128:
                raise HdrError("INVALID_INPUT", "idempotency_key must be 8-128 characters")
            if not approval_token:
                raise HdrError("APPROVAL_REQUIRED", "apply needs an approval token from approve_recovery_plan")
            request_sha = _sha(f"{plan_id}|{approval.token_sha256(approval_token)}")
            with closing(self.settings.db_path) as conn:
                prior = conn.execute("SELECT * FROM idempotency WHERE key=?", (idempotency_key,)).fetchone()
                if prior is not None:
                    if prior["request_sha256"] != request_sha:
                        raise HdrError("IDEMPOTENCY_CONFLICT", "this idempotency key was used for a different request")
                    stored = json.loads(prior["response_json"])
                    if stored.get("status") == "in_progress":
                        raise HdrError("IN_PROGRESS", "this request is still running")
                    rec["output"] = {"replay": True}
                    return ApplyOut(**{**stored, "idempotent_replay": True})

                p = self._plan_row(conn, plan_id)
                hid = p["household_id"]
                rec["ids"].update(household_id=hid, disruption_id=p["disruption_id"])
                try:
                    claims = approval.verify(self.settings.approval_secret, approval_token)
                except approval.ApprovalError as e:
                    raise HdrError(e.code, str(e)) from None
                if (claims.get("p"), claims.get("v"), claims.get("h"), claims.get("s")) != (
                    plan_id, p["version"], p["actions_hash"], p["base_state_version"]
                ):
                    raise HdrError("APPROVAL_INVALID", "approval token was issued for a different plan or version")
                appr = conn.execute(
                    "SELECT * FROM approvals WHERE plan_id=? AND token_sha256=? AND decision='approve'",
                    (plan_id, approval.token_sha256(approval_token)),
                ).fetchone()
                if appr is None or appr["revoked"]:
                    raise HdrError("APPROVAL_INVALID", "approval was revoked or never recorded")
                if p["status"] == "applied" or p["status"] == "verified":
                    raise HdrError("PLAN_ALREADY_APPLIED", "every action in this plan has already been applied")
                if p["status"] not in ("approved", "partially_applied"):
                    raise HdrError("PLAN_NOT_APPROVED", f"plan is {p['status']}")

                with transaction(conn):
                    if repo.state_version(conn, hid) != p["expected_state_version"]:
                        conn.execute("UPDATE plans SET status='stale' WHERE id=?", (plan_id,))
                        conn.execute("UPDATE approvals SET revoked=1 WHERE plan_id=?", (plan_id,))
                        raise HdrError("STALE_PLAN", "household changed since approval; plan again and re-approve")
                    conn.execute(
                        "INSERT INTO idempotency VALUES (?,?,?,?,?)",
                        (idempotency_key, plan_id, request_sha, json.dumps({"status": "in_progress"}), repo.now_iso()),
                    )
                    conn.execute("UPDATE plans SET status='applying' WHERE id=?", (plan_id,))

                rows = conn.execute("SELECT * FROM actions WHERE plan_id=? ORDER BY seq", (plan_id,)).fetchall()
                skipped = 0
                for row in rows:
                    if row["status"] == "applied":
                        skipped += 1
                        continue
                    self._execute_action(conn, hid, plan_id, row)

                rows = conn.execute("SELECT * FROM actions WHERE plan_id=? ORDER BY seq", (plan_id,)).fetchall()
                failed = [r for r in rows if r["status"] != "applied"]
                status = "applied" if not failed else "partially_applied"
                results = [
                    ActionResult(seq=r["seq"], action_id=r["id"], type=r["type"], summary=r["summary"],
                                 status=r["status"], error=r["error"], attempts=r["attempts"])
                    for r in rows
                ]
                next_step = (
                    "Read the state back with verify_recovery."
                    if not failed
                    else "Some writes failed. Verify to see what is missing, then retry apply with a new idempotency key "
                    "(only failed actions run again)."
                )
                out = ApplyOut(
                    plan_id=plan_id, status=status, results=results,
                    applied=sum(1 for r in rows if r["status"] == "applied") - skipped,
                    failed=len(failed), skipped_already_applied=skipped,
                    state_version=repo.state_version(conn, hid), next_step=next_step,
                )
                with transaction(conn):
                    conn.execute("UPDATE plans SET status=? WHERE id=?", (status, plan_id))
                    conn.execute("UPDATE idempotency SET response_json=? WHERE key=?",
                                 (out.model_dump_json(), idempotency_key))
            rec["output"] = {"status": status, "applied": out.applied, "failed": out.failed}
            return out

    def _execute_action(self, conn: sqlite3.Connection, hid: str, plan_id: str, row: sqlite3.Row) -> None:
        target = json.loads(row["target_json"])
        proposed = json.loads(row["proposed_json"]) if row["proposed_json"] else None
        kind = row["type"]
        fault_key = {
            "assign_escort": ("escort", target.get("escort_id")),
            "set_supervision": ("supervision", target.get("child_id")),
            "cancel_calendar_event": ("calendar", target.get("event_id")),
            "create_calendar_event": ("calendar", target.get("event_id")),
            "notify_member": ("notify", target.get("member_id")),
        }[kind]
        with transaction(conn):
            injected = faults.take(conn, hid, *fault_key)
        error: str | None = None
        if injected is not None:
            error = f"{fault_key[0]} service unavailable (injected fault)"
        else:
            try:
                with transaction(conn):
                    if kind == "assign_escort":
                        repo.write_escort(conn, hid, target["escort_id"], proposed["member_id"],
                                          hm(proposed["depart"]), hm(proposed["arrive"]), plan_id)
                    elif kind == "set_supervision":
                        repo.write_supervision(conn, hid, target["child_id"],
                                               [(m, hm(s), hm(e)) for m, s, e in proposed["segments"]], plan_id)
                    elif kind == "cancel_calendar_event":
                        repo.cancel_event(conn, hid, target["event_id"], plan_id)
                    elif kind == "create_calendar_event":
                        repo.create_event(conn, hid, CalendarEvent(
                            id=target["event_id"], member_id=proposed["member_id"], title=proposed["title"],
                            start=hm(proposed["start"]), end=hm(proposed["end"]), location=proposed["location"],
                            hard=True, kind=proposed["kind"], status="confirmed",
                            responsibility_id=proposed["responsibility_id"], reminder_min=proposed["reminder_min"],
                        ), plan_id)
                    elif kind == "notify_member":
                        # simulated push channel: delivery is recorded in the notifications outbox
                        conn.execute(
                            "INSERT INTO notifications VALUES (?,?,?,?,?,?,?,?,?)",
                            (_new_id("msg"), hid, plan_id, row["id"], target["member_id"], proposed.get("channel", "push"),
                             proposed["message"], "delivered", repo.now_iso()),
                        )
                    new_version = repo.bump_version(conn, hid)
                    conn.execute("UPDATE plans SET expected_state_version=? WHERE id=?", (new_version, plan_id))
                    conn.execute(
                        "UPDATE actions SET status='applied', error=NULL, attempts=attempts+1, applied_at=? WHERE id=?",
                        (repo.now_iso(), row["id"]),
                    )
                return
            except (repo.NotFound, sqlite3.DatabaseError, KeyError) as e:
                error = f"{type(e).__name__}: {e}"
        with transaction(conn):
            conn.execute("UPDATE actions SET status='failed', error=?, attempts=attempts+1 WHERE id=?", (error, row["id"]))

    def verify_recovery(self, plan_id: str, actor: str = "agent") -> VerifyOut:
        with self._audited("verify_recovery", actor, {"plan_id": plan_id}, plan_id=plan_id) as rec:
            # fresh connection: read back what was actually written
            with closing(self.settings.db_path) as conn:
                p = self._plan_row(conn, plan_id)
                hid = p["household_id"]
                rec["ids"].update(household_id=hid, disruption_id=p["disruption_id"])
                d = self._disruption_row(conn, p["disruption_id"])
                rows = conn.execute("SELECT * FROM actions WHERE plan_id=? ORDER BY seq", (plan_id,)).fetchall()
                checks = [self._check_action(conn, hid, r) for r in rows]
                state = self._snapshot(conn, hid)
            report = check(state)
            result = PlanResult.model_validate_json(p["result_json"])

            affected = set(json.loads(d["affected_json"]))
            for r in rows:
                t = json.loads(r["target_json"])
                if r["type"] == "assign_escort":
                    affected.add(t["escort_id"])
                elif r["type"] == "set_supervision":
                    affected.add(f"supervise:{t['child_id']}")
            flagged = {ref for v in report.violations for ref in v.refs}
            flagged |= {f"supervise:{ref}" for v in report.violations if v.code == "SUPERVISION_GAP" for ref in v.refs}
            resolved = [a for a in affected if a not in flagged]

            failed = [c.summary for c, r in zip(checks, rows) if r["status"] != "applied"]
            verified = sum(1 for c in checks if c.verified)
            ready = bool(result.feasible and rows and verified == len(rows) and report.ok)
            violations = [v.detail for v in report.violations]
            if not result.feasible:
                violations += [u.detail for u in result.unresolved]
            if ready:
                summary = (f"HOUSEHOLD READY: {verified}/{len(rows)} writes verified, "
                           f"{len(resolved)}/{len(affected)} affected responsibilities resolved, "
                           f"0 unassigned, 0 constraint violations.")
                card = Card(title="Household ready",
                            fields=[f"{verified}/{len(rows)} changes read back",
                                    f"{len(resolved)}/{len(affected)} responsibilities covered"])
            else:
                summary = (f"NOT READY: {verified}/{len(rows)} writes verified, {len(failed)} failed, "
                           f"{len(report.unassigned)} unassigned, {len(violations)} open issue(s).")
                card = Card(title=f"Not ready: {len(failed)} change{'s' if len(failed) != 1 else ''} did not go through"
                            if failed else "Not ready",
                            fields=[f"Failed: {', '.join(failed)}" if failed else f"{len(violations)} open issue(s)",
                                    f"{verified}/{len(rows)} changes read back"],
                            action="Try again" if failed else None)
            out = VerifyOut(
                plan_id=plan_id, ready=ready, checks=checks, writes_verified=verified, writes_total=len(rows),
                affected_total=len(affected), affected_resolved=len(resolved), unassigned=report.unassigned,
                violations=violations, failed_actions=failed, summary=summary, card=card,
            )
            with closing(self.settings.db_path) as conn, transaction(conn):
                conn.execute("INSERT INTO verifications VALUES (?,?,?,?,?)",
                             (_new_id("ver"), plan_id, int(ready), out.model_dump_json(), repo.now_iso()))
                if ready:
                    conn.execute("UPDATE plans SET status='verified' WHERE id=?", (plan_id,))
                    conn.execute("UPDATE disruptions SET status='resolved' WHERE id=?", (p["disruption_id"],))
            rec["output"] = {"ready": ready, "verified": verified, "total": len(rows), "violations": len(violations)}
            return out

    @staticmethod
    def _check_action(conn: sqlite3.Connection, hid: str, row: sqlite3.Row) -> CheckOut:
        target = json.loads(row["target_json"])
        proposed = json.loads(row["proposed_json"]) if row["proposed_json"] else None
        kind = row["type"]
        actual: Any = None
        expected: Any = proposed
        if kind == "assign_escort":
            r = conn.execute("SELECT assigned_member_id, depart, arrive FROM escorts WHERE household_id=? AND id=?",
                             (hid, target["escort_id"])).fetchone()
            actual = r and {"member_id": r["assigned_member_id"], "depart": fmt(r["depart"]), "arrive": fmt(r["arrive"])}
        elif kind == "set_supervision":
            rs = conn.execute("SELECT member_id, start_min, end_min FROM supervision WHERE household_id=? AND child_id=?"
                              " ORDER BY start_min", (hid, target["child_id"])).fetchall()
            actual = {"segments": [[r["member_id"], fmt(r["start_min"]), fmt(r["end_min"])] for r in rs]}
        elif kind == "cancel_calendar_event":
            r = repo.read_event(conn, hid, target["event_id"])
            actual = r and {"status": r["status"]}
        elif kind == "create_calendar_event":
            r = repo.read_event(conn, hid, target["event_id"])
            actual = r and {
                "member_id": r["member_id"], "title": r["title"], "start": fmt(r["start_min"]), "end": fmt(r["end_min"]),
                "location": r["location"], "kind": r["kind"], "responsibility_id": r["responsibility_id"],
                "reminder_min": r["reminder_min"], "status": r["status"],
            }
        elif kind == "notify_member":
            r = conn.execute("SELECT member_id, status, message FROM notifications WHERE action_id=? ORDER BY created_at DESC",
                             (row["id"],)).fetchone()
            expected = {"member_id": target["member_id"], "status": "delivered", "message": proposed["message"]}
            actual = r and {"member_id": r["member_id"], "status": r["status"], "message": r["message"]}
        verified = row["status"] == "applied" and actual == expected
        return CheckOut(seq=row["seq"], type=kind, summary=row["summary"], expected=expected, actual=actual, verified=verified)

    def get_audit_log(self, disruption_id: str, actor: str = "agent") -> AuditOut:
        with closing(self.settings.db_path) as conn:
            d = self._disruption_row(conn, disruption_id)
            plan_ids = [r["id"] for r in conn.execute("SELECT id FROM plans WHERE disruption_id=?", (disruption_id,))]
            marks = ",".join("?" * len(plan_ids)) or "''"
            rows = conn.execute(
                f"SELECT * FROM audit_events WHERE disruption_id=? OR plan_id IN ({marks}) ORDER BY id",
                (disruption_id, *plan_ids),
            ).fetchall()
            applied = conn.execute(
                f"SELECT COUNT(*) FROM actions WHERE status='applied' AND plan_id IN ({marks})", plan_ids
            ).fetchone()[0]
            failed = conn.execute(
                f"SELECT COUNT(*) FROM actions WHERE status='failed' AND plan_id IN ({marks})", plan_ids
            ).fetchone()[0]
        events = [
            AuditEventOut(ts=r["ts"], actor=r["actor"], tool=r["tool"], result=r["result"], error_code=r["error_code"],
                          plan_id=r["plan_id"], input=json.loads(r["input_json"]), output=json.loads(r["output_json"]),
                          duration_ms=r["duration_ms"])
            for r in rows
        ]
        tools: dict[str, int] = {}
        for e in events:
            tools[e.tool] = tools.get(e.tool, 0) + 1
        summary = {
            "household_id": d["household_id"],
            "disruption_status": d["status"],
            "tool_calls": tools,
            "plans": len(plan_ids),
            "approvals": sum(1 for e in events if e.tool == "approve_recovery_plan" and e.result == "ok"),
            "writes_applied": applied,
            "writes_failed": failed,
            "verifications": sum(1 for e in events if e.tool == "verify_recovery"),
            "errors": [f"{e.tool}:{e.error_code}" for e in events if e.result == "error"],
        }
        with self._audited("get_audit_log", actor, {"disruption_id": disruption_id},
                           disruption_id=disruption_id, household_id=d["household_id"]) as rec:
            rec["output"] = {"events": len(events)}
        return AuditOut(disruption_id=disruption_id, events=events, summary=summary)

    def report_availability_change(self, household_id: str, member_id: str, available: bool,
                                   reason: str = "", actor: str = "agent") -> AvailabilityOut:
        inputs = {"household_id": household_id, "member_id": member_id, "available": available,
                  "reason_length": len(reason or "")}
        with self._audited("report_availability_change", actor, inputs, household_id=household_id) as rec:
            with closing(self.settings.db_path) as conn:
                snap = self._snapshot(conn, household_id)
                if not snap.has_member(member_id):
                    raise HdrError("INVALID_MEMBER", f"{member_id!r} is not a household member")
                member = snap.member(member_id)
                if not member.is_adult:
                    raise HdrError("INVALID_MEMBER", "availability can only be reported for adults")
                with transaction(conn):
                    member.available = available
                    repo.update_member(conn, household_id, member)
                    version = repo.bump_version(conn, household_id)
                    invalidated = self._invalidate_open_plans(conn, household_id, "stale")
            rec["output"] = {"state_version": version, "invalidated": invalidated}
            return AvailabilityOut(household_id=household_id, member_id=member_id, available=available,
                                   state_version=version, invalidated_plans=invalidated)


def settings_from_env(env: dict[str, str]) -> Settings:
    import secrets

    secret = env.get("HDR_APPROVAL_SECRET")
    return Settings(
        db_path=env.get("HDR_DB_PATH", str(Path("var") / "hdr.sqlite3")),
        approval_secret=secret.encode() if secret else secrets.token_bytes(32),
        approval_ttl_seconds=int(env.get("HDR_APPROVAL_TTL_SECONDS", "900")),
        fixture_path=env.get("HDR_FIXTURE", str(DEMO_FIXTURE)),
    )
