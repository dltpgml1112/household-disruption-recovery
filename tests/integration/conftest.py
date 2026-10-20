from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from hdr.mcp_server.app import create_app
from hdr.service.recovery import RecoveryService, Settings

from tests.integration.live import LiveServer, free_port

ADMIN_TOKEN = "test-admin-token"


@dataclass
class Stack:
    live: LiveServer
    db: Path
    service: RecoveryService

    @property
    def mcp_url(self) -> str:
        return f"{self.live.url}/mcp"


def start_stack(tmp_path: Path, *, variant: str | None = None, ttl: int = 900):
    db = tmp_path / "hdr.sqlite3"
    service = RecoveryService(
        Settings(db_path=str(db), approval_secret=b"integration-secret-0123456789abcdef", approval_ttl_seconds=ttl)
    )
    service.reset(variant)
    port = free_port()
    return service, db, LiveServer(create_app(service, port=port, admin_token=ADMIN_TOKEN), port)


@pytest.fixture
def stack(tmp_path):
    service, db, live = start_stack(tmp_path)
    with live:
        yield Stack(live, db, service)


@pytest.fixture
def stack_linda_unavailable(tmp_path):
    service, db, live = start_stack(tmp_path, variant="linda-unavailable")
    with live:
        yield Stack(live, db, service)
