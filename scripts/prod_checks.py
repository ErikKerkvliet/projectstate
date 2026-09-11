"""Post-deploy checks against a running instance: retry storm, caps, exhausted credit, wallet isolation, audit ledger.

usage (on the server):  /opt/projectstate/venv/bin/python scripts/prod_checks.py [https://127.0.0.1]
Creates throwaway x402 wallet tenants with granted credit (no real payment). They show up in the dashboard as
wallets with a 'prodcheck' note in the ledger and can be left alone or deleted afterwards.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

env_file = Path(__file__).resolve().parents[1] / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
os.environ.setdefault("BASE_URL", BASE)
os.environ["X402_MODE"] = "off"  # this process only grants credit; it never talks to a facilitator

import httpx  # noqa: E402
import httpx2  # noqa: E402
from eth_account import Account  # noqa: E402
from mcp import Client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from projectstate.config import Settings, usd_to_micro  # noqa: E402
from projectstate.db import Database  # noqa: E402
from projectstate.mcp_app import Services  # noqa: E402

s = Settings()
db = Database(s.db_path)
svc = Services(db, s)
stamp = str(int(time.time()))
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))


def wallet(tag: str, credit: str = "1.00") -> tuple[str, str]:
    address = Account.create().address.lower()
    tid = f"x_{address}"
    db.ensure_tenant(tid, "x402", address)
    svc.meter.credit(tid, "x402", usd_to_micro(credit), ref=f"prodcheck:{tag}:{stamp}", note=f"prodcheck {tag}")
    return tid, svc.x402.issue_credit_token(tid)


def client(token: str):
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=60, verify=False)
    return Client(streamable_http_client(f"{BASE}/mcp", http_client=http))


def text(r) -> str:
    return " ".join(getattr(c, "text", "") for c in r.content)


async def main() -> None:
    # 1. retry storm: 40 concurrent identical writes with one idempotency key -> exactly one charge
    tid, token = wallet("storm")
    before = svc.meter.balance(tid)
    async with client(token) as c:
        await c.call_tool("project_open", {"project": "prodcheck"})
        args = {"project": "prodcheck", "kind": "note", "title": "storm", "body": stamp, "idempotency_key": f"storm-{stamp}"}
        rs = await asyncio.gather(*[c.call_tool("remember", args) for _ in range(40)])
        rs2 = [await c.call_tool("remember", args) for _ in range(5)]
    charges = db.val("SELECT COUNT(*) FROM ledger WHERE tenant_id=? AND kind='charge' AND tool='remember'", (tid,), 0)
    check("retry storm charged once", charges == 1 and all(not r.is_error for r in rs + rs2),
          f"charges={charges}, spent={before - svc.meter.balance(tid)} micro-USD, deduped={db.val('SELECT COUNT(*) FROM calls WHERE tenant_id=? AND deduped=1', (tid,), 0)}")

    # 2. daily cap
    tid, token = wallet("cap")
    svc.meter.set_caps(tid, 5, None, None)
    async with client(token) as c:
        oks = [not (await c.call_tool("project_open", {"project": f"cap{i}"})).is_error for i in range(5)]
        r = await c.call_tool("project_open", {"project": "cap-over"})
    check("daily cap enforced", all(oks) and r.is_error and "Daily cap reached" in text(r), text(r)[:90])

    # 3. per-minute cap
    tid, token = wallet("rate")
    svc.meter.set_caps(tid, None, None, 3)
    async with client(token) as c:
        for i in range(3):
            await c.call_tool("project_open", {"project": f"rate{i}"})
        r = await c.call_tool("project_open", {"project": "rate-over"})
    check("per-minute cap enforced", r.is_error and "Rate limit" in text(r), text(r)[:80])

    # 4. exhausted credit -> PaymentRequired again
    tid, token = wallet("broke", credit="0.001")
    async with client(token) as c:
        r = await c.call_tool("recall", {"project": "x", "query": "y"})
    pr = r.structured_content if isinstance(r.structured_content, dict) else {}
    check("exhausted credit asks for payment", r.is_error and pr.get("x402Version") == 2 and "Credit exhausted" in pr.get("error", ""), str(pr.get("error", text(r)))[:110])

    # 5. wallet isolation
    ta, ka = wallet("alice")
    tb, kb = wallet("bob")
    async with client(ka) as a:
        await a.call_tool("project_open", {"project": "iso"})
        r = await a.call_tool("remember", {"project": "iso", "kind": "decision", "title": f"alice secret {stamp}"})
        eid = int(text(r).split("#")[1].split()[0])
    async with client(kb) as b:
        await b.call_tool("project_open", {"project": "iso"})
        r1 = await b.call_tool("recall", {"project": "iso", "query": "alice secret"})
        r2 = await b.call_tool("update", {"project": "iso", "id": eid, "status": "reverted"})
        r3 = await b.call_tool("project_open", {})
    check("wallet isolation", stamp not in text(r1) and "No entries" in text(r1) and r2.is_error and "not found" in text(r2) and stamp not in text(r3))

    # 6. audit: ledger == wallets, every charge has its call
    bad = [r["id"] for r in db.q("SELECT id FROM tenants") if not svc.meter.ledger_matches_wallet(r["id"])]
    orphans = db.val("SELECT COUNT(*) FROM ledger l WHERE l.kind='charge' AND NOT EXISTS (SELECT 1 FROM calls c WHERE c.id=l.call_id AND c.charged=-l.amount)", (), 0)
    check("audit ledger consistent", not bad and orphans == 0, f"mismatched wallets={bad}, orphan charges={orphans}")

    # 7. unpaid call gets an x402 PaymentRequired, and no account surface is left
    async with Client(streamable_http_client(f"{BASE}/mcp", http_client=httpx2.AsyncClient(timeout=30, verify=False))) as c:
        r = await c.call_tool("project_open", {"project": "unpaid"})
    pr = r.structured_content if isinstance(r.structured_content, dict) else {}
    gone = all(httpx.get(f"{BASE}{p}", verify=False, follow_redirects=False).status_code == 404 for p in ("/signup", "/login", "/account", "/token", "/register"))
    check("unpaid call -> PaymentRequired, no account routes", r.is_error and pr.get("x402Version") == 2 and bool(pr.get("accepts")) and gone,
          f"amount={pr.get('accepts', [{}])[0].get('amount')}, account routes gone={gone}")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


asyncio.run(main())
