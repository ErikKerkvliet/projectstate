"""Builds the MCP ASGI app and the metering middleware.

There is one rail: x402. A caller is identified by the wallet that paid (tenant `x_<address>`), bound to the
request either by the MCP session id or by the credit token handed out with the paid result.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from typing import Any

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, ListToolsResult, TextContent

from . import __version__
from .config import Settings
from .db import Database
from .meter import Meter, MeterError, micro_to_usd
from .store import Store
from .tenancy import Caller, current_caller
from .tools import INSTRUCTIONS, register_tools
from .x402_rail import CREDIT_META_KEY, PAYMENT_META_KEY, PAYMENT_RESPONSE_META_KEY, X402Error, X402Rail

log = logging.getLogger("projectstate.mcp")

WRITE_TOOLS = {"remember", "update", "project_status", "project_open"}
RAIL = "x402"


def _error_result(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


def _payment_required_result(pr: dict[str, Any]) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(pr))], structured_content=pr, is_error=True)


def _headers_of(ctx: Any) -> dict[str, str]:
    req = getattr(ctx, "request", None)
    h = getattr(req, "headers", None)
    if h is None:
        return {}
    try:
        return {k.lower(): v for k, v in dict(h).items()}
    except Exception:
        return {}


class Services:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.store = Store(db)
        self.meter = Meter(db, settings)
        self.x402 = X402Rail(db, self.meter, settings)


def make_metering_middleware(svc: Services):
    meter, settings, x402 = svc.meter, svc.settings, svc.x402
    inflight: dict[tuple[str, str], asyncio.Future] = {}

    async def middleware(ctx: Any, call_next: Any) -> Any:
        if ctx.method == "tools/list":
            result = await call_next(ctx)
            try:
                prices = meter.prices()
                tools = result.tools if isinstance(result, ListToolsResult) else result.get("tools", [])
                for t in tools:
                    name = t.name if hasattr(t, "name") else t.get("name")
                    m: dict[str, Any] = {"priceUsd": micro_to_usd(prices.get(name, 0)), "currency": "USD"}
                    if x402.enabled:
                        m["x402"] = {
                            "network": x402.network,
                            "packUsd": micro_to_usd(x402.pack_price),
                            "minPaymentUsd": micro_to_usd(x402.min_payment),
                        }
                    if hasattr(t, "meta"):
                        t.meta = {**(t.meta or {}), **m}
                    else:
                        t["_meta"] = {**(t.get("_meta") or {}), **m}
            except Exception as exc:  # never break tools/list over pricing metadata
                log.warning("price injection failed: %s", exc)
            return result
        if ctx.method != "tools/call":
            return await call_next(ctx)

        params = dict(ctx.params or {})
        tool = str(params.get("name") or "")
        args = dict(params.get("arguments") or {})
        meta = dict(params.get("_meta") or {})
        headers = _headers_of(ctx)
        session_id = headers.get("mcp-session-id")
        t0 = time.perf_counter()
        settle: dict[str, Any] | None = None
        new_credit: str | None = None

        # -- who is calling? ------------------------------------------------------------------
        auth = headers.get("authorization", "")
        credit_tok = meta.get(CREDIT_META_KEY) or (auth[7:].strip() if auth.lower().startswith("bearer ") else None)
        bound_tenant = x402.session_tenant(session_id) or x402.credit_token_tenant(credit_tok)
        sessionless = not session_id and bound_tenant is None
        payment = meta.get(PAYMENT_META_KEY)
        if payment is not None:
            try:
                tenant_id, settle = await x402.handle_payment(payment, session_id, tool)
                new_credit = x402.issue_credit_token(tenant_id)
            except X402Error as exc:
                meter.log_call(None, RAIL, tool, (time.perf_counter() - t0) * 1000, False, "x402_rejected", exc.message, 0, None, session_id)
                return _payment_required_result(await x402.payment_required(tool, exc.message, sessionless))
        else:
            tenant_id = bound_tenant
        if tenant_id is None:
            meter.log_call(None, RAIL, tool, 0, False, "payment_required", "no payment / unbound caller", 0, None, session_id)
            return _payment_required_result(await x402.payment_required(tool, "Payment required to call this tool.", sessionless))
        svc.db.touch_tenant(tenant_id)
        caller = Caller(tenant_id=tenant_id, rail=RAIL)

        # -- idempotency / dedup --------------------------------------------------------------
        explicit_key = args.get("idempotency_key") or meta.get("idempotencyKey") or meta.get("idempotency_key")
        if "idempotency_key" in args and tool != "remember":
            args.pop("idempotency_key")
            params["arguments"] = args
            ctx = dataclasses.replace(ctx, params=params)
        key = meter.request_key(tenant_id, tool, {k: v for k, v in args.items() if k != "idempotency_key"}, explicit_key)
        hit = meter.dedup_get(tenant_id, key)
        if hit is None and (tenant_id, key) in inflight:
            # identical call still running (concurrent retry storm): wait for it instead of running/charging twice
            try:
                replay_data = await asyncio.wait_for(asyncio.shield(inflight[(tenant_id, key)]), timeout=60)
                hit = (None, replay_data) if replay_data is not None else None
            except Exception:
                hit = None
        if hit is not None:
            meter.log_call(tenant_id, RAIL, tool, (time.perf_counter() - t0) * 1000, True, None, None, 0, key, session_id, deduped=True)
            replay = CallToolResult.model_validate(hit[1])
            replay.meta = {**(replay.meta or {}), "projectstate/deduplicated": True}
            if settle:
                replay.meta[PAYMENT_RESPONSE_META_KEY] = settle
                replay.meta[CREDIT_META_KEY] = new_credit
            return replay
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        inflight[(tenant_id, key)] = fut
        try:
            res = await _run_metered(ctx, call_next, tenant_id, caller, tool, key, session_id, t0, settle)
            if new_credit:
                res.meta = {**(res.meta or {}), CREDIT_META_KEY: new_credit}
            if not fut.done():
                # waiters replay a successful result; on failure they run the call themselves
                fut.set_result(None if res.is_error else res.model_dump(by_alias=True, exclude_none=True))
            return res
        finally:
            inflight.pop((tenant_id, key), None)
            if not fut.done():
                fut.set_result(None)

    async def _run_metered(ctx: Any, call_next: Any, tenant_id: str, caller: Caller, tool: str, key: str, session_id: str | None, t0: float, settle: dict[str, Any] | None) -> CallToolResult:
        # -- caps and balance ------------------------------------------------------------------
        price = meter.price(tool)
        try:
            meter.check_caps(tenant_id)
            meter.check_balance(tenant_id, price)
        except MeterError as exc:
            meter.log_call(tenant_id, RAIL, tool, (time.perf_counter() - t0) * 1000, False, exc.kind, exc.message, 0, key, session_id)
            if exc.kind == "insufficient_balance":
                return _payment_required_result(await x402.payment_required(tool, exc.message))
            return _error_result(exc.message)

        # -- run the tool ----------------------------------------------------------------------
        token = current_caller.set(caller)
        try:
            result = await call_next(ctx)
        except Exception as exc:
            kind = "protocol_error" if type(exc).__name__ == "MCPError" else "exception"
            meter.log_call(tenant_id, RAIL, tool, (time.perf_counter() - t0) * 1000, False, kind, f"{type(exc).__name__}: {exc}", 0, key, session_id)
            raise
        finally:
            current_caller.reset(token)
        duration = (time.perf_counter() - t0) * 1000
        if isinstance(result, CallToolResult):
            res = result
        elif isinstance(result, dict):
            res = CallToolResult.model_validate(result)
        else:
            res = CallToolResult.model_validate(result.model_dump(by_alias=True)) if result is not None else CallToolResult(content=[])
        ok = not res.is_error
        err_msg = None
        if not ok:
            err_msg = " ".join(getattr(c, "text", "") for c in res.content)[:500]
        charged = price if ok else 0
        call_id = meter.log_call(tenant_id, RAIL, tool, duration, ok, None if ok else "tool_error", err_msg, charged, key, session_id)
        if ok:
            if charged:
                meter.charge(tenant_id, RAIL, tool, call_id, charged, key)
            meter.dedup_put(tenant_id, key, call_id, res.model_dump(by_alias=True, exclude_none=True))
            if tool in WRITE_TOOLS:
                svc.db.exec("DELETE FROM dedup WHERE tenant_id=? AND request_key LIKE 'h:%' AND request_key<>?", (tenant_id, key))
            res.meta = {**(res.meta or {}), "projectstate/chargedUsd": micro_to_usd(charged), "projectstate/balanceUsd": micro_to_usd(meter.balance(tenant_id))}
        if settle:
            res.meta = {**(res.meta or {}), PAYMENT_RESPONSE_META_KEY: settle}
        meter.check_volume_alarm(tenant_id)
        return res

    return middleware


def build_server(svc: Services) -> MCPServer:
    s = svc.settings
    mcp = MCPServer(
        name="projectstate",
        title="projectstate",
        instructions=INSTRUCTIONS,
        website_url=s.base_url,
        version=__version__,
        middleware=[make_metering_middleware(svc)],
    )
    register_tools(mcp, svc.store)
    svc.x402.tool_info = {
        t.name: {"description": t.description, "input_schema": t.parameters} for t in mcp._tool_manager.list_tools()
    }
    return mcp


def build_app(svc: Services):
    sec = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    server = build_server(svc)
    app = server.streamable_http_app(streamable_http_path="/mcp", json_response=True, transport_security=sec)
    return server, app
