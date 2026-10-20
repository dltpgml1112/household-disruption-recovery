"""Print the README comparison table from two result folders.

    uv run python evals/report.py evals/results/<rules run> evals/results/<strands run>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def load(folder: str) -> tuple[str, dict, dict]:
    summary = json.loads((Path(folder) / "summary.json").read_text(encoding="utf-8"))
    (kind, agent), = summary["agents"].items()
    return kind, agent, summary


def frac(x: dict) -> str:
    return f"{x['n']}/{x['of']}" if x and x.get("of") else "–"


def main(rules_dir: str, llm_dir: str) -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy code page
    _, r, _ = load(rules_dir)
    _, s, meta = load(llm_dir)
    rows = [
        ("Scenarios completed (every turn right, final state right)", frac(r["task_completion"]),
         frac(s["task_completion"])),
        ("Turns with the expected behaviour", frac(r["intent_understanding"]), frac(s["intent_understanding"])),
        ("Turns with the expected tools, in order", frac(r["tool_selection_and_order"]),
         frac(s["tool_selection_and_order"])),
        ("Tool calls per turn (of which beyond the expected tools)",
         f"{r['tool_calls_per_turn']} ({r['extra_tool_calls_per_turn']})",
         f"{s['tool_calls_per_turn']} ({s['extra_tool_calls_per_turn']})"),
        ("Clarifying question when one was needed", frac(r["clarification_when_needed"]),
         frac(s["clarification_when_needed"])),
        ("Scenarios with an unauthorised write", frac(r["unauthorized_write_scenarios"]),
         frac(s["unauthorized_write_scenarios"])),
        ("Apply attempts the server refused for missing approval", str(r["unapproved_apply_attempts_blocked"]),
         str(s["unapproved_apply_attempts_blocked"])),
        ("Replies claiming \"ready\" when it was not (before the guard)",
         f"{frac(r['false_ready_turns'])} ({r['false_ready_turns_before_guard']['n']})",
         f"{frac(s['false_ready_turns'])} ({s['false_ready_turns_before_guard']['n']})"),
        ("Replies with a time or count not in the tool results", frac(r["turns_with_unsupported_facts"]),
         frac(s["turns_with_unsupported_facts"])),
        ("Turns handed to the rule agent", "–", str(s["fallback_turns"])),
        ("Median time per turn", f"{r['median_turn_ms'] / 1000:.1f} s", f"{s['median_turn_ms'] / 1000:.1f} s"),
    ]
    model = meta.get("model", "LLM")
    print(f"| | Rule agent | Strands + {model} |")
    print("|---|---|---|")
    for name, a, b in rows:
        print(f"| {name} | {a} | {b} |")
    print()
    print("| Category | Rule agent | Strands |")
    print("|---|---|---|")
    for cat in r["by_category"]:
        print(f"| {cat.replace('_', ' ')} | {frac(r['by_category'][cat])} | {frac(s['by_category'].get(cat))} |")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
