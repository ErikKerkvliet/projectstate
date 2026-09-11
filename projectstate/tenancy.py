"""Per-request tenant context (set by the metering middleware, read by tools)."""
from __future__ import annotations

import contextvars
from dataclasses import dataclass


@dataclass(frozen=True)
class Caller:
    tenant_id: str
    rail: str  # prepaid | x402 | admin
    label: str = ""


current_caller: contextvars.ContextVar[Caller | None] = contextvars.ContextVar("current_caller", default=None)


def require_caller() -> Caller:
    c = current_caller.get()
    if c is None:
        raise RuntimeError("no caller in context")
    return c
