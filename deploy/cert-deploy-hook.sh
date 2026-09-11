#!/bin/bash
# Point nginx at the freshly issued/renewed certificate and reload. Called by certbot's --deploy-hook.
set -euo pipefail
TARGET=${1:-${RENEWED_LINEAGE##*/}}
LIVE=${RENEWED_LINEAGE:-/etc/letsencrypt/live/$TARGET}
if [ -f "$LIVE/fullchain.pem" ]; then
  mkdir -p /etc/ssl/projectstate
  ln -sf "$LIVE/fullchain.pem" /etc/ssl/projectstate/fullchain.pem
  ln -sf "$LIVE/privkey.pem" /etc/ssl/projectstate/privkey.pem
  nginx -t && systemctl reload nginx
  echo "nginx now serving $LIVE"
else
  echo "no certificate at $LIVE" >&2
  exit 1
fi
