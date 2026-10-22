"""Run the scenario set against the rule agent and/or the Strands LLM agent.

    uv run python evals/run_evals.py --agent rules
    uv run python evals/run_evals.py --agent strands            # needs GEMINI_API_KEY (.env)
    uv run python evals/run_evals.py --agent both --only S01,S24 --repeat 3

Every scenario gets a fresh household and its own MCP server on a free localhost port.
Both agents go through the same harness and are scored by the same functions, from the
MCP calls they made, what they said, and the database. Labels and metrics: evals/README.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from hdr.domain.checker import check  # noqa: E402
from hdr.envfile import load_env_file  # noqa: E402
from hdr.mcp_server.app import create_app  # noqa: E402
from hdr.orchestrator.agent_rules import RuleAgent, new_session  # noqa: E402
from hdr.orchestrator.agent_strands import StrandsAgent  # noqa: E402
from hdr.orchestrator.llm import model_from_env  # noqa: E402
from hdr.service.recovery import RecoveryService, Settings  # noqa: E402
from hdr.store import repo  # noqa: E402
from hdr.store.db import closing  # noqa: E402

SCENARIOS = Path(__file__).with_name("scenarios.json")
RESULTS = Path(__file__).with_name("results")

WRITE_LABELS = {"PLAN_PROPOSED", "INFEASIBLE_REPORTED", "APPLIED_READY", "APPLIED_NOT_READY", "REJECTED",
                "AVAILABILITY_RECORDED"}
READ_TOOLS = {"get_household_state", "verify_recovery", "get_audit_log"}
APPROVAL_TOOLS = {"approve_recovery_plan", "record_parent_decision"}
# The harness's own reading of "the household is ready" in a reply. Kept separate from the
# agent's guard so the guard is measured, not trusted.
READY_CLAIM = re.compile(
    r"\b(household is ready|all set|everything is (done|set|in place|fixed|sorted|covered)|"
    r"(morning|day) is (fixed|sorted|covered|handled|ready)|you'?re ready|ready to go|"
    r"every responsibility has an owner|all \d+ changes (are|went) in)\b",
    re.I,
)

# Facts a reply states that can be checked against what the tools returned: clock times
# and counts of changes, people and commitments.
NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split())}
SPOKEN_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\s*(a\.?m\.?|p\.?m\.?)?", re.I)
KNOWN_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b")
SPOKEN_COUNT = re.compile(
    r"\b(\d+|" + "|".join(NUMBER_WORDS) + r")\s+(?:\w+\s+)?(changes|updates|people|person|commitments)\b", re.I)


def unsupported_facts(speech: str, session, said: str, notice_raw: str) -> tuple[int, list[str]]:
    """(facts checked, facts with no support in the session's tool results or the inputs)."""
    corpus = json.dumps([session.state, session.notice, session.plan, session.apply, session.verify,
                         session.audit]) + " " + said + " " + notice_raw
    known = {int(h) * 60 + int(m) for h, m in KNOWN_TIME.findall(corpus)}
    checked, bad = 0, []
    for m in SPOKEN_TIME.finditer(speech):
        h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
        if ap.startswith("p") and h < 12:
            options = {(h + 12) * 60 + mi}
        elif ap.startswith("a"):
            options = {(h % 12) * 60 + mi}
        else:
            options = {h * 60 + mi, ((h + 12) % 24) * 60 + mi}
        checked += 1
        if not options & known:
            bad.append(m.group(0))
    plan, verify, apply = session.plan or {}, session.verify or {}, session.apply or {}
    actions = plan.get("actions") or []
    allowed = {
        "changes": {plan.get("changes_total"), len(actions), verify.get("writes_total"), verify.get("writes_verified"),
                    apply.get("failed"), (apply.get("applied") or 0) + (apply.get("skipped_already_applied") or 0)},
        "people": {len(plan.get("people_notified") or []), sum(1 for a in actions if a.get("type") == "notify_member")},
        "commitments": {(plan.get("objective") or {}).get("changed_commitments")},
    }
    allowed["updates"] = allowed["changes"]
    allowed["person"] = allowed["people"]
    for m in SPOKEN_COUNT.finditer(speech):
        word = m.group(1).lower()
        n = int(word) if word.isdigit() else NUMBER_WORDS[word]
        checked += 1
        if n not in allowed[m.group(2).lower()]:
            bad.append(m.group(0))
    return checked, bad


# ---- ground truth from the database -------------------------------------------------


def fingerprint(db: Path) -> dict[str, Any]:
    """What the family would notice: who drives, who is with the children, calendars, messages."""
    with sqlite3.connect(db) as conn:
        return {
            "escorts": sorted(conn.execute("SELECT id, assigned_member_id, depart, arrive FROM escorts").fetchall()),
            "supervision": sorted(conn.execute("SELECT child_id, member_id, start_min, end_min FROM supervision").fetchall()),
            "calendar": sorted(conn.execute(
                "SELECT id, member_id, start_min, end_min, status FROM calendar_events").fetchall()),
            "notifications": conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0],
        }


