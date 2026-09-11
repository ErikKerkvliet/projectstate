import httpx2
from eth_account import Account
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from x402 import x402Client
from x402.mechanisms.evm.exact import register_exact_evm_client
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.schemas import PaymentRequired

from .conftest import payment_required as _pr
from .conftest import text


def _payer():
    acct = Account.create()
    x = x402Client()
    register_exact_evm_client(x, EthAccountSigner(acct))
    return acct, x


async def _pay(x, pr):
    p = await x.create_payment_payload(PaymentRequired(**pr))
    return p.model_dump(by_alias=True, exclude_none=True)


async def test_session_binding_pack_replay_and_exhaustion(server, h):
    """Legacy (sessionful) connection: one pack payment, then calls draw from it until exhausted."""
    acct, x = _payer()
    tid = f"x_{acct.address.lower()}"
    async with h.client(None, mode="legacy") as c:
        tools = await c.list_tools()
        assert tools.tools[0].meta["x402"]["packUsd"] == "0.1"
        pr = _pr(await c.call_tool("project_open", {"project": "xp"}))
        acc = pr["accepts"][0]
        assert acc["scheme"] == "exact" and acc["network"] == "eip155:84532" and acc["amount"] == "100000" and acc["payTo"].lower() == server.pay_to.lower()
        assert "credited to your wallet" in pr["error"] and "x402/payment" in pr["error"]
        payment = await _pay(x, pr)
        r2 = await c.call_tool("project_open", {"project": "xp"}, meta={"x402/payment": payment})
        assert not r2.is_error, text(r2)
        assert r2.meta["x402/payment-response"]["success"] is True and r2.meta["x402/payment-response"]["transaction"].startswith("0x")
        assert r2.meta["projectstate/balanceUsd"] == "0.098" and r2.meta["projectstate/x402-credit"].startswith("xc_")
        # follow-up calls in the same session draw from the credit, no payment attached
        r3 = await c.call_tool("remember", {"project": "xp", "kind": "note", "title": "paid with usdc"})
        assert not r3.is_error and r3.meta["projectstate/chargedUsd"] == "0.002" and "x402/payment-response" not in r3.meta
        # replaying the same payment is refused (nonce) and answered with a fresh PaymentRequired
        assert "already used" in _pr(await c.call_tool("recall", {"project": "xp", "query": "usdc"}, meta={"x402/payment": payment}))["error"]
        # exhaust the credit: 0.096 left -> 19 recalls at 0.005 = 0.095, the 20th must ask for payment
        for i in range(19):
            assert not (await c.call_tool("recall", {"project": "xp", "query": f"q{i}"})).is_error
        pr5 = _pr(await c.call_tool("recall", {"project": "xp", "query": "final"}))
        assert "Credit exhausted" in pr5["error"]
        r6 = await c.call_tool("recall", {"project": "xp", "query": "final"}, meta={"x402/payment": await _pay(x, pr5)})
        assert not r6.is_error and r6.meta["projectstate/balanceUsd"] == "0.096"
    assert h.svc.meter.ledger_matches_wallet(tid)
    rows = h.svc.db.q("SELECT status, amount, payer FROM x402_payments WHERE payer=? ORDER BY id", (acct.address.lower(),))
    assert [r["status"] for r in rows][:2] == ["settled", "settled"]
    assert h.svc.db.val("SELECT COUNT(*) FROM ledger WHERE tenant_id=? AND kind='topup'", (tid,)) == 2
    # a new session is not bound; the leftover balance survives and the next payment re-binds it
    async with h.client(None, mode="legacy") as c2:
        _pr(await c2.call_tool("project_open", {"project": "xp"}))
        pr = _pr(await c2.call_tool("project_status", {"project": "xp"}))
        r = await c2.call_tool("project_open", {"project": "xp"}, meta={"x402/payment": await _pay(x, pr)})
        assert not r.is_error and r.meta["projectstate/balanceUsd"] == "0.194"  # 0.096 + 0.10 - 0.002


