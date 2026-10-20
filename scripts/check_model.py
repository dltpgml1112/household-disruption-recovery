"""Check the configured LLM before running the agent or the evals.

    uv run python scripts/check_model.py           # list Flash models, ping the configured one

Reads GEMINI_API_KEY and HDR_LLM_MODEL from the environment or .env. Prints model names
only, never the key.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hdr.envfile import load_env_file  # noqa: E402
from hdr.orchestrator.llm import DEFAULT_GEMINI_MODEL  # noqa: E402


def main() -> int:
    load_env_file()
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        print("GEMINI_API_KEY is not set (.env or environment)")
        return 2
    from google import genai

    client = genai.Client(api_key=key)
    wanted = os.environ.get("HDR_LLM_MODEL") or DEFAULT_GEMINI_MODEL
    names = sorted(m.name.removeprefix("models/") for m in client.models.list())
    flash = [n for n in names if "flash" in n]
    print(f"{len(names)} models visible; flash models:")
    for n in flash:
        print("  ", n, "<- configured" if n == wanted else "")
    if wanted not in names:
        print(f"configured model {wanted!r} is not in the list")
        return 3
    started = time.perf_counter()
    reply = client.models.generate_content(model=wanted, contents="Reply with the single word: ok")
    print(f"ping {wanted}: {reply.text!r} in {int((time.perf_counter() - started) * 1000)} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
