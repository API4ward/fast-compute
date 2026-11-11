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

# If the domain is proxied by Cloudflare (orange cloud), trust Cloudflare's edge ranges and take
# the real client IP from CF-Connecting-IP (otherwise every visitor looks like a Cloudflare IP
# and the per-IP auth-failure rate limit would lump users together). BEHIND_CLOUDFLARE=0 to skip.
GLOBAL=""
if [ "${BEHIND_CLOUDFLARE:-1}" = "1" ]; then
  CF_RANGES="$(curl -fsSL https://www.cloudflare.com/ips-v4; echo; curl -fsSL https://www.cloudflare.com/ips-v6)"
  CF_RANGES="$(echo "$CF_RANGES" | tr -s ' \n' ' ')"
  if [ -n "$CF_RANGES" ]; then
    GLOBAL="{
	servers {
		trusted_proxies static ${CF_RANGES}
		client_ip_headers CF-Connecting-IP X-Forwarded-For
	}
}
"
  fi
fi

[ -f /etc/caddy/Caddyfile ] && cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak.$(date +%s)"
cat > /etc/caddy/Caddyfile <<EOF
# Managed by dockerserver/deploy/install-caddy.sh
${GLOBAL}
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
	# Host header is passed through unchanged; X-Forwarded-For/-Proto are set by Caddy (to the
	# real client IP when behind Cloudflare, see above) and trusted by uvicorn
	# (--proxy-headers --forwarded-allow-ips=127.0.0.1).
	reverse_proxy 127.0.0.1:8800 {
		# exactly one client IP for the app (the real visitor, resolved above)
		header_up X-Forwarded-For {client_ip}
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
