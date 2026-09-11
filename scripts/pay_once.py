"""One real x402 payment against this server, then report what happened.

Run it, paste the payer wallet's private key when asked (it is not echoed, not stored, and never
reaches the shell history), and it does exactly one payment for the smallest amount the server asks.

    /opt/projectstate/venv/bin/python scripts/pay_once.py [URL]

Default URL is the live endpoint. Use a hot wallet holding only a couple of dollars of USDC on Base.
"""
from __future__ import annotations

import asyncio
import getpass
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eth_account import Account  # noqa: E402
from mcp import Client  # noqa: E402
from x402 import x402Client  # noqa: E402
from x402.mechanisms.evm.exact import register_exact_evm_client  # noqa: E402
from x402.mechanisms.evm.signers import EthAccountSigner  # noqa: E402
from x402.schemas import PaymentRequired  # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 else "https://projectstate.online/mcp"


def payment_required(result):
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


def text(r) -> str:
    return " ".join(getattr(c, "text", "") for c in r.content)


async def main() -> int:
    key = getpass.getpass("Private key of the PAYER wallet (input hidden, not stored): ").strip()
    if not key:
        print("no key given, nothing done")
        return 1
    try:
        acct = Account.from_key(key)
    except Exception as exc:
        print("that is not a valid private key:", exc)
        return 1
    del key
    print(f"\npayer   : {acct.address}")
    print(f"endpoint: {URL}\n")

    x = x402Client()
    register_exact_evm_client(x, EthAccountSigner(acct))
    async with Client(URL) as mcp:
        args = {"project": "live-check", "name": "live payment check"}
        r = await mcp.call_tool("project_open", args)
        pr = payment_required(r)
        if pr is None:
            print("server did not ask for payment (already have credit?):\n", text(r)[:300])
            return 0
        acc = pr["accepts"][0]
        amount = Decimal(acc["amount"]) / Decimal(10**6)
        print(f"server asks: ${amount} USDC on {acc['network']}")
        print(f"  asset : {acc['asset']}")
        print(f"  payTo : {acc['payTo']}")
        if input(f"\nSign and send ${amount} USDC? [yes/no] ").strip().lower() not in ("yes", "y"):
            print("cancelled, nothing sent")
            return 1

        payload = (await x.create_payment_payload(PaymentRequired(**pr))).model_dump(by_alias=True, exclude_none=True)
        print("\nsigned, sending...")
        r2 = await mcp.call_tool("project_open", args, meta={"x402/payment": payload})
        pr2 = payment_required(r2)
        if pr2 is not None:
            print("REJECTED:", pr2.get("error", "")[:300])
            return 1
        meta = r2.meta or {}
        settle = meta.get("x402/payment-response") or {}
        print("\nPAID. Settlement:")
        print("  success    :", settle.get("success"))
        print("  transaction:", settle.get("transaction"))
        print("  explorer   : https://basescan.org/tx/" + str(settle.get("transaction", "")))
        print("  charged    : $" + str(meta.get("projectstate/chargedUsd")))
        print("  credit left: $" + str(meta.get("projectstate/balanceUsd")))
        print("  credit token:", meta.get("projectstate/x402-credit"))
        print("\ntool result:\n" + text(r2)[:400])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
