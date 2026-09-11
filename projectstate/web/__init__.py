"""Web layer: user portal, OAuth login/consent UI, admin dashboard. Assembled in routes()."""
from __future__ import annotations


def routes(svc):
    from .app import build_web_routes

    return build_web_routes(svc)
