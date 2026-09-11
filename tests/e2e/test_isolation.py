from .conftest import payment_required, text


async def test_wallet_isolation_over_the_wire(server, h):
    ta, ka = h.wallet()
    tb, kb = h.wallet()
    async with h.client(ka) as a:
        await a.call_tool("project_open", {"project": "shared", "name": "Alice's"})
        r = await a.call_tool("remember", {"project": "shared", "kind": "decision", "title": "Alice secret decision", "body": "alpha-only"})
        alice_id = int(text(r).split("#")[1].split()[0])
    async with h.client(kb) as b:
        r = await b.call_tool("project_open", {})
        assert "no projects" in text(r)
        await b.call_tool("project_open", {"project": "shared", "name": "Bob's"})
        r = await b.call_tool("recall", {"project": "shared", "query": "alice secret alpha"})
        assert "Alice" not in text(r) and "No entries" in text(r)
        r = await b.call_tool("update", {"project": "shared", "id": alice_id, "status": "reverted"})
        assert r.is_error and "not found" in text(r)
        r = await b.call_tool("remember", {"project": "shared", "kind": "decision", "title": "steal", "supersedes": alice_id})
        assert r.is_error and "unknown entry" in text(r)
        r = await b.call_tool("project_open", {"project": "shared"})
        assert "Bob's" in text(r) and "Alice" not in text(r)
        assert "Alice" not in text(await b.call_tool("recall", {"project": "shared"}))
    async with h.client(ka) as a:
        r = await a.call_tool("project_open", {"project": "shared"})
        assert "Alice secret decision" in text(r) and "Bob" not in text(r)
    e = h.svc.store.get_entry(ta, alice_id)
    assert e and e["status"] == "active"
    # spending is per wallet
    assert h.svc.db.val("SELECT COUNT(*) FROM ledger WHERE tenant_id=? AND kind='charge'", (tb,)) == 5
    assert h.svc.meter.ledger_matches_wallet(ta) and h.svc.meter.ledger_matches_wallet(tb)


async def test_revoked_credit_token_stops_working(server, h):
    tid, key = h.wallet()
    async with h.client(key) as c:
        assert not (await c.call_tool("project_open", {"project": "revoke-me"})).is_error
    h.svc.db.exec("DELETE FROM x402_credit_tokens WHERE tenant_id=?", (tid,))
    async with h.client(key) as c:
        payment_required(await c.call_tool("project_open", {"project": "revoke-me"}))
    # the balance survives a revoked token
    assert h.balance(tid) > 0


async def test_another_wallets_token_cannot_reach_your_data(server, h):
    ta, ka = h.wallet()
    tb, kb = h.wallet()
    async with h.client(ka) as a:
        await a.call_tool("project_open", {"project": "private-a"})
        await a.call_tool("remember", {"project": "private-a", "kind": "note", "title": "top secret alpha"})
    async with h.client(kb) as b:
        r = await b.call_tool("project_open", {"project": "private-a"})
        assert "top secret" not in text(r)  # B gets its own empty project of the same name
        assert "0 total" in text(r)
