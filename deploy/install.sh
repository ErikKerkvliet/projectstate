#!/bin/bash
# Idempotent server setup. Run as root on the VPS from /opt/projectstate/app: bash deploy/install.sh
set -euo pipefail
APP=/opt/projectstate/app
install -m 644 $APP/deploy/projectstate.service /etc/systemd/system/projectstate.service
install -m 644 $APP/deploy/projectstate-backup.service /etc/systemd/system/projectstate-backup.service
install -m 644 $APP/deploy/projectstate-backup.timer /etc/systemd/system/projectstate-backup.timer
install -m 644 $APP/deploy/certbot-renew.service /etc/systemd/system/certbot-renew.service
install -m 644 $APP/deploy/certbot-renew.timer /etc/systemd/system/certbot-renew.timer
install -m 644 $APP/deploy/nginx-projectstate.conf /etc/nginx/sites-available/projectstate
install -m 644 $APP/deploy/nginx-projectstate-proxy.conf /etc/nginx/snippets/projectstate-proxy.conf
ln -sf /etc/nginx/sites-available/projectstate /etc/nginx/sites-enabled/projectstate
rm -f /etc/nginx/sites-enabled/default
install -m 644 $APP/deploy/fail2ban-jail.local /etc/fail2ban/jail.local
install -m 644 $APP/deploy/fail2ban-filter-projectstate-login.conf /etc/fail2ban/filter.d/projectstate-login.conf
mkdir -p /var/www/acme /var/backups/projectstate /opt/projectstate/data /etc/ssl/projectstate
if [ ! -e /etc/ssl/projectstate/fullchain.pem ]; then
  # placeholder self-signed certificate until deploy/issue-cert.sh installs the Let's Encrypt one
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -keyout /etc/ssl/projectstate/selfsigned-key.pem -out /etc/ssl/projectstate/selfsigned.pem \
    -subj "/CN=93.127.138.101" -addext "subjectAltName=IP:93.127.138.101" >/dev/null 2>&1
  ln -sf /etc/ssl/projectstate/selfsigned.pem /etc/ssl/projectstate/fullchain.pem
  ln -sf /etc/ssl/projectstate/selfsigned-key.pem /etc/ssl/projectstate/privkey.pem
fi
chmod 600 /etc/ssl/projectstate/selfsigned-key.pem 2>/dev/null || true
chown mcpstate:mcpstate /opt/projectstate/data
chmod 700 /var/backups/projectstate
[ -f $APP/.env ] || { cp $APP/deploy/env.example $APP/.env; echo "created $APP/.env from template — fill it in"; }
chown mcpstate:mcpstate $APP/.env; chmod 600 $APP/.env
# unattended upgrades
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOT'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Download-Upgradeable-Packages "1";
APT::Periodic::AutocleanInterval "7";
APT::Periodic::Unattended-Upgrade "1";
EOT
sed -i 's|^//\s*"${distro_id}:${distro_codename}-updates";|        "${distro_id}:${distro_codename}-updates";|' /etc/apt/apt.conf.d/50unattended-upgrades
sed -i 's|^//Unattended-Upgrade::Remove-Unused-Dependencies "false";|Unattended-Upgrade::Remove-Unused-Dependencies "true";|' /etc/apt/apt.conf.d/50unattended-upgrades
systemctl daemon-reload
systemctl enable --now projectstate-backup.timer certbot-renew.timer
systemctl enable projectstate
systemctl enable --now fail2ban
systemctl restart fail2ban
nginx -t && systemctl reload nginx
echo "install.sh done"
