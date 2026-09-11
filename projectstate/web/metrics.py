"""Dashboard queries. Cheap aggregations over calls/ledger/search_log; percentiles computed in Python."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import Database
from ..meter import TOOLS


def _days(n: int) -> list[str]:
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def _pct(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


class Metrics:
    def __init__(self, db: Database):
        self.db = db

    def calls_per_day(self, days: int = 30) -> list[dict[str, Any]]:
        rows = self.db.q(
            "SELECT substr(ts,1,10) d, COUNT(*) n, SUM(CASE WHEN ok=1 THEN 1 ELSE 0 END) ok, SUM(charged) rev, SUM(deduped) dd "
            "FROM calls WHERE ts >= ? GROUP BY d", (_days(days)[0],),
        )
        by = {r["d"]: r for r in rows}
        out = []
        for d in _days(days):
            r = by.get(d)
            out.append({"day": d, "calls": r["n"] if r else 0, "ok": r["ok"] if r else 0, "revenue": r["rev"] if r else 0, "deduped": r["dd"] if r else 0})
        return out

    def calls_per_week(self, weeks: int = 12) -> list[dict[str, Any]]:
        rows = self.db.q(
            "SELECT strftime('%Y-W%W', ts) w, COUNT(*) n, SUM(charged) rev FROM calls WHERE ts >= date('now', ?) GROUP BY w ORDER BY w",
            (f"-{weeks * 7} days",),
        )
        return [{"week": r["w"], "calls": r["n"], "revenue": r["rev"]} for r in rows]

    def per_tool(self, days: int = 7) -> list[dict[str, Any]]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.db.q("SELECT tool, duration_ms, ok, charged, deduped FROM calls WHERE ts >= ?", (since,))
        acc: dict[str, dict[str, Any]] = {t: {"tool": t, "calls": 0, "errors": 0, "revenue": 0, "durs": [], "deduped": 0} for t in TOOLS}
        for r in rows:
            a = acc.setdefault(r["tool"], {"tool": r["tool"], "calls": 0, "errors": 0, "revenue": 0, "durs": [], "deduped": 0})
            a["calls"] += 1
            a["errors"] += 0 if r["ok"] else 1
            a["revenue"] += r["charged"] or 0
            a["deduped"] += r["deduped"] or 0
            if r["duration_ms"] is not None and not r["deduped"]:
                a["durs"].append(float(r["duration_ms"]))
        out = []
        total = sum(a["calls"] for a in acc.values()) or 1
        for a in acc.values():
            d = sorted(a["durs"])
            out.append({
                "tool": a["tool"], "calls": a["calls"], "share": a["calls"] / total, "errors": a["errors"],
                "error_rate": (a["errors"] / a["calls"]) if a["calls"] else 0.0, "revenue": a["revenue"], "deduped": a["deduped"],
                "p50": _pct(d, 0.5), "p95": _pct(d, 0.95), "n_timed": len(d),
            })
        out.sort(key=lambda x: -x["calls"])
        return out

    def recent_errors(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.q("SELECT ts, tenant_id, rail, tool, error_kind, error_msg FROM calls WHERE ok=0 ORDER BY id DESC LIMIT ?", (limit,))]

    def error_kinds(self, days: int = 7) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.q(
            "SELECT error_kind, COUNT(*) n FROM calls WHERE ok=0 AND ts >= date('now', ?) GROUP BY error_kind ORDER BY n DESC", (f"-{days} days",))]

    def tenants(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.q(
            "SELECT t.id, t.kind, t.label, t.created_at, t.last_seen_at, w.balance, w.blocked, w.daily_call_cap, w.daily_spend_cap, w.per_minute_cap, "
            "(SELECT COUNT(*) FROM calls c WHERE c.tenant_id=t.id) AS calls_total, "
            "(SELECT COUNT(*) FROM calls c WHERE c.tenant_id=t.id AND c.ts >= date('now')) AS calls_today, "
            "(SELECT COUNT(*) FROM calls c WHERE c.tenant_id=t.id AND c.ts >= datetime('now','-1 hour')) AS calls_hour, "
            "(SELECT COALESCE(SUM(-amount),0) FROM ledger l WHERE l.tenant_id=t.id AND l.kind='charge') AS spent, "
            "(SELECT COALESCE(SUM(amount),0) FROM ledger l WHERE l.tenant_id=t.id AND l.kind='topup') AS paid "
            "FROM tenants t LEFT JOIN wallets w ON w.tenant_id=t.id ORDER BY t.last_seen_at DESC LIMIT ?", (limit,),
        )
        return [dict(r) for r in rows]

    def tenant(self, tenant_id: str) -> dict[str, Any] | None:
        rows = self.tenants(limit=100000)
        return next((r for r in rows if r["id"] == tenant_id), None)

    def ledger(self, tenant_id: str | None = None, limit: int = 200, kind: str | None = None) -> list[dict[str, Any]]:
        where, params = [], []
        if tenant_id:
            where.append("tenant_id=?")
            params.append(tenant_id)
        if kind:
            where.append("kind=?")
            params.append(kind)
        sql = "SELECT * FROM ledger" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in self.db.q(sql, (*params, limit))]

    def revenue_vs_cost(self, days: int = 30, monthly_cost: int = 0) -> list[dict[str, Any]]:
        daily_cost = monthly_cost // 30
        rows = self.db.q("SELECT substr(ts,1,10) d, SUM(-amount) rev FROM ledger WHERE kind='charge' AND ts >= ? GROUP BY d", (_days(days)[0],))
        by = {r["d"]: r["rev"] for r in rows}
        paid = {r["d"]: r["p"] for r in self.db.q("SELECT substr(ts,1,10) d, SUM(amount) p FROM ledger WHERE kind='topup' AND ts >= ? GROUP BY d", (_days(days)[0],))}
        return [{"day": d, "revenue": by.get(d, 0), "cost": daily_cost, "paid_in": paid.get(d, 0)} for d in _days(days)]

    def totals(self) -> dict[str, Any]:
        v = self.db.val
        return {
            "calls_today": v("SELECT COUNT(*) FROM calls WHERE ts >= date('now')", (), 0),
            "calls_7d": v("SELECT COUNT(*) FROM calls WHERE ts >= date('now','-7 days')", (), 0),
            "calls_30d": v("SELECT COUNT(*) FROM calls WHERE ts >= date('now','-30 days')", (), 0),
            "calls_prev_7d": v("SELECT COUNT(*) FROM calls WHERE ts >= date('now','-14 days') AND ts < date('now','-7 days')", (), 0),
            "revenue_today": v("SELECT COALESCE(SUM(-amount),0) FROM ledger WHERE kind='charge' AND ts >= date('now')", (), 0),
            "revenue_30d": v("SELECT COALESCE(SUM(-amount),0) FROM ledger WHERE kind='charge' AND ts >= date('now','-30 days')", (), 0),
            "revenue_total": v("SELECT COALESCE(SUM(-amount),0) FROM ledger WHERE kind='charge'", (), 0),
            "paid_in_30d": v("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE kind='topup' AND ts >= date('now','-30 days')", (), 0),
            "wallets_paid": v("SELECT COUNT(DISTINCT tenant_id) FROM ledger WHERE kind='topup'", (), 0),
            "settlements_30d": v("SELECT COUNT(*) FROM x402_payments WHERE status='settled' AND ts >= date('now','-30 days')", (), 0),
            "paid_in_total": v("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE kind='topup'", (), 0),
            "outstanding_balance": v("SELECT COALESCE(SUM(balance),0) FROM wallets", (), 0),
            "tenants": v("SELECT COUNT(*) FROM tenants", (), 0),
            "active_7d": v("SELECT COUNT(DISTINCT tenant_id) FROM calls WHERE ts >= date('now','-7 days') AND tenant_id IS NOT NULL", (), 0),
            "errors_7d": v("SELECT COUNT(*) FROM calls WHERE ok=0 AND ts >= date('now','-7 days')", (), 0),
            "deduped_7d": v("SELECT COUNT(*) FROM calls WHERE deduped=1 AND ts >= date('now','-7 days')", (), 0),
            "entries": v("SELECT COUNT(*) FROM entries WHERE deleted=0", (), 0),
            "projects": v("SELECT COUNT(*) FROM projects", (), 0),
        }

    def alarms(self, include_ack: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM alarms" + ("" if include_ack else " WHERE acknowledged=0") + " ORDER BY id DESC LIMIT 100"
        return [dict(r) for r in self.db.q(sql)]

    def volume_outliers(self, threshold_per_hour: int) -> list[dict[str, Any]]:
        """Tenants whose last-hour volume is over the threshold or > 5x their own hourly average over 7 days."""
        rows = self.db.q(
            "SELECT tenant_id, COUNT(*) n FROM calls WHERE ts >= datetime('now','-1 hour') AND tenant_id IS NOT NULL GROUP BY tenant_id"
        )
        out = []
        for r in rows:
            avg = self.db.val("SELECT COUNT(*)/168.0 FROM calls WHERE tenant_id=? AND ts >= datetime('now','-7 days')", (r["tenant_id"],), 0.0)
            if r["n"] >= threshold_per_hour or (avg >= 2 and r["n"] > 5 * avg):
                out.append({"tenant_id": r["tenant_id"], "calls_hour": r["n"], "avg_hour_7d": round(float(avg), 1)})
        return out

    def x402_payments(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.q("SELECT id, ts, payer, network, amount, tx, session_id, tool, status FROM x402_payments ORDER BY id DESC LIMIT ?", (limit,))]

    def recent_searches(self, limit: int = 40) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.q("SELECT ts, tenant_id, project_id, query, mode, n_candidates, n_returned, chars_returned FROM search_log ORDER BY id DESC LIMIT ?", (limit,))]
