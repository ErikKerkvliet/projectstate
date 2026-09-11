import asyncio
import time

import httpx

from .conftest import payment_required, text


async def test_all_tools_and_error_messages(server, h):
    tid, key = h.wallet()
    async with h.client(key) as c:
        tools = await c.list_tools()
        assert sorted(t.name for t in tools.tools) == ["project_open", "project_status", "recall", "remember", "update"]
        meta = {t.name: t.meta for t in tools.tools}
        assert meta["recall"]["priceUsd"] == "0.005" and meta["recall"]["x402"]["packUsd"] == "0.1"
        r = await c.call_tool("recall", {"project": "ghost", "query": "x"})
        assert r.is_error and "Unknown project 'ghost'" in text(r) and "project_open" in text(r)
        r = await c.call_tool("project_open", {"project": "Bad Slug!"})
        assert r.is_error and "Invalid project slug" in text(r)
        assert not (await c.call_tool("project_open", {"project": "p1"})).is_error
        r = await c.call_tool("remember", {"project": "p1", "kind": "thought", "title": "x"})
        assert r.is_error and "Invalid kind 'thought'" in text(r)
        r = await c.call_tool("remember", {"project": "p1", "kind": "task", "title": "Do it", "status": "superseded"})
        assert r.is_error and "Invalid status 'superseded' for kind 'task'" in text(r)
        r = await c.call_tool("remember", {"project": "p1", "kind": "decision", "title": "Pick SQLite", "body": "because FTS5", "tags": ["db"]})
        assert not r.is_error and text(r).startswith("Saved #")
        eid = int(text(r).split("#")[1].split()[0])
        r = await c.call_tool("update", {"project": "p1", "id": eid, "status": "done"})
        assert r.is_error and "Invalid status 'done' for a decision" in text(r)
        r = await c.call_tool("update", {"project": "p1", "id": 999999, "status": "reverted"})
        assert r.is_error and "not found" in text(r)
        r = await c.call_tool("recall", {"project": "p1", "query": "sqlite"})
        assert not r.is_error and "Pick SQLite" in text(r) and "tags: db" in text(r)
        r = await c.call_tool("project_status", {"project": "p1", "set_status": "going well"})
        assert not r.is_error and "going well" in text(r)
    rows = h.svc.db.q("SELECT ok, error_kind, error_msg, charged, rail FROM calls WHERE tenant_id=? ORDER BY id", (tid,))
    errs = [r for r in rows if not r["ok"]]
    assert len(errs) == 6 and all(r["charged"] == 0 and r["error_kind"] == "tool_error" and r["error_msg"] for r in errs)
    assert all(r["rail"] == "x402" for r in rows)
    assert h.svc.meter.ledger_matches_wallet(tid)


async def test_retry_storm_is_charged_once(server, h):
    tid, key = h.wallet()
    before = h.balance(tid)
    async with h.client(key) as c:
        await c.call_tool("project_open", {"project": "storm"})
        args = {"project": "storm", "kind": "note", "title": "storm note", "body": "x", "idempotency_key": "storm-1"}
        results = await asyncio.gather(*[c.call_tool("remember", args) for _ in range(25)])
        assert all(not r.is_error for r in results)
        assert len({text(r) for r in results}) == 1
        for _ in range(10):
            r = await c.call_tool("remember", args)
            assert r.meta.get("projectstate/deduplicated") is True
        r = await c.call_tool("remember", {"project": "storm", "kind": "note", "title": "other note"})
        assert not r.is_error and r.meta.get("projectstate/deduplicated") is None
        r2 = await c.call_tool("remember", {"project": "storm", "kind": "note", "title": "other note"})
        assert r2.meta.get("projectstate/deduplicated") is True
    charges = h.svc.db.q("SELECT amount FROM ledger WHERE tenant_id=? AND kind='charge'", (tid,))
    assert len(charges) == 3  # project_open + storm note (once) + other note (once)
    assert before - h.balance(tid) == 2000 * 3
    assert h.svc.db.val("SELECT COUNT(*) FROM calls WHERE tenant_id=? AND deduped=1", (tid,)) == 24 + 10 + 1
    assert h.svc.meter.ledger_matches_wallet(tid)


