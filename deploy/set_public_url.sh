#!/bin/bash
# Set the public URL the server advertises (OAuth-free, but it is used in docs, x402 resource URLs and cookies).
#   sudo bash deploy/set_public_url.sh https://projectstate.nl
set -euo pipefail
URL=${1:-}
[[ "$URL" =~ ^https?://[^/]+$ ]] || { echo "usage: $0 https://your.domain   (no trailing path)"; exit 1; }
ENV=/opt/projectstate/app/.env
cp -p "$ENV" "$ENV.bak-$(date -u +%Y%m%d%H%M%S)"
sed -i "s|^BASE_URL=.*|BASE_URL=$URL|" "$ENV"
grep -q "^BASE_URL=" "$ENV" || echo "BASE_URL=$URL" >> "$ENV"
chown mcpstate:mcpstate "$ENV"; chmod 600 "$ENV"
systemctl restart projectstate
for i in $(seq 1 30); do curl -s -m 2 http://127.0.0.1:8000/healthz >/dev/null 2>&1 && break; sleep 1; done
echo "BASE_URL=$URL"; curl -s http://127.0.0.1:8000/healthz; echo
