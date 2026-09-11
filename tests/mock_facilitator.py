"""Mock x402 facilitator (v2 HTTP API: /supported, /verify, /settle) for tests and local runs.

It really checks the EIP-3009 authorization: recipient, amount, validity window and the EIP-712 signature (recovers the
signer), and rejects reused nonces. It does not touch a chain: settle returns a fake transaction hash. It also lets a
test simulate an under-funded payer via the `broke` set.

Run standalone: python tests/mock_facilitator.py 8791
"""
from __future__ import annotations

import secrets
import sys
import threading
import time
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from x402.mechanisms.evm.utils import get_asset_info, get_evm_chain_id
from x402.mechanisms.evm.eip712 import hash_eip3009_authorization
from x402.mechanisms.evm.types import ExactEIP3009Authorization
from x402.mechanisms.evm.verify import verify_eoa_signature

NETWORK = "eip155:84532"
STATE: dict[str, Any] = {"seen_nonces": set(), "broke": set(), "verify_calls": 0, "settle_calls": 0, "fail_settle": False}


def _get(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d:
            return d[k]
    return default


def _check(body: dict[str, Any]) -> tuple[bool, str, str]:
    payload = _get(body, "paymentPayload", "payment_payload") or {}
    reqs = _get(body, "paymentRequirements", "payment_requirements") or {}
    inner = payload.get("payload") or {}
    auth = inner.get("authorization") or {}
    sig = inner.get("signature") or ""
    payer = str(_get(auth, "from", "from_address", default="")).lower()
    if not payer or not sig:
        return False, "invalid_payload", payer
    if str(reqs.get("network")) != NETWORK or reqs.get("scheme") != "exact":
        return False, "unsupported_scheme", payer
    if str(auth.get("to", "")).lower() != str(_get(reqs, "payTo", "pay_to", default="")).lower():
        return False, "invalid_exact_evm_payload_recipient_mismatch", payer
    if int(auth.get("value", 0)) != int(reqs.get("amount", -1)):
        return False, "invalid_exact_evm_payload_authorization_value_mismatch", payer
    now = int(time.time())
    if int(_get(auth, "validBefore", "valid_before", default=0)) < now + 5:
        return False, "invalid_exact_evm_payload_authorization_valid_before", payer
    if int(_get(auth, "validAfter", "valid_after", default=0)) > now:
        return False, "invalid_exact_evm_payload_authorization_valid_after", payer
    nonce = str(auth.get("nonce"))
    if nonce in STATE["seen_nonces"]:
        return False, "nonce_already_used", payer
    if payer in STATE["broke"]:
        return False, "insufficient_funds", payer
    info = get_asset_info(NETWORK, reqs.get("asset"))
    a = ExactEIP3009Authorization(
        from_address=auth.get("from"), to=auth.get("to"), value=str(auth.get("value")),
        valid_after=str(_get(auth, "validAfter", "valid_after")), valid_before=str(_get(auth, "validBefore", "valid_before")), nonce=nonce,
    )
    digest = hash_eip3009_authorization(a, get_evm_chain_id(NETWORK), reqs.get("asset"), (reqs.get("extra") or {}).get("name", info["name"]), (reqs.get("extra") or {}).get("version", info.get("version", "2")))
    if not verify_eoa_signature(digest, bytes.fromhex(sig.removeprefix("0x")), payer):
        return False, "invalid_exact_evm_payload_signature", payer
    return True, "", payer


async def supported(request: Request):
    return JSONResponse({"kinds": [{"x402Version": 2, "scheme": "exact", "network": NETWORK}], "extensions": [], "signers": {"eip155:*": ["0x0000000000000000000000000000000000000001"]}})


async def verify(request: Request):
    STATE["verify_calls"] += 1
    ok, reason, payer = _check(await request.json())
    if ok:
        return JSONResponse({"isValid": True, "payer": payer})
    return JSONResponse({"isValid": False, "invalidReason": reason, "payer": payer or None})


async def settle(request: Request):
    STATE["settle_calls"] += 1
    body = await request.json()
    ok, reason, payer = _check(body)
    if not ok or STATE["fail_settle"]:
        return JSONResponse({"success": False, "errorReason": reason or "settle_failed", "transaction": "", "network": NETWORK, "payer": payer or None})
    auth = ((body.get("paymentPayload") or body.get("payment_payload") or {}).get("payload") or {}).get("authorization") or {}
    STATE["seen_nonces"].add(str(auth.get("nonce")))
    return JSONResponse({"success": True, "transaction": "0x" + secrets.token_hex(32), "network": NETWORK, "payer": payer, "amount": str(auth.get("value"))})


app = Starlette(routes=[Route("/supported", supported), Route("/verify", verify, methods=["POST"]), Route("/settle", settle, methods=["POST"])])


def run_in_thread(port: int) -> threading.Thread:
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    return t


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8791, log_level="info")
