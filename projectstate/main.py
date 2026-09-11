"""ASGI entry point: landing page, docs, admin dashboard and the x402-paid MCP endpoint."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .config import settings
from .db import get_db
from .mcp_app import Services, build_app

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("projectstate")

db = get_db()
svc = Services(db, settings)
mcp_server, mcp_app = build_app(svc)


@asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    async with mcp_server.session_manager.run():
        log.info("projectstate %s up at %s (mcp: %s, x402: %s)", __import__("projectstate").__version__, settings.base_url, settings.mcp_url, svc.x402.status()["mode"])
        yield


async def healthz(request: Request):
    db.val("SELECT 1")
    return JSONResponse({"ok": True, "x402": svc.x402.status()["enabled"]})


def build_routes():
    from .web import routes as web_routes

    return [
        Route("/healthz", healthz),
        *web_routes(svc),
        # `/x402/mcp` is kept as an alias of `/mcp` for clients configured before the rename
        Mount("/x402", app=mcp_app),
        Mount("/", app=mcp_app),
    ]


app = Starlette(routes=build_routes(), lifespan=lifespan)
