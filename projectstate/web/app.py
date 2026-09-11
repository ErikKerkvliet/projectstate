"""Web routes: landing page, docs and the admin dashboard. No user accounts — callers are x402 wallets."""
from __future__ import annotations

import logging
import os
import secrets
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from ..config import usd_to_micro
from ..meter import TOOLS, fmt_usd, micro_to_usd
from .metrics import Metrics
from .passwords import verify_password
from . import svg

log = logging.getLogger("projectstate.web")
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["usd"] = lambda m: fmt_usd(int(m or 0))
TEMPLATES.env.filters["usd_short"] = lambda m: "$" + micro_to_usd(int(m or 0))
TEMPLATES.env.filters["pct"] = lambda f: f"{(f or 0) * 100:.1f}%"
TEMPLATES.env.filters["ms"] = lambda v: "-" if v is None else f"{v:.0f} ms"
COOKIE = "ps_session"


class _Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


class Web:
    def __init__(self, svc: Any):
        self.svc = svc
        self.db = svc.db
        self.settings = svc.settings
        self.meter = svc.meter
        self.metrics = Metrics(svc.db)
        self.signer = URLSafeTimedSerializer(self.settings.secret_key, salt="session")
        self.backup_dir = Path(os.environ.get("BACKUP_DIR", "/var/backups/projectstate"))

    # -- session helpers ----------------------------------------------------------------------
    def session(self, request: Request) -> dict[str, Any]:
        if hasattr(request.state, "sess"):
            return request.state.sess
        raw = request.cookies.get(COOKIE)
        data: dict[str, Any] = {}
        if raw:
            try:
                data = self.signer.loads(raw, max_age=14 * 86400)
            except BadSignature:
                data = {}
        data.setdefault("csrf", secrets.token_urlsafe(16))
        request.state.sess = data
        return data

    def save(self, request: Request, response: Response) -> Response:
        sess = self.session(request)
        response.set_cookie(COOKIE, self.signer.dumps(sess), max_age=14 * 86400, httponly=True, samesite="lax", secure=self.settings.base_url.startswith("https://"), path="/")
        return response

    def redirect(self, request: Request, url: str, msg: str | None = None, kind: str = "ok") -> Response:
        if msg:
            self.session(request)["flash"] = [kind, msg]
        return self.save(request, RedirectResponse(url, status_code=303))

    async def check_csrf(self, request: Request) -> dict[str, Any]:
        form = await request.form()
        if form.get("csrf") != self.session(request).get("csrf"):
            raise PermissionError("bad csrf token")
        return {k: (v if isinstance(v, str) else "") for k, v in form.items()}

    def render(self, request: Request, name: str, status: int = 200, **ctx: Any) -> Response:
        sess = self.session(request)
        flash = sess.pop("flash", None)
        base = {"request": request, "csrf": sess["csrf"], "flash": flash, "is_admin": bool(sess.get("admin")), "settings": self.settings, "base_url": self.settings.base_url}
        return self.save(request, TEMPLATES.TemplateResponse(request, name, {**base, **ctx}, status_code=status))

    def require_admin(self, request: Request) -> None:
        if not self.session(request).get("admin"):
            raise _Redirect("/admin/login")

    # -- public --------------------------------------------------------------------------------
    async def index(self, request: Request):
        return self.render(request, "index.html", prices={t: micro_to_usd(p) for t, p in self.meter.prices().items()},
                           x402=self.svc.x402.status(), pack=micro_to_usd(self.svc.x402.pack_price), minpay=micro_to_usd(self.svc.x402.min_payment))

    async def docs(self, request: Request):
        return self.render(request, "docs.html", prices={t: micro_to_usd(p) for t, p in self.meter.prices().items()},
                           x402=self.svc.x402.status(), pack=micro_to_usd(self.svc.x402.pack_price), minpay=micro_to_usd(self.svc.x402.min_payment))

    async def icon(self, request: Request):
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128"><rect width="128" height="128" rx="24" fill="#2563eb"/>'
            '<rect x="28" y="30" width="72" height="12" rx="6" fill="#fff"/><rect x="28" y="58" width="52" height="12" rx="6" fill="#fff" opacity=".85"/>'
            '<rect x="28" y="86" width="36" height="12" rx="6" fill="#fff" opacity=".7"/><circle cx="98" cy="92" r="10" fill="#fbbf24"/></svg>'
        )
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})

    # -- admin ---------------------------------------------------------------------------------
    async def admin_login(self, request: Request):
        if request.method == "POST":
            form = await self.check_csrf(request)
            ip = request.client.host if request.client else ""
            self.db.exec("DELETE FROM login_attempts WHERE ts < ?", (time.time() - 3600,))
            fails = self.db.val("SELECT COUNT(*) FROM login_attempts WHERE ip=? AND ok=0 AND ts > ?", (ip, time.time() - 900), 0)
            ok = fails < 10 and bool(self.settings.admin_password_hash) and form.get("username") == self.settings.admin_username and verify_password(form.get("password", ""), self.settings.admin_password_hash)
            self.db.exec("INSERT INTO login_attempts(ts,ip,ok) VALUES(?,?,?)", (time.time(), ip, 1 if ok else 0))
            if not ok:
                msg = "Wrong credentials." if self.settings.admin_password_hash else "Admin login is disabled: set ADMIN_PASSWORD_HASH in .env."
                return self.render(request, "admin_login.html", status=400, error=msg)
            self.session(request)["admin"] = True
            return self.redirect(request, "/admin")
        return self.render(request, "admin_login.html")

    async def admin_logout(self, request: Request):
        try:
            await self.check_csrf(request)
        except PermissionError:
            pass
        self.session(request).pop("admin", None)
        return self.redirect(request, "/admin/login", "Logged out.")

    async def admin_overview(self, request: Request):
        self.require_admin(request)
        m = self.metrics
        days = m.calls_per_day(30)
        weeks = m.calls_per_week(12)
        cost_month = self.meter.setting_int("monthly_cost", 0)
        rvc = m.revenue_vs_cost(30, cost_month)
        per_tool = m.per_tool(7)
        charts = {
            "calls_day": svg.bars([d["calls"] for d in days], [d["day"] for d in days]),
            "calls_week": svg.bars([w["calls"] for w in weeks], [w["week"] for w in weeks], color="#0891b2"),
            "rev_cost": svg.lines([("metered", [r["revenue"] / 1e6 for r in rvc], "#16a34a"), ("cost (VPS/30)", [r["cost"] / 1e6 for r in rvc], "#dc2626"), ("paid in (USDC)", [r["paid_in"] / 1e6 for r in rvc], "#6366f1")], [r["day"] for r in rvc], fmt=lambda v: f"${v:.2f}"),
            "tools": svg.hbars([(t["tool"], t["calls"]) for t in per_tool]),
        }
        return self.render(request, "admin_overview.html", totals=m.totals(), charts=charts, per_tool=per_tool, alarms=m.alarms(),
                           outliers=m.volume_outliers(self.meter.setting_int("alarm.calls_per_hour", 300)), cost_month=cost_month,
                           rev_30=sum(r["revenue"] for r in rvc), cost_30=sum(r["cost"] for r in rvc), x402=self.svc.x402.status(),
                           search=self.svc.store.search_quality(30), errors=m.error_kinds(7), top=sorted(m.tenants(500), key=lambda t: -t["calls_today"])[:8])

    async def admin_tools(self, request: Request):
        self.require_admin(request)
        if request.method == "POST":
            form = await self.check_csrf(request)
            for t in TOOLS:
                v = form.get(f"price_{t}")
                if v:
                    try:
                        self.meter.set_price(t, v)
                    except Exception:
                        return self.redirect(request, "/admin/tools", f"Invalid price for {t}.", "err")
            return self.redirect(request, "/admin/tools", "Prices updated; effective within 2 seconds, no restart.")
        days = int(request.query_params.get("days", "7"))
        return self.render(request, "admin_tools.html", per_tool=self.metrics.per_tool(days), prices={t: micro_to_usd(p) for t, p in self.meter.prices().items()}, days=days)

    async def admin_users(self, request: Request):
        self.require_admin(request)
        return self.render(request, "admin_users.html", tenants=self.metrics.tenants(), threshold=self.meter.setting_int("alarm.calls_per_hour", 300),
                           gcaps=(self.meter.setting_int("cap.daily_calls", 2000), self.meter.setting_int("cap.daily_spend", 5_000_000), self.meter.setting_int("cap.per_minute", 60)))

    async def admin_user(self, request: Request):
        self.require_admin(request)
        tid = request.path_params["tid"]
        t = self.metrics.tenant(tid)
        if t is None:
            return self.render(request, "message.html", status=404, title="Unknown wallet", text=tid)
        if request.method == "POST":
            form = await self.check_csrf(request)
            action = form.get("action")
            try:
                if action == "caps":
                    self.meter.set_caps(tid, int(form.get("daily_calls") or 0) or None, usd_to_micro(form.get("daily_spend") or "0") or None,
                                        int(form.get("per_minute") or 0) or None, blocked=form.get("blocked") == "on")
                    msg = "Caps updated."
                elif action == "credit":
                    amt = usd_to_micro(form.get("amount") or "0")
                    if amt == 0:
                        raise ValueError("amount 0")
                    self.meter.adjust(tid, amt, form.get("note") or "manual adjustment")
                    msg = f"Balance adjusted by {fmt_usd(amt)}."
                elif action == "revoke_tokens":
                    n = self.db.exec("DELETE FROM x402_credit_tokens WHERE tenant_id=?", (tid,))
                    self.svc.x402.forget_sessions(tid)
                    msg = "Credit tokens revoked; the wallet must pay again to re-bind (its balance is untouched)."
                else:
                    msg = "Nothing done."
            except (ValueError, InvalidOperation) as exc:
                return self.redirect(request, f"/admin/users/{tid}", f"Invalid input: {exc}", "err")
            return self.redirect(request, f"/admin/users/{tid}", msg)
        calls = [dict(r) for r in self.db.q("SELECT ts, rail, tool, duration_ms, ok, error_kind, error_msg, charged, deduped FROM calls WHERE tenant_id=? ORDER BY id DESC LIMIT 50", (tid,))]
        tokens = [dict(r) for r in self.db.q("SELECT created_at, last_used_at FROM x402_credit_tokens WHERE tenant_id=? ORDER BY rowid DESC LIMIT 20", (tid,))]
        payments = [p for p in self.metrics.x402_payments(500) if (p["payer"] or "") and tid.endswith(p["payer"])][:20]
        return self.render(request, "admin_user.html", t=t, ledger=self.metrics.ledger(tid, 100), calls=calls, projects=self.svc.store.list_projects(tid),
                           ledger_ok=self.meter.ledger_matches_wallet(tid), tokens=tokens, payments=payments)

    async def admin_errors(self, request: Request):
        self.require_admin(request)
        return self.render(request, "admin_errors.html", errors=self.metrics.recent_errors(200), kinds=self.metrics.error_kinds(7))

    async def admin_ledger(self, request: Request):
        self.require_admin(request)
        tid = request.query_params.get("tenant") or None
        kind = request.query_params.get("kind") or None
        return self.render(request, "admin_ledger.html", rows=self.metrics.ledger(tid, 500, kind), tenant=tid or "", kind=kind or "", x402=self.metrics.x402_payments(50))

    async def admin_search(self, request: Request):
        self.require_admin(request)
        return self.render(request, "admin_search.html", q30=self.svc.store.search_quality(30), q7=self.svc.store.search_quality(7), recent=self.metrics.recent_searches(60))

    async def admin_settings(self, request: Request):
        self.require_admin(request)
        keys = ["cap.daily_calls", "cap.daily_spend", "cap.per_minute", "x402.pack", "x402.min_payment", "monthly_cost", "alarm.calls_per_hour", "dedup_window"]
        money = {"cap.daily_spend", "x402.pack", "x402.min_payment", "monthly_cost"}
        if request.method == "POST":
            form = await self.check_csrf(request)
            try:
                for k in keys:
                    v = form.get(k)
                    if v is None or v == "":
                        continue
                    self.db.set_setting(k, str(usd_to_micro(v)) if k in money else str(int(v)))
            except (ValueError, InvalidOperation) as exc:
                return self.redirect(request, "/admin/settings", f"Invalid value: {exc}", "err")
            self.svc.x402.clear_cache()
            return self.redirect(request, "/admin/settings", "Settings saved (effective immediately).")
        current = {k: (micro_to_usd(int(self.db.get_setting(k, "0") or 0)) if k in money else self.db.get_setting(k, "")) for k in keys}
        return self.render(request, "admin_settings.html", current=current, x402=self.svc.x402.status(), env_path="/opt/projectstate/app/.env")

    async def admin_alarm_ack(self, request: Request):
        self.require_admin(request)
        await self.check_csrf(request)
        self.db.exec("UPDATE alarms SET acknowledged=1 WHERE id=?", (int(request.path_params["aid"]),))
        return self.redirect(request, "/admin", "Alarm acknowledged.")

    async def admin_system(self, request: Request):
        self.require_admin(request)
        backups = []
        if self.backup_dir.exists():
            for p in sorted(self.backup_dir.glob("**/*.db*"), reverse=True)[:30]:
                st = p.stat()
                backups.append({"name": f"{p.parent.name}/{p.name}", "size": st.st_size, "mtime": __import__("datetime").datetime.utcfromtimestamp(st.st_mtime).isoformat(timespec="minutes")})
        dbsize = self.settings.db_path.stat().st_size if self.settings.db_path.exists() else 0
        return self.render(request, "admin_system.html", backups=backups, backup_dir=str(self.backup_dir), dbsize=dbsize, x402=self.svc.x402.status(),
                           dedup_rows=self.db.val("SELECT COUNT(*) FROM dedup", (), 0), tokens=self.db.val("SELECT COUNT(*) FROM x402_credit_tokens", (), 0))

    async def admin_json(self, request: Request):
        self.require_admin(request)
        return JSONResponse({"totals": self.metrics.totals(), "per_tool": self.metrics.per_tool(7), "alarms": self.metrics.alarms(), "x402": self.svc.x402.status()})


