"""Boots the real app under uvicorn (subprocess) + a mock x402 facilitator (thread) once per test session."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import httpx2
import pytest
from eth_account import Account

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Server:
    base: str
    db_path: Path
    pay_to: str
    facilitator: str
    admin_password: str

    @property
    def mcp(self) -> str:
        return f"{self.base}/mcp"

    @property
    def x402(self) -> str:
        return f"{self.base}/x402/mcp"


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    from mock_facilitator import run_in_thread

    from projectstate.web.passwords import hash_password

    fport = _free_port()
    run_in_thread(fport)
    port = _free_port()
    db_path = tmp_path_factory.mktemp("db") / "e2e.db"
    pay_to = Account.create().address
    env = {
        **os.environ,
        "BASE_URL": f"http://127.0.0.1:{port}", "DB_PATH": str(db_path), "SECRET_KEY": "e2e-secret",
        "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD_HASH": hash_password("e2e-admin-pass"),
        "X402_MODE": "mock", "X402_FACILITATOR_URL": f"http://127.0.0.1:{fport}", "X402_NETWORK": "eip155:84532", "X402_PAY_TO": pay_to,
        "X402_PACK_USD": "0.10", "X402_MIN_PAYMENT_USD": "0.01", "DEFAULT_PER_MINUTE_CAP": "1000", "DEFAULT_DAILY_CALL_CAP": "5000",
        "LOG_LEVEL": "WARNING", "PYTHONPATH": str(ROOT),
    }
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "projectstate.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"], env=env, cwd=str(ROOT))
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("server did not start")
    yield Server(base=base, db_path=db_path, pay_to=pay_to, facilitator=f"http://127.0.0.1:{fport}", admin_password="e2e-admin-pass")
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def svc(server):
    """Direct handle on the same SQLite file the server uses (for setup/inspection)."""
    from projectstate.config import Settings
    from projectstate.db import Database
    from projectstate.mcp_app import Services

    os.environ["BASE_URL"] = server.base
    db = Database(server.db_path)
    s = Settings()
    s.x402_mode = "off"  # no facilitator init on the inspection side
    yield Services(db, s)
    db.close()


class Helper:
    def __init__(self, server: Server, svc):
        self.server = server
        self.svc = svc

    def wallet(self, credit_usd: str | None = "1.00") -> tuple[str, str]:
        """A funded x402 caller without going through a real payment: credit the wallet tenant and hand out a token."""
        from projectstate.config import usd_to_micro

        address = Account.create().address.lower()
        tenant_id = f"x_{address}"
        self.svc.db.ensure_tenant(tenant_id, "x402", address)
        if credit_usd:
            self.svc.meter.credit(tenant_id, "x402", usd_to_micro(credit_usd), ref=f"e2e:{address}:{time.time_ns()}", note="test credit")
        return tenant_id, self.svc.x402.issue_credit_token(tenant_id)

    def client(self, token: str | None, url: str | None = None, mode: str = "auto"):
        """mode='legacy' forces the 2025-era handshake (Mcp-Session-Id sessions); 'auto' uses 2026-07-28 (sessionless)."""
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        http = httpx2.AsyncClient(headers=headers, timeout=30)
        return Client(streamable_http_client(url or self.server.mcp, http_client=http), mode=mode)

    def balance(self, tid: str) -> int:
        return self.svc.meter.balance(tid)


@pytest.fixture
def h(server, svc):
    return Helper(server, svc)


def text(result) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


def payment_required(result) -> dict:
    """Assert the result is an x402 PaymentRequired and return it."""
    assert result.is_error, text(result)
    sc = result.structured_content
    assert isinstance(sc, dict) and sc.get("x402Version") == 2 and sc["accepts"], sc
    import json

    assert json.loads(result.content[0].text)["accepts"] == sc["accepts"]
    return sc