async def test_recall_after_write_is_fresh_despite_dedup(server, h):
    tid, key = h.wallet()
    async with h.client(key) as c:
        await c.call_tool("project_open", {"project": "fresh"})
        r1 = await c.call_tool("recall", {"project": "fresh", "query": "kafka"})
        assert "No entries" in text(r1)
        await c.call_tool("remember", {"project": "fresh", "kind": "decision", "title": "Use Kafka for events"})
        r2 = await c.call_tool("recall", {"project": "fresh", "query": "kafka"})
        assert "Use Kafka" in text(r2) and r2.meta.get("projectstate/deduplicated") is None
        r3 = await c.call_tool("recall", {"project": "fresh", "query": "kafka"})
        assert r3.meta.get("projectstate/deduplicated") is True


async def test_caps_are_enforced_per_wallet(server, h):
    tid, key = h.wallet()
    h.svc.meter.set_caps(tid, 4, None, None)
    async with h.client(key) as c:
        for i in range(4):
            assert not (await c.call_tool("project_open", {"project": f"c{i}"})).is_error
        r = await c.call_tool("project_open", {"project": "c9"})
        assert r.is_error and "Daily cap reached: 4 calls today (cap 4)" in text(r)
        assert (await c.call_tool("recall", {"project": "c1", "query": "q"})).is_error
    assert h.svc.db.val("SELECT COUNT(*) FROM calls WHERE tenant_id=? AND error_kind='cap_daily_calls'", (tid,)) == 2
    assert h.svc.db.val("SELECT COUNT(*) FROM ledger WHERE tenant_id=? AND kind='charge'", (tid,)) == 4

    tid2, key2 = h.wallet()
    h.svc.meter.set_caps(tid2, None, None, 3)
    async with h.client(key2) as c:
        for i in range(3):
            assert not (await c.call_tool("project_open", {"project": f"r{i}"})).is_error
        r = await c.call_tool("project_open", {"project": "r9"})
        assert r.is_error and "Rate limit" in text(r) and "60 seconds" in text(r)

    tid3, key3 = h.wallet()
    h.svc.meter.set_caps(tid3, None, 6000, None)  # $0.006/day
    async with h.client(key3) as c:
        assert not (await c.call_tool("project_open", {"project": "s1"})).is_error  # 0.002
        assert not (await c.call_tool("recall", {"project": "s1", "query": "a"})).is_error  # 0.005 -> 0.007 spent
        r = await c.call_tool("project_open", {"project": "s2"})
        assert r.is_error and "Daily spend cap reached" in text(r)

    tid4, key4 = h.wallet()
    h.svc.meter.set_caps(tid4, None, None, None, blocked=True)
    async with h.client(key4) as c:
        r = await c.call_tool("project_open", {"project": "b1"})
        assert r.is_error and "blocked" in text(r)


async def test_exhausted_credit_asks_for_payment_again(server, h):
    tid, key = h.wallet(credit_usd="0.003")
    async with h.client(key) as c:
        r = await c.call_tool("project_open", {"project": "z"})  # 0.002 -> 0.001 left
        assert not r.is_error and r.meta["projectstate/balanceUsd"] == "0.001"
        pr = payment_required(await c.call_tool("recall", {"project": "z", "query": "a"}))
        assert "Credit exhausted" in pr["error"] and "$0.0050" in pr["error"] and "$0.0010" in pr["error"]
        assert pr["accepts"][0]["amount"] == "100000"  # bound caller is quoted the pack
    assert h.balance(tid) == 1000 and h.svc.meter.ledger_matches_wallet(tid)
    assert h.svc.db.val("SELECT COUNT(*) FROM calls WHERE tenant_id=? AND error_kind='insufficient_balance'", (tid,)) == 1


