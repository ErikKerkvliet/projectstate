"""Operator keys: free, metered, capped access without a wallet or any payment."""
import httpx

from .conftest import payment_required, text


def _key(h, label="test key", tenant=None):
    tenant = tenant or h.svc.x402.new_operator_tenant(label)
    return tenant, h.svc.x402.issue_credit_token(tenant, kind="comp", label=label)


async def test_operator_key_calls_are_free_and_never_charged(server, h):
    tid, key = _key(h, "free agent")
    assert h.balance(tid) == 0  # no wallet, no credit, no payment
    async with h.client(key) as c:
        r = await c.call_tool("project_open", {"project": "free", "name": "Free project"})
        assert not r.is_error, text(r)
        assert r.meta["projectstate/chargedUsd"] == "0"
        assert r.meta["projectstate/operatorKey"] is True
        assert "projectstate/balanceUsd" not in r.meta
        for i in range(5):
            rr = await c.call_tool("remember", {"project": "free", "kind": "note", "title": f"note {i}"})
            assert not rr.is_error
        r = await c.call_tool("recall", {"project": "free", "query": "note"})
        assert not r.is_error and "note 1" in text(r)
    assert h.balance(tid) == 0
    assert h.svc.db.val("SELECT COUNT(*) FROM ledger WHERE tenant_id=?", (tid,), 0) == 0
    assert h.svc.db.val("SELECT COALESCE(SUM(charged),0) FROM calls WHERE tenant_id=?", (tid,), 0) == 0
    assert h.svc.db.val("SELECT COUNT(*) FROM calls WHERE tenant_id=? AND rail='comp'", (tid,), 0) == 7
    assert h.svc.meter.ledger_matches_wallet(tid)


async def test_operator_key_still_respects_caps(server, h):
    tid, key = _key(h, "capped agent")
    h.svc.meter.set_caps(tid, 3, None, None)
    async with h.client(key) as c:
        for i in range(3):
            assert not (await c.call_tool("project_open", {"project": f"cap{i}"})).is_error
        r = await c.call_tool("project_open", {"project": "over"})
        assert r.is_error and "Daily cap reached" in text(r)
    # blocking works too
    h.svc.meter.set_caps(tid, None, None, None, blocked=True)
    async with h.client(key) as c:
        assert (await c.call_tool("project_open", {"project": "x"})).is_error


async def test_revoked_key_stops_working(server, h):
    import hashlib

    tid, key = _key(h, "revoke me")
    async with h.client(key) as c:
        assert not (await c.call_tool("project_open", {"project": "rev"})).is_error
    h.svc.x402.revoke_token(hashlib.sha256(key.encode()).hexdigest())
    async with h.client(key) as c:
        payment_required(await c.call_tool("project_open", {"project": "rev"}))


async def test_keys_are_isolated_but_can_share_a_space(server, h):
    ta, ka = _key(h, "agent A")
    tb, kb = _key(h, "agent B")
    shared_tenant, k_shared = _key(h, "agent C shares A", tenant=ta)
    async with h.client(ka) as a:
        await a.call_tool("project_open", {"project": "shared-space"})
        await a.call_tool("remember", {"project": "shared-space", "kind": "decision", "title": "secret of A"})
    async with h.client(kb) as b:  # different key, different space
        r = await b.call_tool("project_open", {})
        assert "no projects" in text(r)
    async with h.client(k_shared) as c:  # same tenant, same space
        r = await c.call_tool("recall", {"project": "shared-space", "query": "secret"})
        assert "secret of A" in text(r)
    assert shared_tenant == ta and tb != ta


async def test_unknown_key_is_asked_to_pay(server, h):
    async with h.client("ps_this-key-does-not-exist") as c:
        payment_required(await c.call_tool("project_open", {"project": "nope"}))


def test_admin_can_mint_and_revoke_a_key(server, h):
    adm = httpx.Client(base_url=server.base, follow_redirects=False)
    csrf = adm.get("/admin/login").text.split('name="csrf" value="')[1].split('"')[0]
    adm.post("/admin/login", data={"csrf": csrf, "username": "admin", "password": server.admin_password})
    page = adm.get("/admin/keys")
    assert page.status_code == 200 and "Operator keys" in page.text
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    r = adm.post("/admin/keys", data={"csrf": csrf, "action": "create", "label": "from dashboard"})
    assert r.status_code == 303
    shown = adm.get("/admin/keys").text
    assert "ps_" in shown and "from dashboard" in shown
    token = shown.split('class="key">')[1].split("<")[0].strip()
    assert token.startswith("ps_")
    hit = h.svc.x402.resolve_token(token)
    assert hit and hit[1] == "comp"
    row = h.svc.db.one("SELECT token_hash FROM x402_credit_tokens WHERE kind='comp' AND label='from dashboard'")
    csrf = adm.get("/admin/keys").text.split('name="csrf" value="')[1].split('"')[0]
    assert adm.post("/admin/keys", data={"csrf": csrf, "action": "revoke", "hash": row["token_hash"]}).status_code == 303
    assert h.svc.x402.resolve_token(token) is None
