"""Give an x402 wallet credit without a payment (admin/testing) and print a credit token.

usage: python scripts/grant_credit.py 0.50 [0xWALLET]
Without an address a throwaway one is generated. Reads DB_PATH from .env.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

env_file = Path(__file__).resolve().parents[1] / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
os.environ["X402_MODE"] = "off"  # no facilitator needed to hand out credit

from eth_account import Account  # noqa: E402

from projectstate.config import Settings, usd_to_micro  # noqa: E402
from projectstate.db import Database  # noqa: E402
from projectstate.mcp_app import Services  # noqa: E402

amount = sys.argv[1] if len(sys.argv) > 1 else "0.10"
address = (sys.argv[2] if len(sys.argv) > 2 else Account.create().address).lower()
s = Settings()
svc = Services(Database(s.db_path), s)
tenant = f"x_{address}"
svc.db.ensure_tenant(tenant, "x402", address)
svc.meter.credit(tenant, "x402", usd_to_micro(amount), ref=f"grant:{address}:{time.time_ns()}", note="manual grant")
token = svc.x402.issue_credit_token(tenant)
print("tenant:", tenant)
print("balance:", svc.meter.balance(tenant) / 1e6, "USD")
print("credit token:", token)
print("use as: Authorization: Bearer", token)