async def test_price_change_without_redeploy(server, h):
    tid, key = h.wallet()
    h.svc.meter.set_price("project_status", "0.004")
    time.sleep(2.5)  # server-side price cache TTL (2 s)
    async with h.client(key) as c:
        await c.call_tool("project_open", {"project": "pp"})
        tools = await c.list_tools()
        assert {t.name: t.meta["priceUsd"] for t in tools.tools}["project_status"] == "0.004"
        r = await c.call_tool("project_status", {"project": "pp"})
        assert r.meta["projectstate/chargedUsd"] == "0.004"
    h.svc.meter.set_price("project_status", "0.002")
    time.sleep(2.5)
    assert h.svc.db.val("SELECT amount FROM ledger WHERE tenant_id=? AND tool='project_status'", (tid,)) == -4000


async def test_audit_log_covers_every_cent(server, h):
    total_charges = h.svc.db.val("SELECT COALESCE(SUM(-amount),0) FROM ledger WHERE kind='charge'", (), 0)
    total_calls_charged = h.svc.db.val("SELECT COALESCE(SUM(charged),0) FROM calls", (), 0)
    assert total_charges == total_calls_charged
    orphans = h.svc.db.val(
        "SELECT COUNT(*) FROM ledger l WHERE l.kind='charge' AND NOT EXISTS (SELECT 1 FROM calls c WHERE c.id=l.call_id AND c.tenant_id=l.tenant_id AND c.tool=l.tool AND c.charged=-l.amount)", (), 0)
    assert orphans == 0
    for r in h.svc.db.q("SELECT id FROM tenants"):
        assert h.svc.meter.ledger_matches_wallet(r["id"]), r["id"]


def test_no_account_surface_is_left(server):
    for path in ("/signup", "/login", "/account", "/topup/paypal/create", "/authorize", "/token", "/register", "/oauth/login"):
        r = httpx.get(f"{server.base}{path}", follow_redirects=False)
        assert r.status_code == 404, f"{path} -> {r.status_code}"
    landing = httpx.get(f"{server.base}/").text
    assert "PayPal" not in landing and "Sign up" not in landing and "x402" in landing
    # the MCP endpoint answers without any auth header: payment, not authentication, is the gate
    r = httpx.post(f"{server.base}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                               "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
                   headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 200 and "projectstate" in r.text


def test_admin_dashboard(server, h):
    adm = httpx.Client(base_url=server.base, follow_redirects=False)
    assert adm.get("/admin").status_code == 303
    page = adm.get("/admin/login")
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    assert adm.post("/admin/login", data={"csrf": csrf, "username": "admin", "password": "wrong"}).status_code == 400
    assert adm.post("/admin/login", data={"csrf": csrf, "username": "admin", "password": server.admin_password}).status_code == 303
    tid = h.svc.db.val("SELECT id FROM tenants ORDER BY created_at DESC LIMIT 1")
    for path in ("/admin", "/admin/tools", "/admin/users", f"/admin/users/{tid}", "/admin/errors", "/admin/ledger", "/admin/search", "/admin/settings", "/admin/system"):
        resp = adm.get(path)
        assert resp.status_code == 200, path
        assert "Traceback" not in resp.text and "PayPal" not in resp.text
    s = adm.get("/admin/api/summary").json()
    assert s["totals"]["calls_7d"] > 0 and any(t["p95"] is not None for t in s["per_tool"])
    # admin can adjust a wallet's balance and revoke its tokens
    before = h.balance(tid)
    csrf = adm.get(f"/admin/users/{tid}").text.split('name="csrf" value="')[1].split('"')[0]
    assert adm.post(f"/admin/users/{tid}", data={"csrf": csrf, "action": "credit", "amount": "0.50", "note": "goodwill"}).status_code == 303
    assert h.balance(tid) == before + 500_000
    assert adm.post(f"/admin/users/{tid}", data={"csrf": csrf, "action": "revoke_tokens"}).status_code == 303
    assert h.svc.db.val("SELECT COUNT(*) FROM x402_credit_tokens WHERE tenant_id=?", (tid,)) == 0
    assert h.svc.meter.ledger_matches_wallet(tid)
