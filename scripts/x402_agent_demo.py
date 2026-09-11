"""An account-less agent paying with x402 over MCP, step by step (no client library magic).

usage: python scripts/x402_agent_demo.py https://HOST/mcp [PRIVATE_KEY_HEX]
Without a key a throwaway wallet is generated (it will fail verification for lack of USDC, which is also a useful test).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eth_account import Account
from mcp import Client
from x402 import x402Client
from x402.mechanisms.evm.exact import register_exact_evm_client
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.schemas import PaymentRequired


def payment_required_of(result) -> dict | None:
    if not result.is_error:
        return None
    sc = result.structured_content
    if isinstance(sc, dict) and "accepts" in sc:
        return sc
    try:
        d = json.loads(result.content[0].text)
        return d if "accepts" in d else None
    except Exception:
        return None


async def main(url: str, key: str | None) -> None:
    acct = Account.from_key(key) if key else Account.create()
    print("payer wallet:", acct.address)
    x = x402Client()
    register_exact_evm_client(x, EthAccountSigner(acct))
    async with Client(url) as mcp:
        tools = await mcp.list_tools()
        print("tools:", [(t.name, t.meta) for t in tools.tools][:2], "...")
        args = {"project": "x402-demo", "name": "x402 demo"}
        r = await mcp.call_tool("project_open", args)
        pr = payment_required_of(r)
        print("\n1) unpaid call -> is_error=%s, PaymentRequired: %s" % (r.is_error, json.dumps(pr, indent=1)[:900] if pr else r.content[0].text[:300]))
        if not pr:
            return
        payload = await x.create_payment_payload(PaymentRequired(**pr))
        pd = payload.model_dump(by_alias=True, exclude_none=True)
        print("\n2) signed payment:", json.dumps(pd)[:300], "...")
        r2 = await mcp.call_tool("project_open", args, meta={"x402/payment": pd})
        print("\n3) paid retry -> is_error=%s\n%s\n_meta=%s" % (r2.is_error, r2.content[0].text[:500], json.dumps(r2.meta, indent=1)[:600]))
        if r2.is_error:
            return
        # This client speaks the sessionless 2026-07-28 protocol, so follow-up calls present the credit token from the
        # paid result (session-based clients get this binding automatically via Mcp-Session-Id).
        credit = (r2.meta or {}).get("projectstate/x402-credit")
        print("\ncredit token:", credit)
        for i in range(3):
            r3 = await mcp.call_tool("remember", {"project": "x402-demo", "kind": "note", "title": f"paid note {i}", "body": "drawn from the x402 credit"}, meta={"projectstate/x402-credit": credit})
            print(f"\n4.{i}) follow-up call (credit token, no payment) -> is_error={r3.is_error} charged={r3.meta.get('projectstate/chargedUsd') if r3.meta else None} balance={r3.meta.get('projectstate/balanceUsd') if r3.meta else None}")
        r4 = await mcp.call_tool("recall", {"project": "x402-demo", "query": "paid note"})
        print(f"\n5) call WITHOUT token -> is_error={r4.is_error} (sessionless: asks for payment again, credit stays on the wallet)")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else os.environ.get("X402_DEMO_KEY")))
