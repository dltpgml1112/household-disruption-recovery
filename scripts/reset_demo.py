"""Reset the demo household on a running MCP server (normal-day state).

    uv run python scripts/reset_demo.py                         # uses HDR_ADMIN_TOKEN from the environment
    uv run python scripts/reset_demo.py --variant linda-unavailable
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=os.environ.get("HDR_ADMIN_URL", "http://127.0.0.1:8765"))
    ap.add_argument("--variant")
    args = ap.parse_args()
    token = os.environ.get("HDR_ADMIN_TOKEN")
    if not token:
        print("HDR_ADMIN_TOKEN is not set (see .env.example)", file=sys.stderr)
        return 2
    headers = {"x-admin-token": token}
    httpx.delete(f"{args.server}/admin/faults", headers=headers, timeout=10).raise_for_status()
    r = httpx.post(f"{args.server}/admin/reset", json={"variant": args.variant}, headers=headers, timeout=10)
    r.raise_for_status()
    print(r.json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
