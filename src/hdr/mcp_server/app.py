"""Self-hosted MCP server (Streamable HTTP) exposing the recovery workflow."""

from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
from typing import Annotated, Any, Callable

import anyio
import pydantic_core
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from hdr.service.recovery import HdrError, RecoveryService, settings_from_env
from hdr.service.schemas import (
    ApplyOut,
    ApprovalOut,
    AuditOut,
    AvailabilityOut,
    HouseholdStateOut,
    NoticeOut,
    PlanOut,
    VerifyOut,
)

SERVER_NAME = "household-disruption-recovery"
INSTRUCTIONS = (
    "Recovers a family's day after a school schedule change. Typical flow: get_household_state, "
    "ingest_school_notice, plan_disruption_recovery, show the plan to a parent, approve_recovery_plan only "
    "after the parent explicitly agrees, apply_recovery_plan with the approval token, then verify_recovery. "
    "Never tell the family the morning is fixed unless verify_recovery returns ready=true."
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
PLANNING = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
# a school notice or an availability change is written without approval (it is a fact, not a
# choice); a closure cancels the school trip, so these are not additive-only
RECORDS_FACTS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
# verification records its result and marks the plan verified; household rows are only read
VERIFIES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

# MCP Apps (extension io.modelcontextprotocol/ui, spec 2026-01-26): the plan and result card as
# an HTML view that hosts render in a sandboxed frame. Alexa+ renders MCP Apps in its webview.
APP_MIME_TYPE = "text/html;profile=mcp-app"
CARD_URI = "ui://household-disruption-recovery/card"
CARD_HTML = (Path(__file__).with_name("ui") / "recovery_card.html").read_text(encoding="utf-8")
CARD_UI = {"ui": {"resourceUri": CARD_URI}}
# Hosts that follow MCP Apps keep app-only tools out of the model's tool list.
APP_ONLY = {"ui": {"visibility": ["app"]}}

TOOL_NAMES = [
    "get_household_state",
    "ingest_school_notice",
    "plan_disruption_recovery",
    "approve_recovery_plan",
    "apply_recovery_plan",
    "verify_recovery",
    "get_audit_log",
    "report_availability_change",
]


def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except HdrError as e:
        raise ToolError(f"{e.code}: {e.message}") from None


def _with_card(out: BaseModel, invoking: str, invoked: str) -> CallToolResult:
    """The text and structured content MCPServer would return for `out`, plus `_meta.ui` on the
    result. The MCP Apps spec links a view on the tool definition; Alexa+'s add-on docs show
    `resourceUri` on the tool result. The server does both."""
    return CallToolResult(
        content=[TextContent(type="text", text=pydantic_core.to_json(out, fallback=str, indent=2).decode())],
        structured_content=out.model_dump(mode="json"),
        meta={"ui": {"resourceUri": CARD_URI, "invoking": invoking, "invoked": invoked}},
    )


def build_mcp(service: RecoveryService) -> MCPServer:
    mcp = MCPServer(
        name=SERVER_NAME,
        title="Household Disruption Recovery",
        description="Plan, approve, apply and verify a family's recovery from a school schedule disruption.",
        instructions=INSTRUCTIONS,
        version="0.1.0",
        log_level=os.environ.get("HDR_MCP_LOG_LEVEL", "WARNING"),  # type: ignore[arg-type]
    )

    async def delay(tool: str) -> None:
        ms = service.tool_delay_ms(tool)
        if ms:
            await anyio.sleep(ms / 1000)

    @mcp.tool(annotations=READ_ONLY)
    async def get_household_state(
        household_id: str, window_start: str | None = None, window_end: str | None = None
    ) -> HouseholdStateOut:
        """Members, permissions, calendar, escort and supervision duties, travel times and rules.
        Optional window_start/window_end (HH:MM) limit the calendar to that time window."""
        await delay("get_household_state")
        return _call(service.get_household_state, household_id, window_start, window_end)

    @mcp.resource(CARD_URI, name="recovery_card", title="Plan and result card",
                  description="The plan or result card as an MCP App view, for hosts that render MCP Apps.",
                  mime_type=APP_MIME_TYPE, meta={"ui": {"prefersBorder": True}})
    def recovery_card() -> str:
        return CARD_HTML

    @mcp.tool(annotations=RECORDS_FACTS)
    async def ingest_school_notice(
        household_id: str, raw_notice: str, source: str, received_at: str,
        clarification: dict[str, str] | None = None,
    ) -> NoticeOut:
        """Parse a school notice into a structured change with a confidence score. A confident
        late start updates the school arrival window and lists the household conflicts it causes.
        Low confidence returns questions instead; pass clarification={doors_open, classes_begin}
        (HH:MM) once a parent confirms the times."""
        await delay("ingest_school_notice")
        return _call(service.ingest_school_notice, household_id, raw_notice, source, received_at, clarification)

    @mcp.tool(annotations=PLANNING, meta=CARD_UI)
    async def plan_disruption_recovery(
        disruption_id: str, household_state_version: int
    ) -> Annotated[CallToolResult, PlanOut]:
        """Compute a recovery plan with the deterministic planner. Does not change household
        commitments. Returns conflicts, proposed actions (before/after), the reasons for each
        choice, and unresolved items when no feasible plan exists."""
        await delay("plan_disruption_recovery")
        out = _call(service.plan_disruption_recovery, disruption_id, household_state_version)
        return _with_card(out, "Planning the morning", "Plan ready" if out.feasible else "No plan covers everyone")

    @mcp.tool(annotations=PLANNING, meta=APP_ONLY)
    async def approve_recovery_plan(
        plan_id: str, plan_version: int, decision: str, approver_member_id: str, channel: str = "voice"
    ) -> ApprovalOut:
        """Record a parent's decision ('approve' or 'reject') on one plan version. Call only after
        the parent explicitly agreed. Returns the approval token needed by apply_recovery_plan."""
        await delay("approve_recovery_plan")
        return _call(service.approve_recovery_plan, plan_id, plan_version, decision, approver_member_id, channel)

    @mcp.tool(annotations=WRITES)
    async def apply_recovery_plan(plan_id: str, approval_token: str, idempotency_key: str) -> ApplyOut:
        """Execute an approved plan: reassign responsibilities, update calendars, send messages.
        Rejects missing, forged, expired or stale approvals. Repeating an idempotency_key returns the
        first result without running anything again; a new key retries only failed actions."""
        await delay("apply_recovery_plan")
        return _call(service.apply_recovery_plan, plan_id, approval_token, idempotency_key)

    @mcp.tool(annotations=VERIFIES, meta=CARD_UI)
    async def verify_recovery(plan_id: str) -> Annotated[CallToolResult, VerifyOut]:
        """Read every write back from storage, compare with the plan, and re-check the whole
        household. ready=true only when all writes match and nothing is unassigned or violated."""
        await delay("verify_recovery")
        out = _call(service.verify_recovery, plan_id)
        return _with_card(out, "Reading every change back", "Household ready" if out.ready else "Not ready")

    @mcp.tool(annotations=READ_ONLY)
    async def get_audit_log(disruption_id: str) -> AuditOut:
        """Every tool call, decision, approval, write and verification for one disruption."""
        await delay("get_audit_log")
        return _call(service.get_audit_log, disruption_id)

    @mcp.tool(annotations=RECORDS_FACTS)
    async def report_availability_change(
        household_id: str, member_id: str, available: bool, reason: str = ""
    ) -> AvailabilityOut:
        """Record that an adult can or cannot help today (e.g. the caregiver called in sick).
        Open plans become stale and must be re-planned."""
        await delay("report_availability_change")
        return _call(service.report_availability_change, household_id, member_id, available, reason)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "server": SERVER_NAME, "tools": TOOL_NAMES})

    return mcp


