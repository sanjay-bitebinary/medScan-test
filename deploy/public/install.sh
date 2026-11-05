#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/MedScan_V3}"
DOMAIN="${DOMAIN:-_}"

apt-get update
apt-get install -y ca-certificates curl nginx
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi

install -d -m 0755 "$APP_DIR"
install -m 0644 "$(dirname "$0")/docker-compose.yml" "$APP_DIR/deploy/public/docker-compose.yml"
install -m 0644 "$(dirname "$0")/nginx-medscan.conf" /etc/nginx/sites-available/medscan-v3
ln -sfn /etc/nginx/sites-available/medscan-v3 /etc/nginx/sites-enabled/medscan-v3
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx || systemctl restart nginx

echo "Copy the MedScan_V3 repository into $APP_DIR, create $APP_DIR/deploy/public/.env, then run:"
echo "  cd $APP_DIR && docker compose -f deploy/public/docker-compose.yml up -d --build"
echo "After DNS points to this host, issue TLS with: certbot --nginx -d $DOMAIN"
