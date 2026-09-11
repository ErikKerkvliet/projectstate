"""x402 rail: account-less USDC payments inside MCP tool calls (x402 v2 MCP transport).

Aggregation: one on-chain payment buys a *pack* (settings 'x402.pack', default $0.10) that is credited to the wallet
tenant `x_<payer address>`; the MCP session is bound to that tenant and subsequent calls are metered from the
balance until it runs out, at which point the next call returns PaymentRequired again. So sub-cent calls settle
in one transaction per pack instead of one per call.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from typing import Any

from .config import Settings
from .db import Database, dumps, now_iso
from .meter import Meter, fmt_usd

log = logging.getLogger("projectstate.x402")

PAYMENT_META_KEY = "x402/payment"
PAYMENT_RESPONSE_META_KEY = "x402/payment-response"
CREDIT_META_KEY = "projectstate/x402-credit"


class X402Error(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class X402Rail:
    def __init__(self, db: Database, meter: Meter, settings: Settings):
        self.db = db
        self.meter = meter
        self.settings = settings
        self.mode = settings.x402_mode
        self.enabled = self.mode != "off" and bool(settings.x402_pay_to)
        self.network = settings.x402_network
        self.pay_to = settings.x402_pay_to
        self.facilitator_url = settings.x402_facilitator_url
        self._server: Any = None
        self._reqs_cache: tuple[int, list[Any]] | None = None
        self._sessions: dict[str, tuple[str, float]] = {}
        self._init_error: str | None = None
        # filled by mcp_app.build_server: {tool: {"description": str, "input_schema": dict}} for Bazaar discovery
        self.tool_info: dict[str, dict[str, Any]] = {}
        self._discovery_cache: dict[str, dict[str, Any] | None] = {}
        self._last_init_attempt = 0.0
        self._init_lock: Any = None
        if self.enabled:
            self._try_init()

    # -- setup --------------------------------------------------------------------------------
    def _try_init(self) -> bool:
        """Synchronous facilitator handshake (`/supported`). Called at startup; later retries go through `_ensure_init`."""
        if self._server is not None:
            return True
        self._last_init_attempt = time.time()
        try:
            from x402 import x402ResourceServer
            from x402.http import HTTPFacilitatorClient
            from x402.mechanisms.evm.exact import register_exact_evm_server

            # HTTPFacilitatorClient accepts a plain dict {"url", "create_headers"}; cdp-sdk builds one for the
            # Coinbase facilitator (JWT auth headers) when CDP keys are configured.
            cfg: dict[str, Any] = {"url": self.facilitator_url}
            if self.settings.cdp_api_key_id and self.settings.cdp_api_key_secret and self.mode == "live":
                try:
                    from cdp.x402 import create_facilitator_config  # type: ignore

                    cfg = dict(create_facilitator_config(self.settings.cdp_api_key_id, self.settings.cdp_api_key_secret))
                    self.facilitator_url = str(cfg.get("url", self.facilitator_url))
                except Exception as exc:  # pragma: no cover
                    log.error("CDP facilitator config failed (pip install cdp-sdk?): %s", exc)
            client = HTTPFacilitatorClient(cfg)
            server = x402ResourceServer(client)
            register_exact_evm_server(server, self.network)
            server.initialize()
            self._server = server
            self._init_error = None
            log.info("x402 rail ready: mode=%s network=%s facilitator=%s pay_to=%s", self.mode, self.network, cfg.get("url"), self.pay_to)
            return True
        except Exception as exc:
            self._init_error = f"{type(exc).__name__}: {exc}"
            log.error("x402 rail init failed: %s", self._init_error)
            return False

    async def _ensure_init(self) -> bool:
        """Retry a failed facilitator init lazily, at most once per 60 s, off the event loop."""
        if self._server is not None:
            return True
        if time.time() - self._last_init_attempt < 60:
            return False
        import asyncio

        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        async with self._init_lock:
            if self._server is not None:
                return True
            if time.time() - self._last_init_attempt < 60:
                return False
            return await asyncio.to_thread(self._try_init)

    @property
    def pack_price(self) -> int:
        return self.meter.setting_int("x402.pack", 100_000)

    @property
    def min_payment(self) -> int:
        return self.meter.setting_int("x402.min_payment", 10_000)

    def per_call_amount(self, tool: str) -> int:
        return max(self.meter.price(tool), self.min_payment)

    def requirements(self, amount: int) -> list[Any]:
        """Payment requirements for an exact micro-USD amount (cached per amount)."""
        from x402.schemas import ResourceConfig

        cache: dict[int, list[Any]] = self.__dict__.setdefault("_reqs_by_amount", {})
        if amount in cache:
            return cache[amount]
        usd = f"${amount / 1_000_000:.6f}".rstrip("0").rstrip(".")
        reqs = self._server.build_payment_requirements(
            ResourceConfig(scheme="exact", network=self.network, pay_to=self.pay_to, price=usd, max_timeout_seconds=600)
        )
        if len(cache) > 50:
            cache.clear()
        cache[amount] = reqs
        return reqs

    def clear_cache(self) -> None:
        self.__dict__.pop("_reqs_by_amount", None)

    # -- session binding ----------------------------------------------------------------------
    def session_tenant(self, session_id: str | None) -> str | None:
        if not session_id:
            return None
        hit = self._sessions.get(session_id)
        if hit is None:
            return None
        self._sessions[session_id] = (hit[0], time.time())
        return hit[0]

    def _bind(self, session_id: str | None, tenant_id: str) -> None:
        if session_id:
            self._sessions[session_id] = (tenant_id, time.time())
        if len(self._sessions) > 5000:
            cutoff = time.time() - 6 * 3600
            self._sessions = {k: v for k, v in self._sessions.items() if v[1] > cutoff}

    def forget_sessions(self, tenant_id: str) -> None:
        """Unbind every MCP session currently pointing at this tenant (balance is untouched)."""
        self._sessions = {k: v for k, v in self._sessions.items() if v[0] != tenant_id}

    def issue_credit_token(self, tenant_id: str) -> str:
        """Opaque bearer that lets a sessionless client keep drawing from its wallet credit."""
        tok = "xc_" + secrets.token_urlsafe(24)
        self.db.exec("INSERT INTO x402_credit_tokens(token_hash,tenant_id,created_at) VALUES(?,?,?)", (hashlib.sha256(tok.encode()).hexdigest(), tenant_id, now_iso()))
        return tok

    def credit_token_tenant(self, token: str | None) -> str | None:
        if not token or not token.startswith("xc_"):
            return None
        r = self.db.one("SELECT tenant_id FROM x402_credit_tokens WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
        if r is None:
            return None
        self.db.exec("UPDATE x402_credit_tokens SET last_used_at=? WHERE token_hash=?", (now_iso(), hashlib.sha256(token.encode()).hexdigest()))
        return r["tenant_id"]

    # -- payment required ---------------------------------------------------------------------
    async def payment_required(self, tool: str, error: str, sessionless: bool = False) -> dict[str, Any]:
        """Return the PaymentRequired object (x402 v2) for a tool call, as a plain dict.

        Sessionful callers (Mcp-Session-Id or a credit token) are quoted the pack; sessionless ones the per-call amount,
        so a generic client that cannot present anything on its next call never overpays."""
        if not self.enabled or not await self._ensure_init():
            return {
                "x402Version": 2,
                "error": f"x402 payments are temporarily unavailable on this server ({self._init_error or 'mode off'}). "
                "Retry in a minute.",
                "accepts": [],
            }
        from x402.schemas import ResourceInfo

        price_per_call = self.meter.price(tool)
        amount = self.per_call_amount(tool) if sessionless else self.pack_price
        calls = (amount // price_per_call) if price_per_call else 0
        msg = (
            f"{error} Pay {fmt_usd(amount)} in USDC; it is credited to your wallet address and covers about {calls} call(s) "
            f"at the current prices (this tool: {fmt_usd(price_per_call)}/call). Retry the same call with the payment in "
            f"_meta['{PAYMENT_META_KEY}']. The paid result carries _meta['{CREDIT_META_KEY}']: send that value as "
            f"'Authorization: Bearer <token>' (or in _meta['{CREDIT_META_KEY}']) on later calls to keep drawing from the credit; "
            f"on a session-based connection (Mcp-Session-Id) this happens automatically."
        )
        pr = await self._server.create_payment_required_response(
            self.requirements(amount),
            self.resource_info(tool),
            msg,
            self.discovery_extension(tool),
        )
        return pr.model_dump(by_alias=True, exclude_none=True)

    # -- discovery metadata (x402 Bazaar) ------------------------------------------------------
    SERVICE_NAME = "projectstate"
    TAGS = ["memory", "agents", "project-state", "mcp", "x402"]
    EXAMPLES: dict[str, dict[str, Any]] = {
        "project_open": {"project": "my-app", "name": "My app"},
        "project_status": {"project": "my-app"},
        "remember": {"project": "my-app", "kind": "decision", "title": "Use SQLite FTS5 for recall", "body": "no vectors in v1"},
        "recall": {"project": "my-app", "query": "auth token expiry"},
        "update": {"project": "my-app", "id": 42, "status": "done"},
    }

    def resource_info(self, tool: str) -> Any:
        from x402.schemas import ResourceInfo

        info = self.tool_info.get(tool, {})
        desc = (info.get("description") or f"projectstate tool {tool}").split("\n")[0][:200]
        return ResourceInfo(
            url=f"mcp://tool/{tool}",
            description=desc,
            mime_type="text/plain",
            service_name=self.SERVICE_NAME,
            tags=self.TAGS,
            icon_url=f"{self.settings.base_url}/icon.svg",
        )

    def discovery_extension(self, tool: str) -> dict[str, Any] | None:
        """Bazaar `mcp` discovery declaration so facilitators can catalog the tool (never breaks a payment)."""
        if tool in self._discovery_cache:
            return self._discovery_cache[tool]
        ext: dict[str, Any] | None = None
        try:
            from x402.extensions.bazaar import DeclareMcpDiscoveryConfig, declare_mcp_discovery_extension

            info = self.tool_info.get(tool)
            if info:
                ext = declare_mcp_discovery_extension(
                    DeclareMcpDiscoveryConfig(
                        tool_name=tool,
                        description=(info.get("description") or "").split("\n")[0][:300] or None,
                        transport="streamable-http",
                        input_schema=info.get("input_schema") or {"type": "object"},
                        example=self.EXAMPLES.get(tool),
                    )
                )
        except Exception as exc:  # pragma: no cover
            log.warning("bazaar discovery declaration failed for %s: %s", tool, exc)
            ext = None
        self._discovery_cache[tool] = ext
        return ext

    # -- payment handling ---------------------------------------------------------------------
    async def handle_payment(self, payment: Any, session_id: str | None, tool: str) -> tuple[str, dict[str, Any]]:
        """Verify + settle a payment from _meta. Returns (tenant_id, settle_response dict). Raises X402Error."""
        if not self.enabled or not await self._ensure_init():
            raise X402Error(f"x402 payments are temporarily unavailable on this server ({self._init_error or 'mode off'}). Retry in a minute.")
        from x402.schemas import PaymentPayload

        try:
            if isinstance(payment, str):
                payment = json.loads(payment)
            payload = PaymentPayload(**payment)
        except Exception as exc:
            raise X402Error(f"Malformed x402 payment payload in _meta['{PAYMENT_META_KEY}']: {exc}")
        reqs = self.requirements(self.pack_price) + self.requirements(self.per_call_amount(tool))
        req = self._server.find_matching_requirements(reqs, payload)
        if req is None:
            raise X402Error(
                f"Payment does not match the accepted requirements (need scheme=exact, network={self.network}, "
                f"amount={reqs[0].amount} or {reqs[-1].amount} of {reqs[0].asset}, payTo={self.pay_to}). Re-read the PaymentRequired response."
            )
        auth = (payload.payload or {}).get("authorization") or {}
        nonce = str(auth.get("nonce") or payload.payload.get("nonce") or "")
        payer = str(auth.get("from") or "").lower()
        if not nonce:
            raise X402Error("Payment payload has no authorization nonce; use an x402 client that produces EIP-3009 authorizations.")
        with self.db.tx():
            if self.db.one("SELECT 1 FROM x402_payments WHERE nonce=?", (nonce,)):
                raise X402Error("This payment authorization was already used (replay). Send a fresh payment.")
            pid = self.db.exec(
                "INSERT INTO x402_payments(ts,payer,network,amount,nonce,session_id,tool,status,raw) VALUES(?,?,?,?,?,?,?,?,?)",
                (now_iso(), payer, str(req.network), int(req.amount), nonce, session_id, tool, "verifying", dumps(payment)[:4000]),
            )
        try:
            vr = await self._server.verify_payment(payload, req)
        except Exception as exc:
            self.db.exec("UPDATE x402_payments SET status='verify_error' WHERE id=?", (pid,))
            raise X402Error(f"Payment verification failed at the facilitator: {type(exc).__name__}: {exc}")
        if not vr.is_valid:
            reason = getattr(vr.verify, "invalid_reason", None) or "invalid"
            self.db.exec("UPDATE x402_payments SET status=? WHERE id=?", (f"invalid:{reason}"[:60], pid))
            raise X402Error(f"Payment rejected by the facilitator: {reason}. Check the payer has enough USDC on {self.network} and sign a fresh authorization.")
        payer = (getattr(vr.verify, "payer", None) or payer or "").lower()
        try:
            sr = await self._server.settle_payment(payload, req)
        except Exception as exc:
            self.db.exec("UPDATE x402_payments SET status='settle_error' WHERE id=?", (pid,))
            raise X402Error(f"Payment settlement failed: {type(exc).__name__}: {exc}. Nothing was charged; retry with a fresh authorization.")
        if not sr.success:
            self.db.exec("UPDATE x402_payments SET status=? WHERE id=?", (f"settle_failed:{sr.error_reason}"[:60], pid))
            raise X402Error(f"Payment settlement failed: {sr.error_reason or 'unknown'}. Nothing was charged; retry with a fresh authorization.")
        payer = (sr.payer or payer or "unknown").lower()
        tenant_id = f"x_{payer}"
        amount = int(req.amount)  # USDC atomic units (6 decimals) == micro-USD
        self.db.ensure_tenant(tenant_id, "x402", payer)
        with self.db.tx():
            self.db.exec("UPDATE x402_payments SET status='settled', tx=?, payer=? WHERE id=?", (sr.transaction, payer, pid))
            self.meter.credit(tenant_id, "x402", amount, ref=f"x402:{nonce}", note=f"x402 {sr.transaction} on {sr.network}")
        self._bind(session_id, tenant_id)
        log.info("x402 settled %s from %s tx=%s session=%s", fmt_usd(amount), payer, sr.transaction, session_id)
        return tenant_id, sr.model_dump(by_alias=True, exclude_none=True)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "network": self.network,
            "pay_to": self.pay_to,
            "facilitator": self.facilitator_url,
            "init_error": self._init_error,
            "pack_usd": fmt_usd(self.pack_price),
            "min_payment_usd": fmt_usd(self.min_payment),
            "bound_sessions": len(self._sessions),
        }