async def test_sessionless_uses_credit_token_or_per_call_amount(server, h):
    """2026-07-28 connection: no Mcp-Session-Id. Without a credit token each call is quoted the per-call amount;
    with the token from the paid result, calls draw from the wallet credit."""
    acct, x = _payer()
    tid = f"x_{acct.address.lower()}"
    async with h.client(None) as c:  # sessionless
        pr = _pr(await c.call_tool("recall", {"project": "sl", "query": "x"}))
        assert pr["accepts"][0]["amount"] == "10000"  # max(price 0.005, min payment 0.01)
        pr_open = _pr(await c.call_tool("project_open", {"project": "sl"}))
        r = await c.call_tool("project_open", {"project": "sl"}, meta={"x402/payment": await _pay(x, pr_open)})
        assert not r.is_error, text(r)
        token = r.meta["projectstate/x402-credit"]
        assert r.meta["projectstate/balanceUsd"] == "0.008"
        # next call without anything -> quoted again (sessionless, unbound)
        _pr(await c.call_tool("project_status", {"project": "sl"}))
        # with the token in _meta -> drawn from credit
        r2 = await c.call_tool("project_status", {"project": "sl"}, meta={"projectstate/x402-credit": token})
        assert not r2.is_error and r2.meta["projectstate/balanceUsd"] == "0.006"
    # token as Authorization header on a fresh sessionless connection
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)
    async with Client(streamable_http_client(server.mcp, http_client=http)) as c3:
        r3 = await c3.call_tool("remember", {"project": "sl", "kind": "note", "title": "via credit token"})
        assert not r3.is_error and r3.meta["projectstate/balanceUsd"] == "0.004"
        _pr(await c3.call_tool("recall", {"project": "sl", "query": "credit"}))  # bound but dry -> pack quote
    # a bogus token is ignored (treated as unbound)
    http = httpx2.AsyncClient(headers={"Authorization": "Bearer xc_bogus"}, timeout=30)
    async with Client(streamable_http_client(server.mcp, http_client=http)) as c4:
        assert _pr(await c4.call_tool("project_status", {"project": "sl"}))["accepts"][0]["amount"] == "10000"
    assert h.svc.meter.ledger_matches_wallet(tid)


async def test_underfunded_and_bad_payloads(server, h):
    from mock_facilitator import STATE

    acct, x = _payer()
    STATE["broke"].add(acct.address.lower())
    async with h.client(None, mode="legacy") as c:
        pr = _pr(await c.call_tool("project_open", {"project": "poor"}))
        r = await c.call_tool("project_open", {"project": "poor"}, meta={"x402/payment": await _pay(x, pr)})
        assert "insufficient_funds" in _pr(r)["error"]
        r = await c.call_tool("project_open", {"project": "poor"}, meta={"x402/payment": {"garbage": True}})
        assert "Malformed x402 payment" in _pr(r)["error"]
        STATE["broke"].discard(acct.address.lower())
        payment = await _pay(x, pr)
        payment["payload"]["authorization"]["value"] = "1"
        r = await c.call_tool("project_open", {"project": "poor"}, meta={"x402/payment": payment})
        assert "rejected by the facilitator" in _pr(r)["error"]
        payment = await _pay(x, pr)
        payment["accepted"]["payTo"] = "0x000000000000000000000000000000000000dEaD"
        r = await c.call_tool("project_open", {"project": "poor"}, meta={"x402/payment": payment})
        assert "does not match" in _pr(r)["error"]
    assert h.svc.db.val("SELECT COUNT(*) FROM ledger WHERE tenant_id=?", (f"x_{acct.address.lower()}",), 0) == 0
    assert h.svc.db.val("SELECT COUNT(*) FROM calls WHERE rail='x402' AND error_kind='x402_rejected'", (), 0) >= 3


async def test_x402_path_is_an_alias_of_mcp(server, h):
    tid, token = h.wallet()
    async with h.client(token, url=server.x402) as c:
        r = await c.call_tool("project_open", {"project": "alias-test"})
        assert not r.is_error
    async with h.client(token, url=server.mcp) as c:
        r = await c.call_tool("recall", {"project": "alias-test", "query": ""})
        assert not r.is_error and "alias-test" in text(r)
