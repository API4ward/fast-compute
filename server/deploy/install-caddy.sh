#!/bin/sh
# Install Caddy (official apt repo) as the HTTPS reverse proxy for DockerServer, with the site
# config kept in the project: /etc/caddy/Caddyfile only imports deploy/Caddyfile.
# Run on the host:  sudo sh deploy/install-caddy.sh
# Later config edits: edit deploy/Caddyfile, then  sudo systemctl reload caddy
#
# Prerequisites for the automatic certificate: DNS for the domain points here (directly or via
# Cloudflare's proxy) and inbound TCP 80/443 are open. Port 8800 must stay closed.
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SITE="$REPO/deploy/Caddyfile"

if ! command -v caddy >/dev/null 2>&1; then
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y caddy
fi

# Caddy runs as user "caddy"; /home/ubuntu is 750, so let it read group-readable project files
# (the secrets file .env is 600 and stays unreadable to caddy).
OWNER_GROUP="$(stat -c %G "$REPO")"
usermod -aG "$OWNER_GROUP" caddy

[ -f /etc/caddy/Caddyfile ] && cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak.$(date +%s)"
printf '# Site config lives in the project:\nimport %s\n' "$SITE" > /etc/caddy/Caddyfile

caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl enable caddy
systemctl restart caddy   # restart (not reload) so the new group membership applies
sleep 2
systemctl --no-pager --lines=5 status caddy
echo "Caddy now serves the sites defined in $SITE"
