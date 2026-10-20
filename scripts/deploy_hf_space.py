"""Deploy this repository to a Hugging Face Space (Docker) as the hosted demo.

    HF_TOKEN=... uv run --with huggingface_hub python scripts/deploy_hf_space.py <owner>/<space> [--repo-url URL]

Uploads the tracked files, adds the Space's README header (sdk: docker, port 7860), and
stores GEMINI_API_KEY (from the environment or .env) as a Space secret if it is set. The
Space builds the Dockerfile: MCP server at /mcp, agent API at /api, simulator at /.
Free CPU Spaces sleep after two days without visits; .github/workflows/keepalive.yml pings it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hdr.envfile import load_env_file  # noqa: E402

HEADER = """---
title: Household Disruption Recovery
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Alexa+ simulation backed by a self-hosted MCP server
---

Hosted demo of Household Disruption Recovery: a web simulation of an Alexa+ add-on (unofficial)
and the self-hosted MCP server behind it (spec 2025-11-25, Streamable HTTP, endpoint `/mcp`).
The family in the demo is made up. What you type is sent to Google's Gemini API.

Code, tests and documentation: {repo_url}
"""


def space_url(repo_id: str) -> str:
    owner, name = repo_id.split("/", 1)
    slug = f"{owner}-{name}".lower().replace("_", "-").replace(".", "-")
    return f"https://{slug}.hf.space"


def main() -> int:
    from huggingface_hub import HfApi

    load_env_file()
    args = sys.argv[1:]
    if not args or "/" not in args[0]:
        print(__doc__)
        return 2
    repo_id = args[0]
    repo_url = args[args.index("--repo-url") + 1] if "--repo-url" in args else "(see the project page)"
    token = os.environ.get("HF_TOKEN")
    if not token:
        print("HF_TOKEN is not set")
        return 2

    api = HfApi(token=token)
    api.create_repo(repo_id, repo_type="space", space_sdk="docker", exist_ok=True)
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files"], check=True, capture_output=True,
                             text=True).stdout.split()
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        for rel in tracked:
            if rel.startswith(("evals/results/", "tests/", ".github/")):
                continue  # not needed to run the demo
            dst = stage / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, dst)
        (stage / "README.md").write_text(HEADER.format(repo_url=repo_url), encoding="utf-8")
        api.upload_folder(folder_path=str(stage), repo_id=repo_id, repo_type="space",
                          commit_message="Deploy the hosted demo")
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        api.add_space_secret(repo_id, "GEMINI_API_KEY", key)
    print(f"Space: https://huggingface.co/spaces/{repo_id}")
    print(f"Demo:  {space_url(repo_id)}   MCP: {space_url(repo_id)}/mcp")
    print("model key:", "set as a Space secret" if key else "not set (the rule agent answers every turn)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
