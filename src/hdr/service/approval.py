"""Approval tokens: HMAC-signed, bound to one plan id + version + action set."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


class ApprovalError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue(secret: bytes, *, plan_id: str, plan_version: int, base_state_version: int, actions_hash: str,
          ttl_seconds: int, now: float | None = None) -> tuple[str, int]:
    expires = int((now if now is not None else time.time()) + ttl_seconds)
    payload = {"p": plan_id, "v": plan_version, "s": base_state_version, "h": actions_hash, "e": expires}
    body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}", expires


def verify(secret: bytes, token: str, now: float | None = None) -> dict:
    try:
        body, sig = token.split(".", 1)
    except (AttributeError, ValueError):
        raise ApprovalError("APPROVAL_INVALID", "approval token is malformed") from None
    expected = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        raise ApprovalError("APPROVAL_INVALID", "approval token signature does not match")
    try:
        payload = json.loads(_unb64(body))
    except ValueError:
        raise ApprovalError("APPROVAL_INVALID", "approval token payload is unreadable") from None
    if payload.get("e", 0) < (now if now is not None else time.time()):
        raise ApprovalError("APPROVAL_EXPIRED", "approval token has expired")
    return payload


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
