"""Everything on one port, for the hosted demo: the MCP server (/mcp), the agent API (/api)
and the built web simulator (/).

The agent still reaches the MCP server over Streamable HTTP (on this same port), exactly as
it does when the three run as separate processes. Each browser session gets its own copy of
the demo household, so visitors do not change each other's morning; MCP clients that connect
from outside work on the shared `hh-demo` household.

    cd apps/simulator && HDR_STATIC_EXPORT=1 NEXT_PUBLIC_HDR_API=same-origin npm run build
    HDR_STATIC_DIR=apps/simulator/out uv run python -m hdr.hosted      # http://127.0.0.1:7860

Local development keeps three processes (scripts/dev.sh). This is what the container runs.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any, Callable

import uvicorn
from starlette.responses import PlainTextResponse
from starlette.staticfiles import StaticFiles

from hdr.envfile import load_env_file
from hdr.mcp_server.app import create_app, public_hosts
from hdr.orchestrator.api import agent_from_env, create_api
from hdr.service.recovery import RecoveryService, settings_from_env


class PathRouter:
    """/api/* goes to the agent API; /mcp, /admin/* and /healthz to the MCP server; everything
    else to the static simulator. Lifespan events go to the MCP app, which runs its session
    manager there (the agent API has no startup work)."""

    def __init__(self, mcp_app: Any, api_app: Any, static_app: Any | None = None):
        self.mcp_app, self.api_app, self.static_app = mcp_app, api_app, static_app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "lifespan":
            await self.mcp_app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith("/api/"):
            await self.api_app(scope, receive, send)
        elif path in ("/mcp", "/healthz") or path.startswith(("/mcp/", "/admin/")):
            await self.mcp_app(scope, receive, send)
        elif self.static_app is not None:
            await self.static_app(scope, receive, send)
        else:
            await PlainTextResponse("Not found", status_code=404)(scope, receive, send)


def build(env: dict[str, str], port: int) -> PathRouter:
    # The admin routes (reset, fault injection, demo notice) stay behind a token that only
    # this process knows unless one is configured.
    admin_token = env.get("HDR_ADMIN_TOKEN") or secrets.token_hex(16)
    service = RecoveryService(settings_from_env(env))
    service.reset(env.get("HDR_VARIANT") or None)  # the shared household for outside MCP clients
    base = f"http://127.0.0.1:{port}"
    mcp_app = create_app(service, host="127.0.0.1", port=port, admin_token=admin_token,
                         extra_hosts=public_hosts(env))
    api_app = create_api(f"{base}/mcp", base, admin_token, agent_from_env(f"{base}/mcp", env),
                         household_per_session=True)
    static_dir = env.get("HDR_STATIC_DIR")
    static = StaticFiles(directory=static_dir, html=True) if static_dir and Path(static_dir).is_dir() else None
    return PathRouter(mcp_app, api_app, static)


def main() -> None:
    load_env_file()
    env = dict(os.environ)
    port = int(env.get("PORT") or env.get("HDR_PORT") or 7860)
    uvicorn.run(build(env, port), host=env.get("HDR_HOST", "127.0.0.1"), port=port,
                log_level=env.get("HDR_LOG_LEVEL", "info"))


if __name__ == "__main__":
    main()
