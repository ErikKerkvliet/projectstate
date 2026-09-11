"""Mint an operator key: free, metered access to projectstate without a wallet or any payment.

    python scripts/make_key.py "my laptop agent"          # new isolated project space
    python scripts/make_key.py "second agent" --tenant key_ab12cd34ef56   # share an existing space
    python scripts/make_key.py --list                     # show live keys
    python scripts/make_key.py --revoke <token>           # revoke one

Give the printed key to an agent as `Authorization: Bearer ps_…`. Anyone holding it can use the
service for free within that tenant's caps, so treat it like a password and revoke it if it leaks.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

env_file = Path(__file__).resolve().parents[1] / ".env"
try:
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
except OSError:
    pass
os.environ["X402_MODE"] = "off"  # minting a key never talks to a facilitator

from projectstate.config import Settings  # noqa: E402
from projectstate.db import Database  # noqa: E402
from projectstate.mcp_app import Services  # noqa: E402

s = Settings()
svc = Services(Database(s.db_path), s)
args = sys.argv[1:]

if "--list" in args:
    rows = svc.db.q(
        "SELECT t.tenant_id, t.kind, t.label, t.created_at, t.last_used_at, t.revoked_at,"
        " (SELECT COUNT(*) FROM calls c WHERE c.tenant_id=t.tenant_id) AS calls"
        " FROM x402_credit_tokens t WHERE t.kind='comp' ORDER BY t.rowid DESC"
    )
    if not rows:
        print("no operator keys yet")
    for r in rows:
        state = "REVOKED" if r["revoked_at"] else "live"
        print(f"{state:8s} tenant={r['tenant_id']:22s} label={r['label'] or '-':24s} created={r['created_at'][:16]} "
              f"last_used={(r['last_used_at'] or '-')[:16]} calls={r['calls']}")
    raise SystemExit(0)

if "--revoke" in args:
    tok = args[args.index("--revoke") + 1]
    svc.x402.revoke_token(hashlib.sha256(tok.encode()).hexdigest())
    print("revoked (if it existed); the agent holding it now gets PaymentRequired")
    raise SystemExit(0)

label = next((a for a in args if not a.startswith("--")), "operator key")
tenant = args[args.index("--tenant") + 1] if "--tenant" in args else None
if tenant:
    if not svc.db.one("SELECT 1 FROM tenants WHERE id=?", (tenant,)):
        print(f"unknown tenant {tenant}")
        raise SystemExit(1)
else:
    tenant = svc.x402.new_operator_tenant(label)

token = svc.x402.issue_credit_token(tenant, kind="comp", label=label)
base = s.base_url
print(f"""
operator key created
  label   : {label}
  tenant  : {tenant}   (its own project space; reuse it with --tenant to share one)
  key     : {token}

Give it to an agent as a header:
  Authorization: Bearer {token}

Claude Code:
  claude mcp add --transport http projectstate {base}/mcp --header "Authorization: Bearer {token}"

Cursor / VS Code / Gemini CLI (mcp config):
  {{"mcpServers": {{"projectstate": {{"url": "{base}/mcp",
     "headers": {{"Authorization": "Bearer {token}"}}}}}}}}

Calls on this key are metered and capped but never charged. Revoke with:
  python scripts/make_key.py --revoke {token}
""")