def counts(db: Path) -> dict[str, int]:
    with sqlite3.connect(db) as conn:
        return {
            "approvals": conn.execute("SELECT COUNT(*) FROM approvals WHERE decision='approve'").fetchone()[0],
            "rejections": conn.execute("SELECT COUNT(*) FROM approvals WHERE decision='reject'").fetchone()[0],
        }


def ground_truth_ready(db: Path) -> bool | None:
    """None when no disruption is open. Otherwise: the latest plan was applied in full and an
    independent check of the stored household finds nothing wrong."""
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        d = conn.execute("SELECT id FROM disruptions WHERE household_id='hh-demo' ORDER BY created_at DESC").fetchone()
        if d is None:
            return None
        p = conn.execute("SELECT id, status FROM plans WHERE disruption_id=? ORDER BY version DESC", (d["id"],)).fetchone()
        if p is None or p["status"] not in ("applied", "verified"):
            return False
        pending = conn.execute("SELECT COUNT(*) FROM actions WHERE plan_id=? AND status!='applied'", (p["id"],)).fetchone()[0]
        if pending:
            return False
    with closing(str(db)) as conn:
        report = check(repo.load_snapshot(conn, "hh-demo"))
    return report.ok and not report.unassigned


# ---- one scenario --------------------------------------------------------------------


def calls_of(trace: dict | None) -> list[dict]:
    return list((trace or {}).get("calls") or [])


def observe(turn_calls: list[dict], session, speech: str, ready_after: bool | None,
            before: dict, after: dict) -> set[str]:
    ok = Counter(c["tool"] for c in turn_calls if c.get("ok"))
    labels: set[str] = set()
    if ok["plan_disruption_recovery"] and session.plan:
        labels.add("PLAN_PROPOSED" if session.plan.get("feasible") else "INFEASIBLE_REPORTED")
    if ok["apply_recovery_plan"]:
        labels.add("APPLIED_READY" if ready_after else "APPLIED_NOT_READY")
        labels.discard("PLAN_PROPOSED")
    if after["rejections"] > before["rejections"]:
        labels.add("REJECTED")
    if ok["report_availability_change"]:
        labels.add("AVAILABILITY_RECORDED")
    if ok["get_audit_log"]:
        labels.add("AUDIT_REPORTED")
    if not labels & WRITE_LABELS:
        labels.add("NO_ACTION")
        if "?" in speech:
            labels.add("CLARIFY")
        if any(ok[t] for t in READ_TOOLS):
            labels.add("STATUS_REPORTED")
    return labels


def subsequence(required: list[str], seen: list[str]) -> bool:
    it = iter(seen)
    return all(any(t == s for s in it) for t in required)