def build_web_routes(svc: Any) -> list[Route]:
    w = Web(svc)

    def wrap(fn):
        async def handler(request: Request):
            try:
                return await fn(request)
            except _Redirect as r:
                return w.save(request, RedirectResponse(r.url, status_code=303))
            except PermissionError:
                return HTMLResponse("Invalid form token; reload the page and try again.", status_code=400)

        return handler

    r = lambda path, fn, methods=("GET",): Route(path, wrap(fn), methods=list(methods))
    return [
        r("/", w.index), r("/docs", w.docs), r("/icon.svg", w.icon),
        r("/admin/login", w.admin_login, ("GET", "POST")), r("/admin/logout", w.admin_logout, ("POST",)),
        r("/admin", w.admin_overview), r("/admin/tools", w.admin_tools, ("GET", "POST")),
        r("/admin/users", w.admin_users), r("/admin/users/{tid}", w.admin_user, ("GET", "POST")),
        r("/admin/errors", w.admin_errors), r("/admin/ledger", w.admin_ledger), r("/admin/search", w.admin_search),
        r("/admin/settings", w.admin_settings, ("GET", "POST")), r("/admin/alarms/{aid:int}/ack", w.admin_alarm_ack, ("POST",)),
        r("/admin/system", w.admin_system), r("/admin/api/summary", w.admin_json),
    ]
