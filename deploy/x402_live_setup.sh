#!/bin/bash
# Switch the x402 rail to LIVE (Base mainnet via the Coinbase CDP facilitator).
# Run on the VPS as root:  sudo bash /opt/projectstate/app/deploy/x402_live_setup.sh
# Asks for your CDP API key id, CDP API key secret (hidden) and your receiving wallet, tests the keys against
# the facilitator BEFORE touching .env, then writes .env, restarts the service and verifies the rail comes up.
set -euo pipefail
APP=/opt/projectstate/app
ENV=$APP/.env
PY=/opt/projectstate/venv/bin/python
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

echo "== x402 live setup"
read -rp "CDP API key ID (from portal.cdp.coinbase.com -> API keys -> Secret API keys): " CDP_ID
read -rsp "CDP API key SECRET (Ed25519 base64 one-liner; input hidden): " CDP_SECRET; echo
read -rp "Receiving wallet address on Base mainnet (0x..., YOUR wallet): " PAY_TO
NETWORK=eip155:8453

echo "== preflight: address"
PAY_TO=$($PY - "$PAY_TO" <<'PYEOF'
import sys
from web3 import Web3
a = sys.argv[1].strip()
if not Web3.is_address(a):
    print("not a valid EVM address: " + a, file=sys.stderr); sys.exit(1)
print(Web3.to_checksum_address(a))
PYEOF
)
echo "   ok: $PAY_TO"

echo "== preflight: CDP keys against the Coinbase facilitator (authenticated /supported call)"
CDP_API_KEY_ID="$CDP_ID" CDP_API_KEY_SECRET="$CDP_SECRET" $PY - "$NETWORK" <<'PYEOF'
import os, sys
from cdp.x402 import create_facilitator_config
from x402.http import HTTPFacilitatorClient
cfg = dict(create_facilitator_config(os.environ["CDP_API_KEY_ID"], os.environ["CDP_API_KEY_SECRET"]))
client = HTTPFacilitatorClient(cfg)
try:
    headers = client._get_verify_headers()  # builds a JWT: fails fast on a malformed secret
except Exception as exc:
    print("   FAILED to build auth headers from the secret:", exc, file=sys.stderr); sys.exit(1)
try:
    sup = client.get_supported()
except Exception as exc:
    print("   FAILED: facilitator refused the keys:", exc, file=sys.stderr); sys.exit(1)
kinds = {(k.scheme, str(k.network)) for k in sup.kinds}
if ("exact", sys.argv[1]) not in kinds:
    print("   FAILED: facilitator does not list exact/" + sys.argv[1] + "; got " + str(sorted(kinds))[:300], file=sys.stderr); sys.exit(1)
print("   ok:", cfg["url"], "supports exact on", sys.argv[1])
PYEOF

echo "== writing .env (backup at $ENV.bak-$(date -u +%Y%m%d%H%M%S))"
cp -p "$ENV" "$ENV.bak-$(date -u +%Y%m%d%H%M%S)"
CDP_ID="$CDP_ID" CDP_SECRET="$CDP_SECRET" PAY_TO="$PAY_TO" NETWORK="$NETWORK" $PY - "$ENV" <<'PYEOF'
import os, sys
path = sys.argv[1]
values = {
    "X402_MODE": "live",
    "X402_NETWORK": os.environ["NETWORK"],
    "X402_PAY_TO": os.environ["PAY_TO"],
    "CDP_API_KEY_ID": os.environ["CDP_ID"],
    "CDP_API_KEY_SECRET": os.environ["CDP_SECRET"],
}
lines = open(path).read().splitlines()
seen = set()
out = []
for line in lines:
    key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
    if key in values:
        out.append(f"{key}={values[key]}"); seen.add(key)
    else:
        out.append(line)
for k, v in values.items():
    if k not in seen:
        out.append(f"{k}={v}")
open(path, "w").write("\n".join(out) + "\n")
PYEOF
chown mcpstate:mcpstate "$ENV"; chmod 600 "$ENV"
echo "   ok"

echo "== restarting projectstate"
systemctl restart projectstate
for i in $(seq 1 30); do curl -s -m 2 http://127.0.0.1:8000/healthz >/dev/null 2>&1 && break; sleep 1; done
curl -s http://127.0.0.1:8000/healthz; echo
LINE=$(journalctl -u projectstate --no-pager -n 40 -o cat | grep -E "x402 rail ready|x402 rail init failed" | tail -1)
echo "   $LINE"
case "$LINE" in
  *"rail ready: mode=live"*"api.cdp.coinbase.com"*) echo "== LIVE. Next: one real test payment with a hot wallet holding a little USDC on Base:";
     echo "   cd $APP && X402_DEMO_KEY=<hot wallet private key> $PY scripts/x402_agent_demo.py https://<public-url>/x402/mcp";
     echo "   then check /admin/ledger (status settled, tx on https://basescan.org) and /admin/users/x_<wallet>";;
  *) echo "== NOT live. Restore with: cp $ENV.bak-* $ENV && systemctl restart projectstate"; exit 1;;
esac