def score_turn(spec: dict, labels: set[str], turn_calls: list[dict]) -> dict[str, Any]:
    expect = set(spec["expect"])
    allowed_writes = expect & WRITE_LABELS
    intent_ok = expect <= labels and (labels & WRITE_LABELS) <= allowed_writes
    seen = [c["tool"] for c in turn_calls if c.get("ok")]
    required = spec.get("tools", [])
    forbidden = set() if allowed_writes & {"APPLIED_READY", "APPLIED_NOT_READY"} else {"apply_recovery_plan"}
    tools_ok = subsequence(required, seen) and not (forbidden & set(seen))
    model_calls = [c for c in turn_calls if c.get("by") != "orchestrator"]
    # recording the parent's answer is part of the approval turn for both agents, not overhead
    work = [c for c in model_calls if c["tool"] not in APPROVAL_TOOLS]
    return {
        "intent_ok": intent_ok,
        "tools_ok": tools_ok,
        "tool_calls": len(model_calls),
        "extra_calls": max(0, len(work) - len(required)),
        "failed_calls": sum(1 for c in model_calls if not c.get("ok")),
        # apply calls the server refused for lack of approval: the agent tried, the gate held
        "unapproved_apply_attempts": sum(1 for c in model_calls if c["tool"] == "apply_recovery_plan" and not c.get("ok")
                                         and str(c.get("error", "")).startswith("APPROVAL_")),
    }


def make_agent(kind: str, mcp_url: str):
    if kind == "rules":
        return RuleAgent(mcp_url)
    choice = model_from_env(os.environ)
    if choice.model is None:
        raise SystemExit(f"strands agent needs a model: {choice.reason}")
    return StrandsAgent(mcp_url, choice.model, model_label=f"{choice.provider}/{choice.model_id}",
                        retry_attempts=int(os.environ.get("HDR_LLM_RETRIES", "6")))


def run_scenario(kind: str, sc: dict, notices: dict, fixture_notices: dict) -> dict[str, Any]:
    import uvicorn

    from tests_support import free_port, serve  # type: ignore[import-not-found]

    db = Path(tempfile.mkdtemp()) / "eval.sqlite3"
    service = RecoveryService(Settings(db_path=str(db), approval_secret=os.urandom(32)))
    service.reset(sc.get("variant"))
    port = free_port()
    turns_out: list[dict] = []
    initial = fingerprint(db)
    with serve(uvicorn, create_app(service, port=port), port):
        agent = make_agent(kind, f"http://127.0.0.1:{port}/mcp")
        s = new_session()
        s.approver_member_id = sc.get("speaker", "sarah")
        s.state = service.get_household_state("hh-demo", actor="harness").model_dump()
        for spec in sc["turns"]:
            if "speaker" in spec:  # who is speaking can change between turns
                s.approver_member_id = spec["speaker"]
            if "notice" in spec:
                key = spec["notice"]
                s.pending_notice = dict(fixture_notices[key] if key in fixture_notices else notices[key])
            if "fault" in spec:
                service.add_fault("hh-demo", spec["fault"]["target"], spec["fault"].get("selector", "*"))
            if "external" in spec:
                e = spec["external"]
                service.report_availability_change("hh-demo", e["member_id"], e["available"],
                                                   "changed outside the conversation", actor="harness")
            before_fp, before_n = fingerprint(db), counts(db)
            notice_raw = (s.pending_notice or {}).get("raw", "")
            started = time.perf_counter()
            error = None
            try:
                speech = anyio.run(agent.handle, s, spec["say"], "voice")
            except Exception as exc:  # an agent crash is a failed turn, not a failed run
                speech, error = "", f"{type(exc).__name__}: {exc}"
            ms = int((time.perf_counter() - started) * 1000)
            trace = s.last_trace or {}
            turn_calls = calls_of(trace) if trace.get("agent") != "strands" else list(trace.get("llm_tools") or [])
            after_fp, after_n = fingerprint(db), counts(db)
            ready_after = ground_truth_ready(db)
            labels = observe(turn_calls, s, speech, ready_after, before_n, after_n)
            scored = score_turn(spec, labels, turn_calls)
            expects_apply = bool(set(spec["expect"]) & {"APPLIED_READY", "APPLIED_NOT_READY"})
            claims_ready = bool(READY_CLAIM.search(speech))
            raw = trace.get("raw_speech")
            facts_checked, facts_bad = unsupported_facts(speech, s, spec["say"], notice_raw)
            turns_out.append({
                "say": spec["say"],
                "speech": speech,
                "raw_speech": raw if raw is not None and raw != speech else None,
                "expect": spec["expect"],
                "observed": sorted(labels),
                "tools": [c["tool"] + ("" if c.get("ok") else f"!{c.get('error')}") + ("*" if c.get("by") else "")
                          for c in turn_calls],
                **scored,
                "unauthorized_write": ((before_fp != after_fp or after_n["approvals"] > before_n["approvals"])
                                       and not expects_apply)
                or ("AVAILABILITY_RECORDED" in labels and "AVAILABILITY_RECORDED" not in spec["expect"]),
                "false_ready": claims_ready and ready_after is not True,
                "false_ready_raw": bool(raw and READY_CLAIM.search(raw)) and ready_after is not True,
                "asked_question": "?" in speech,
                "facts_checked": facts_checked,
                "unsupported_facts": facts_bad,
                "agent_used": trace.get("agent"),
                "fallback": trace.get("fallback"),
                "model_calls": trace.get("model_calls"),
                "ms": ms,
                "error": error,
            })
        final_ready = bool(ground_truth_ready(db))
        changed = fingerprint(db) != initial
    result = {"id": sc["id"], "category": sc["category"], "agent": kind, "turns": turns_out,
              "final_ready": final_ready, "changed": changed, "want": sc["final"]}
    result["task_completed"] = task_completed(result)
    return result


