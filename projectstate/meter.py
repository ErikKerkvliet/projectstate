"""The meter: prices, wallets, caps, idempotent dedup, audit ledger, call log.

Money is integer micro-USD. Every balance change writes a ledger row with balance_after, so the sum of the
ledger for a tenant always equals the wallet balance (checked by tests and the admin dashboard).
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .config import Settings, usd_to_micro
from .db import Database, dumps, now_iso, today_iso

TOOLS = ("project_open", "project_status", "remember", "recall", "plan_check", "update")


def micro_to_usd(m: int) -> str:
    return f"{Decimal(m) / Decimal(1_000_000):.6f}".rstrip("0").rstrip(".") if m else "0"


def fmt_usd(m: int) -> str:
    return f"${Decimal(m) / Decimal(1_000_000):.4f}"


class MeterError(Exception):
    """A metering refusal. `message` is written for the LLM that made the call."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass
class Quote:
    tool: str
    price: int  # micro-USD
    balance: int


class Meter:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self._price_cache: dict[str, int] = {}
        self._price_cache_at = 0.0
        self.seed_defaults()

    # -- settings / prices -------------------------------------------------------------------
    def seed_defaults(self) -> None:
        s = self.settings
        for tool, usd in s.default_prices_usd.items():
            self.db.seed_setting(f"price.{tool}", str(usd_to_micro(usd)))
        self.db.seed_setting("cap.daily_calls", str(s.default_daily_call_cap))
        self.db.seed_setting("cap.daily_spend", str(usd_to_micro(s.default_daily_spend_cap_usd)))
        self.db.seed_setting("cap.per_minute", str(s.default_per_minute_cap))
        self.db.seed_setting("x402.pack", str(usd_to_micro(s.x402_pack_usd)))
        self.db.seed_setting("x402.min_payment", str(usd_to_micro(s.x402_min_payment_usd)))
        self.db.seed_setting("monthly_cost", str(usd_to_micro(s.monthly_cost_usd)))
        self.db.seed_setting("alarm.calls_per_hour", str(s.alarm_calls_per_hour))
        self.db.seed_setting("dedup_window", str(s.dedup_window_seconds))

    def prices(self) -> dict[str, int]:
        if time.monotonic() - self._price_cache_at > 2:
            self._price_cache = {t: int(self.db.get_setting(f"price.{t}", "0") or 0) for t in TOOLS}
            self._price_cache_at = time.monotonic()
        return dict(self._price_cache)

    def price(self, tool: str) -> int:
        return self.prices().get(tool, 0)

    def set_price(self, tool: str, usd: str) -> None:
        if tool not in TOOLS:
            raise ValueError(f"unknown tool {tool}")
        self.db.set_setting(f"price.{tool}", str(usd_to_micro(usd)))
        self._price_cache_at = 0

    def setting_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.db.get_setting(key, str(default)) or default)
        except ValueError:
            return default

    # -- wallets -----------------------------------------------------------------------------
    def wallet(self, tenant_id: str) -> dict[str, Any]:
        r = self.db.one("SELECT * FROM wallets WHERE tenant_id=?", (tenant_id,))
        if r is None:
            self.db.ensure_tenant(tenant_id, "unknown")
            r = self.db.one("SELECT * FROM wallets WHERE tenant_id=?", (tenant_id,))
        assert r is not None
        return dict(r)

    def balance(self, tenant_id: str) -> int:
        return int(self.db.val("SELECT balance FROM wallets WHERE tenant_id=?", (tenant_id,), 0))

    def caps(self, tenant_id: str) -> tuple[int, int, int]:
        w = self.wallet(tenant_id)
        return (
            int(w["daily_call_cap"] if w["daily_call_cap"] is not None else self.setting_int("cap.daily_calls", 2000)),
            int(w["daily_spend_cap"] if w["daily_spend_cap"] is not None else self.setting_int("cap.daily_spend", 5_000_000)),
            int(w["per_minute_cap"] if w["per_minute_cap"] is not None else self.setting_int("cap.per_minute", 60)),
        )

    def set_caps(self, tenant_id: str, daily_calls: int | None, daily_spend: int | None, per_minute: int | None, blocked: bool | None = None) -> None:
        self.wallet(tenant_id)
        with self.db.tx():
            self.db.exec(
                "UPDATE wallets SET daily_call_cap=?, daily_spend_cap=?, per_minute_cap=?, updated_at=? WHERE tenant_id=?",
                (daily_calls, daily_spend, per_minute, now_iso(), tenant_id),
            )
            if blocked is not None:
                self.db.exec("UPDATE wallets SET blocked=? WHERE tenant_id=?", (1 if blocked else 0, tenant_id))

    def usage_today(self, tenant_id: str) -> tuple[int, int]:
        day = today_iso()
        calls = int(self.db.val("SELECT COUNT(*) FROM calls WHERE tenant_id=? AND ts>=? AND deduped=0", (tenant_id, day), 0))
        spend = int(self.db.val("SELECT COALESCE(SUM(-amount),0) FROM ledger WHERE tenant_id=? AND kind='charge' AND ts>=?", (tenant_id, day), 0))
        return calls, spend

    def calls_last_minute(self, tenant_id: str) -> int:
        return int(self.db.val("SELECT COUNT(*) FROM calls WHERE tenant_id=? AND ts>=datetime('now','-60 seconds')", (tenant_id,), 0))

    # -- checks ------------------------------------------------------------------------------
    def check_caps(self, tenant_id: str) -> None:
        w = self.wallet(tenant_id)
        if w["blocked"]:
            raise MeterError("blocked", "This account is blocked by the operator. Contact the service owner via the dashboard URL.")
        daily_calls, daily_spend, per_minute = self.caps(tenant_id)
        if self.calls_last_minute(tenant_id) >= per_minute:
            raise MeterError(
                "cap_per_minute",
                f"Rate limit: more than {per_minute} calls in the last 60 seconds. Wait a minute before retrying; "
                "batch related lookups into one recall() with a broader query instead of many small calls.",
            )
        calls, spend = self.usage_today(tenant_id)
        if calls >= daily_calls:
            raise MeterError(
                "cap_daily_calls",
                f"Daily cap reached: {calls} calls today (cap {daily_calls}). Resets at 00:00 UTC. "
                "If this is legitimate, the account owner can raise the cap in the account settings.",
            )
        if spend >= daily_spend:
            raise MeterError(
                "cap_daily_spend",
                f"Daily spend cap reached: {fmt_usd(spend)} today (cap {fmt_usd(daily_spend)}). Resets at 00:00 UTC. "
                "The account owner can raise the cap in the account settings.",
            )

    def check_balance(self, tenant_id: str, price: int, rail: str = "x402", base_url: str = "") -> Quote:
        bal = self.balance(tenant_id)
        if price > 0 and bal < price:
            raise MeterError(
                "insufficient_balance",
                f"Credit exhausted: this call costs {fmt_usd(price)} but only {fmt_usd(bal)} is left on your wallet's "
                "credit. Pay again with x402 to top it up (the PaymentRequired response on your next call lists the amount).",
            )
        return Quote(tool="", price=price, balance=bal)

    # -- ledger ------------------------------------------------------------------------------
    def _post(self, tenant_id: str, rail: str, kind: str, amount: int, tool: str | None = None, call_id: int | None = None, ref: str | None = None, note: str | None = None) -> int:
        with self.db.tx():
            self.wallet(tenant_id)
            self.db.exec("UPDATE wallets SET balance=balance+?, updated_at=? WHERE tenant_id=?", (amount, now_iso(), tenant_id))
            bal = self.balance(tenant_id)
            self.db.exec(
                "INSERT INTO ledger(ts,tenant_id,rail,kind,tool,call_id,amount,balance_after,ref,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now_iso(), tenant_id, rail, kind, tool, call_id, amount, bal, ref, note),
            )
            return bal

    def charge(self, tenant_id: str, rail: str, tool: str, call_id: int, price: int, request_key: str | None) -> int:
        if price <= 0:
            return self.balance(tenant_id)
        return self._post(tenant_id, rail, "charge", -price, tool=tool, call_id=call_id, ref=request_key)

    def credit(self, tenant_id: str, rail: str, amount: int, ref: str, note: str = "", kind: str = "topup") -> int | None:
        """Idempotent credit: a second credit with the same ref is ignored (returns None)."""
        if amount <= 0:
            raise ValueError("credit amount must be positive")
        with self.db.tx():
            if self.db.one("SELECT 1 FROM ledger WHERE ref=? AND kind=?", (ref, kind)):
                return None
            return self._post(tenant_id, rail, kind, amount, ref=ref, note=note)

    def adjust(self, tenant_id: str, amount: int, note: str) -> int:
        return self._post(tenant_id, "admin", "adjust", amount, note=note, ref=f"adm-{time.time_ns()}")

    def ledger_matches_wallet(self, tenant_id: str) -> bool:
        s = int(self.db.val("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE tenant_id=?", (tenant_id,), 0))
        return s == self.balance(tenant_id)

    # -- dedup -------------------------------------------------------------------------------
    @staticmethod
    def request_key(tenant_id: str, tool: str, arguments: dict[str, Any] | None, explicit: str | None) -> str:
        if explicit:
            return "k:" + hashlib.sha256(f"{tenant_id}|{tool}|{explicit}".encode()).hexdigest()[:40]
        canon = dumps({"t": tool, "a": arguments or {}})
        return "h:" + hashlib.sha256(f"{tenant_id}|{canon}".encode()).hexdigest()[:40]

    def dedup_get(self, tenant_id: str, key: str) -> tuple[int | None, dict[str, Any]] | None:
        window = self.setting_int("dedup_window", 120)
        r = self.db.one("SELECT call_id, result, created_at FROM dedup WHERE tenant_id=? AND request_key=?", (tenant_id, key))
        if r is None:
            return None
        if key.startswith("h:") and time.time() - r["created_at"] > window:
            return None
        return r["call_id"], json.loads(r["result"])

    def dedup_put(self, tenant_id: str, key: str, call_id: int, result: dict[str, Any]) -> None:
        self.db.exec(
            "INSERT OR REPLACE INTO dedup(tenant_id,request_key,call_id,result,created_at) VALUES(?,?,?,?,?)",
            (tenant_id, key, call_id, dumps(result), time.time()),
        )

    def dedup_cleanup(self) -> int:
        cutoff = time.time() - 86400
        with self.db.tx() as c:
            cur = c.execute("DELETE FROM dedup WHERE created_at < ?", (cutoff,))
            return cur.rowcount

    # -- call log ----------------------------------------------------------------------------
    def log_call(self, tenant_id: str | None, rail: str, tool: str, duration_ms: float, ok: bool, error_kind: str | None, error_msg: str | None, charged: int, request_key: str | None, session_id: str | None, deduped: bool = False) -> int:
        return self.db.exec(
            "INSERT INTO calls(ts,tenant_id,rail,tool,duration_ms,ok,error_kind,error_msg,charged,request_key,session_id,deduped)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), tenant_id, rail, tool, round(duration_ms, 2), 1 if ok else 0, error_kind, (error_msg or "")[:500] or None, charged, request_key, session_id, 1 if deduped else 0),
        )

    # -- alarms ------------------------------------------------------------------------------
    def check_volume_alarm(self, tenant_id: str) -> None:
        threshold = self.setting_int("alarm.calls_per_hour", 300)
        n = int(self.db.val("SELECT COUNT(*) FROM calls WHERE tenant_id=? AND ts>=datetime('now','-1 hour')", (tenant_id,), 0))
        if n and n % 50 == 0 and n >= threshold:
            recent = self.db.one("SELECT 1 FROM alarms WHERE tenant_id=? AND kind='volume' AND ts>=datetime('now','-1 hour')", (tenant_id,))
            if recent is None:
                self.db.exec(
                    "INSERT INTO alarms(ts,tenant_id,kind,message) VALUES(?,?,?,?)",
                    (now_iso(), tenant_id, "volume", f"{n} calls in the last hour (alarm threshold {threshold})"),
                )
