#!/bin/bash
# Issue the TLS certificate for this server and point nginx at it.
#
#   sudo bash deploy/issue-cert.sh projectstate.nl      # domain: normal 90-day certificate (+ www)
#   sudo bash deploy/issue-cert.sh 93.127.138.101       # bare IP: Let's Encrypt IP certificate (6-day, profile 'shortlived')
#
# Requires port 80 to reach this machine from the internet (the ACME challenge is served from /var/www/acme).
# Running this accepts the Let's Encrypt Subscriber Agreement and registers an ACME account (no e-mail).
set -euo pipefail
TARGET=${1:-}
[ -n "$TARGET" ] || { echo "usage: $0 <domain|ip>"; exit 1; }
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
CERTBOT=/opt/certbot/bin/certbot
HOOK=/opt/projectstate/app/deploy/cert-deploy-hook.sh
mkdir -p /var/www/acme /etc/ssl/projectstate

echo "== reachability check on port 80"
mkdir -p /var/www/acme/.well-known/acme-challenge
token="preflight-$(date -u +%s)"
echo ok > "/var/www/acme/.well-known/acme-challenge/$token"
if ! curl -fsS -m 15 "http://$TARGET/.well-known/acme-challenge/$token" | grep -q ok; then
  rm -f "/var/www/acme/.well-known/acme-challenge/$token"
  echo "   FAILED: http://$TARGET/.well-known/acme-challenge/$token is not reachable from here."
  echo "   Port 80 must reach this VM from the internet and DNS must point at it. Fix that first;"
  echo "   Let's Encrypt rate-limits failed authorizations."
  exit 1
fi
rm -f "/var/www/acme/.well-known/acme-challenge/$token"
echo "   ok"

if [[ "$TARGET" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "== issuing IP certificate (6-day 'shortlived' profile)"
  $CERTBOT certonly --non-interactive --agree-tos --register-unsafely-without-email \
    --webroot -w /var/www/acme --ip-address "$TARGET" --preferred-profile shortlived --deploy-hook "$HOOK $TARGET"
else
  echo "== issuing certificate for $TARGET (and www, if it resolves here)"
  DOMAINS=(-d "$TARGET")
  if host "www.$TARGET" >/dev/null 2>&1 && curl -fsS -m 10 -o /dev/null "http://www.$TARGET/healthz"; then
    DOMAINS+=(-d "www.$TARGET")
    echo "   including www.$TARGET"
  fi
  $CERTBOT certonly --non-interactive --agree-tos --register-unsafely-without-email \
    --webroot -w /var/www/acme "${DOMAINS[@]}" --deploy-hook "$HOOK $TARGET"
fi

"$HOOK" "$TARGET"
echo "== done. Check: curl -sI https://$TARGET/healthz"
echo "   Remember to set BASE_URL=https://$TARGET in /opt/projectstate/app/.env and restart projectstate."