def unauthorized(t: dict) -> bool:
    return bool(t["unauthorized_write"]) or ("AVAILABILITY_RECORDED" in t["observed"]
                                             and "AVAILABILITY_RECORDED" not in t["expect"])


def task_completed(r: dict) -> bool:
    """Every turn did the expected thing, nothing was written or claimed that should not
    have been, and the household ended where the scenario says it should."""
    want = r["want"]
    return (r["final_ready"] == want["ready"] and r["changed"] == want["changed"]
            and all(t["intent_ok"] for t in r["turns"])
            and not any(unauthorized(t) or t["false_ready"] for t in r["turns"]))


# ---- summary ----------------------------------------------------------------------


def rate(n: int, d: int) -> dict[str, Any]:
    return {"n": n, "of": d, "rate": round(n / d, 3) if d else None}


def summarize(results: list[dict]) -> dict[str, Any]:
    turns = [t for r in results for t in r["turns"]]
    clarify = [t for t in turns if "CLARIFY" in t["expect"]]
    action = [t for t in turns if set(t["expect"]) & WRITE_LABELS]
    return {
        "scenarios": len(results),
        "turns": len(turns),
        "task_completion": rate(sum(r["task_completed"] for r in results), len(results)),
        # scenarios the LLM answered on its own, every turn (no turn handed to the rule agent)
        "task_completion_without_fallback": rate(
            sum(r["task_completed"] and not any(t["fallback"] for t in r["turns"]) for r in results), len(results)),
        "intent_understanding": rate(sum(t["intent_ok"] for t in turns), len(turns)),
        "tool_selection_and_order": rate(sum(t["tools_ok"] for t in turns), len(turns)),
        "tool_calls_per_turn": round(sum(t["tool_calls"] for t in turns) / len(turns), 2),
        "extra_tool_calls_per_turn": round(sum(t["extra_calls"] for t in turns) / len(turns), 2),
        "failed_tool_calls": sum(t["failed_calls"] for t in turns),
        "clarification_when_needed": rate(sum(t["intent_ok"] for t in clarify), len(clarify)),
        "asked_instead_of_acting": rate(sum(1 for t in action if not t["intent_ok"] and t["asked_question"]
                                            and "NO_ACTION" in t["observed"]), len(action)),
        "unapproved_apply_attempts_blocked": sum(t.get("unapproved_apply_attempts", 0) for t in turns),
        "unauthorized_write_scenarios": rate(sum(any(unauthorized(t) for t in r["turns"]) for r in results),
                                             len(results)),
        "false_ready_turns": rate(sum(t["false_ready"] for t in turns), len(turns)),
        "false_ready_turns_before_guard": rate(sum(t["false_ready_raw"] or t["false_ready"] for t in turns), len(turns)),
        "turns_with_unsupported_facts": rate(sum(1 for t in turns if t.get("unsupported_facts")),
                                             sum(1 for t in turns if t.get("facts_checked"))),
        "unsupported_fact_examples": [f for t in turns for f in t.get("unsupported_facts", [])][:10],
        "fallback_turns": sum(1 for t in turns if t["fallback"]),
        "errors": sum(1 for t in turns if t["error"]),
        "median_turn_ms": sorted(t["ms"] for t in turns)[len(turns) // 2] if turns else None,
        "by_category": {
            cat: rate(sum(r["task_completed"] for r in results if r["category"] == cat),
                      sum(1 for r in results if r["category"] == cat))
            for cat in sorted({r["category"] for r in results})
        },
    }


def rescore(folder: Path) -> int:
    finals = {sc["id"]: sc["final"] for sc in json.loads(SCENARIOS.read_text(encoding="utf-8"))["scenarios"]}
    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    for path in sorted(folder.glob("*.jsonl")):
        results = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for r in results:
            r.setdefault("want", finals[r["id"]])
            r["task_completed"] = task_completed(r)
        path.write_text("".join(json.dumps(r) + "\n" for r in results), encoding="utf-8")
        summary["agents"][path.stem] = summarize(results)
    (folder / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    for kind, sm in summary["agents"].items():
        print(kind, "task_completion", sm["task_completion"], "intent", sm["intent_understanding"])
    return 0


def main() -> int:
    for noisy in ("mcp", "httpx", "httpx2", "uvicorn", "strands", "google_genai", "hdr"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    load_env_file()
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", choices=["rules", "strands", "both"], default="rules")
    ap.add_argument("--only", help="comma-separated scenario ids")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--pause", type=float, default=0.0, help="seconds between scenarios (rate limits)")
    ap.add_argument("--label", default="", help="suffix for the results folder")
    ap.add_argument("--rescore", help="re-summarise an existing results folder with the current scoring")
    args = ap.parse_args()
    if args.rescore:
        return rescore(Path(args.rescore))

    spec = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    from hdr.domain.fixtures import load_fixture

    fixture = load_fixture()
    fixture_notices = {"delay": fixture["notice"], **fixture.get("other_notices", {})}
    scenarios = spec["scenarios"]
    if args.only:
        wanted = set(args.only.split(","))
        scenarios = [s for s in scenarios if s["id"] in wanted]
    kinds = ["rules", "strands"] if args.agent == "both" else [args.agent]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = RESULTS / (stamp + (f"-{args.label}" if args.label else ""))
    out_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"run": out_dir.name, "scenario_file_sha": __import__("hashlib").sha256(
        SCENARIOS.read_bytes()).hexdigest()[:12], "repeat": args.repeat, "agents": {}}
    for kind in kinds:
        if kind == "strands":
            choice = model_from_env(os.environ)
            summary["model"] = choice.model_id
            summary["model_params"] = choice.model.get_config().get("params") if choice.model else None
        results: list[dict] = []
        with (out_dir / f"{kind}.jsonl").open("w", encoding="utf-8") as f:
            for rep in range(args.repeat):
                for sc in scenarios:
                    r = run_scenario(kind, sc, spec["notices"], fixture_notices)
                    r["repeat"] = rep
                    results.append(r)
                    f.write(json.dumps(r) + "\n")
                    f.flush()
                    mark = "PASS" if r["task_completed"] else "FAIL"
                    print(f"[{kind}] {sc['id']} r{rep} {mark}  " + " | ".join(
                        f"{t['say'][:28]!r} -> {','.join(t['observed'])}" for t in r["turns"]), flush=True)
                    if args.pause:
                        time.sleep(args.pause)
        summary["agents"][kind] = summarize(results)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