def add_admin_routes(app: Starlette, service: RecoveryService, admin_token: str | None) -> None:
    """Demo/test controls. Disabled unless HDR_ADMIN_TOKEN is set; never part of the MCP surface."""
    if not admin_token:
        return

    def authorized(request: Request) -> bool:
        given = request.headers.get("x-admin-token", "")
        return hmac.compare_digest(given, admin_token)

    async def reset(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body = await request.json() if await request.body() else {}
        try:
            snap = service.reset(body.get("variant"), body.get("household_id"))
        except HdrError as e:
            return JSONResponse({"error": e.code, "message": e.message}, status_code=400)
        except KeyError as e:
            return JSONResponse({"error": "INVALID_INPUT", "message": str(e)}, status_code=400)
        return JSONResponse({"household_id": snap.household_id, "state_version": snap.state_version,
                             "variant": body.get("variant")})

    async def fault(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if request.method == "DELETE":
            service.clear_faults(request.query_params.get("household_id", "hh-demo"))
            service.clear_faults("*")
            return JSONResponse({"cleared": True})
        b = await request.json()
        service.add_fault(b.get("household_id", "hh-demo"), b["target"], b.get("selector", "*"),
                          int(b.get("times", 1)), int(b.get("delay_ms", 0)))
        return JSONResponse({"added": b})

    async def notice(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            return JSONResponse(service.demo_notice(request.query_params.get("kind") or None))
        except HdrError as e:
            return JSONResponse({"error": e.code, "message": e.message}, status_code=404)

    app.add_route("/admin/reset", reset, methods=["POST"])
    app.add_route("/admin/faults", fault, methods=["POST", "DELETE"])
    app.add_route("/admin/demo-notice", notice, methods=["GET"])


PINNED_PROTOCOL = "2025-11-25"
UNSUPPORTED_PROTOCOL_VERSION = -32022


class PinHandshakeProtocol:
    """Serve MCP 2025-11-25 only.

    The SDK also speaks the 2026-07-28 era (a server/discover probe instead of
    initialize, a version stamp on every request). Clients in auto mode, Strands'
    MCPClient among them, try that first. Refusing it with -32022 and naming
    2025-11-25 as supported makes them fall back to the initialize handshake, so
    every client ends up on the version this server is built and tested for.
    """

    def __init__(self, app: Any, path: str = "/mcp"):
        self.app = app
        self.path = path

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"].rstrip("/") != self.path:
            await self.app(scope, receive, send)
            return
        header = dict(scope["headers"]).get(b"mcp-protocol-version", b"").decode()
        chunks, more = [], True
        while more:
            message = await receive()
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        body = b"".join(chunks)
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {}
        method = payload.get("method") if isinstance(payload, dict) else None
        if header in MODERN_PROTOCOL_VERSIONS or method == "server/discover":
            error = {"jsonrpc": "2.0", "id": payload.get("id") if isinstance(payload, dict) else None,
                     "error": {"code": UNSUPPORTED_PROTOCOL_VERSION,
                               "message": f"Unsupported protocol version; this server implements {PINNED_PROTOCOL}",
                               "data": {"supported": [PINNED_PROTOCOL], "requested": header or None}}}
            await JSONResponse(error, status_code=400)(scope, receive, send)
            return

        replayed = False

        async def replay() -> dict:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)


def public_hosts(env: dict[str, str]) -> list[str]:
    """Host names a deployed server answers to: HDR_PUBLIC_HOSTS (comma-separated), or the
    SPACE_HOST that Hugging Face Spaces sets. Empty for local runs."""
    names = [h.strip() for h in (env.get("HDR_PUBLIC_HOSTS") or env.get("SPACE_HOST") or "").split(",")]
    return [h for h in names if h]


def create_app(service: RecoveryService, *, host: str = "127.0.0.1", port: int = 8765,
               admin_token: str | None = None, extra_hosts: list[str] | None = None) -> Starlette:
    """`extra_hosts` are public host names (no scheme). Requests must carry one of them, or a
    local address, in the Host header; browser Origins must be local or https on those hosts."""
    mcp = build_mcp(service)
    extra = extra_hosts or []
    hosts = [f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1:*", "localhost:*"] + extra
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*"] + [f"https://{h}" for h in extra],
    )
    app = mcp.streamable_http_app(streamable_http_path="/mcp", transport_security=security, host=host)
    add_admin_routes(app, service, admin_token)
    app.add_middleware(PinHandshakeProtocol)
    return app


def app_from_env() -> Starlette:
    env = dict(os.environ)
    service = RecoveryService(settings_from_env(env))
    if env.get("HDR_SEED_ON_START", "1") == "1":
        service.reset(env.get("HDR_VARIANT") or None)
    return create_app(
        service,
        host=env.get("HDR_MCP_HOST", "127.0.0.1"),
        port=int(env.get("HDR_MCP_PORT", "8765")),
        admin_token=env.get("HDR_ADMIN_TOKEN") or None,
        extra_hosts=public_hosts(env),
    )
