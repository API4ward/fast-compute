#!/bin/sh
# Install Caddy (official apt repo) as an HTTPS reverse proxy in front of DockerServer.
# Run on the host:  sudo sh deploy/install-caddy.sh   (optionally DOCKERSERVER_DOMAIN=other.example.com)
#
# Prerequisites for the automatic Let's Encrypt certificate:
#   * DNS A record  $DOMAIN -> this host's public IPv4
#   * AWS security group: inbound TCP 80 and 443 open (port 8800 must stay closed)
# DockerServer itself stays bound to 127.0.0.1:8800.
set -eu
DOMAIN="${DOCKERSERVER_DOMAIN:-compute.llmfreedomforge.com}"

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

[ -f /etc/caddy/Caddyfile ] && cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak.$(date +%s)"
cat > /etc/caddy/Caddyfile <<EOF
# Managed by dockerserver/deploy/install-caddy.sh
${DOMAIN} {
	encode gzip
	request_body {
		# uploads are capped at 50 MB by the app; allow multipart overhead
		max_size 60MB
	}
	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options "nosniff"
		Referrer-Policy "no-referrer"
		-Server
	}
	# Host header is passed through unchanged; X-Forwarded-For/-Proto are set by Caddy
	# and trusted by uvicorn (--proxy-headers --forwarded-allow-ips=127.0.0.1).
	reverse_proxy 127.0.0.1:8800 {
		# long-running MCP calls (submit_job wait up to 60 s)
		transport http {
			read_timeout 120s
		}
	}
}
EOF
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl enable caddy
systemctl reload caddy || systemctl restart caddy
sleep 2
systemctl --no-pager --lines=5 status caddy
echo "Caddy serving https://${DOMAIN} -> 127.0.0.1:8800 (cert is issued once DNS points here and 80/443 are open)."
