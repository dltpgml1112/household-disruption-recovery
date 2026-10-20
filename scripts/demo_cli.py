"""Run the killer scenario end to end over real Streamable HTTP MCP calls.

    uv run python scripts/demo_cli.py --fresh                    # starts its own MCP server
    uv run python scripts/demo_cli.py --fresh --fault notify:linda
    uv run python scripts/demo_cli.py --fresh --variant linda-unavailable
    uv run python scripts/demo_cli.py --url http://127.0.0.1:8765/mcp   # use a running server
    uv run python scripts/demo_cli.py --fresh --agent strands     # LLM agent (GEMINI_API_KEY in .env)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path

import anyio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hdr.envfile import load_env_file  # noqa: E402
from hdr.mcp_server.app import create_app  # noqa: E402
from hdr.orchestrator.agent_rules import RuleAgent, new_session  # noqa: E402
from hdr.orchestrator.agent_strands import StrandsAgent  # noqa: E402
from hdr.orchestrator.llm import model_from_env  # noqa: E402
from hdr.service.recovery import RecoveryService, Settings  # noqa: E402


def print_turn(label: str, utterance: str, speech: str, session) -> None:
    print(f"\n=== {label} ===")
    print(f"PARENT : {utterance}")
    print(f"AGENT  : {speech}")
    t = session.last_trace or {}
    who = t.get("agent")
    if who == "strands":
        who += f" ({t.get('model')}, {t.get('model_calls')} model calls)"
    elif t.get("fallback"):
        who += f" (fallback: {t['fallback'][:80]})"
    print(f"MCP    : protocol {t.get('protocol_version')} · {len(t.get('tools', []))} tools listed · agent {who}")
    for w in t.get("wire", []):
        name = w.get("method") or "?"
        if w.get("tool"):
            name += f" {w['tool']}"
        hdr = w.get("protocol_header") or "-"
        print(f"   {w.get('at_ms', 0):>5} ms  HTTP {w.get('status')}  {name:<45} MCP-Protocol-Version: {hdr}")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy code page
    for noisy in ("mcp", "httpx", "httpx2", "uvicorn", "strands", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    load_env_file()
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="existing MCP endpoint; default starts a local server")
    ap.add_argument("--fresh", action="store_true", help="reset the demo household first (local server only)")
    ap.add_argument("--variant", help="fixture variant, e.g. linda-unavailable")
    ap.add_argument("--fault", help="target:selector, e.g. notify:linda (one failure, local server only)")
    ap.add_argument("--agent", choices=["rules", "strands"], default="rules")
    args = ap.parse_args()

    if args.url:
        return anyio.run(run, args.url, None, args.agent)

    import uvicorn

    from tests_support import free_port, serve  # type: ignore[import-not-found]

    db = Path(tempfile.mkdtemp()) / "demo.sqlite3"
    service = RecoveryService(Settings(db_path=str(db), approval_secret=os.urandom(32)))
    service.reset(args.variant)
    if args.fault:
        target, _, selector = args.fault.partition(":")
        service.add_fault("hh-demo", target, selector or "*", times=1)
    port = free_port()
    with serve(uvicorn, create_app(service, port=port), port):
        return anyio.run(run, f"http://127.0.0.1:{port}/mcp", service, args.agent)


def make_agent(kind: str, url: str):
    if kind == "rules":
        return RuleAgent(url)
    choice = model_from_env(os.environ)
    return StrandsAgent(url, choice.model, model_label=f"{choice.provider}/{choice.model_id}",
                        no_model_reason=choice.reason)


async def run(url: str, service, kind: str = "rules") -> int:
    agent, s = make_agent(kind, url), new_session()
    s.pending_notice = {
        "raw": "Due to severe weather, school will operate on a two-hour delay. "
               "Doors open at 10:05 AM and classes begin at 10:15 AM.",
        "source": "Maple Elementary text alert",
        "received_at": "2026-10-15T06:15:00-04:00",
    }
    print("6:15 AM. Text alert from Maple Elementary:")
    print(f'  "{s.pending_notice["raw"]}"')
    script = ["Alexa, fix our morning.", "Yes, do it."]
    for i, utterance in enumerate(script, start=1):
        speech = await agent.handle(s, utterance, "voice")
        print_turn(f"turn {i}", utterance, speech, s)
        if s.plan and not s.plan.get("feasible"):
            break
    if s.verify and not s.verify["ready"]:
        speech = await agent.handle(s, "Try again.", "voice")
        print_turn("retry", "Try again.", speech, s)

    if s.plan:
        print("\n=== plan ===")
        for line in s.plan["rationale"]:
            print("  " + line)
        for a in s.plan["actions"]:
            print(f"  {a['seq']:>2}. [{a['type']}] {a['summary']}")
    if s.verify:
        print("\n=== read-back verification ===")
        for c in s.verify["checks"]:
            print(f"  {'OK ' if c['verified'] else 'BAD'} {c['seq']:>2}. {c['summary']}")
        print(f"\n{s.verify['summary']}")
        print(f"ready={str(s.verify['ready']).lower()} unassigned={len(s.verify['unassigned'])} "
              f"violations={len(s.verify['violations'])}")
        return 0 if s.verify["ready"] else 2
    if s.plan and not s.plan["feasible"]:
        print("\nNO FEASIBLE PLAN — nothing was written. Unresolved:")
        for u in s.plan["unresolved"]:
            print(f"  - {u['detail']} | blockers: {'; '.join(u['blockers'])}")
        return 3
    return 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
