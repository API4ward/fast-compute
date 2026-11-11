#!/bin/sh
# Install cloudflared from Cloudflare's official apt repository (does NOT start any tunnel).
# Run on the host:  sudo sh deploy/install-cloudflared.sh
# Afterwards create the tunnel in the Cloudflare dashboard (Zero Trust -> Networks -> Tunnels),
# add a public hostname compute.llmfreedomforge.com -> http://127.0.0.1:8800, and run the
# `sudo cloudflared service install <token>` command the dashboard shows.
set -eu
mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
  > /etc/apt/sources.list.d/cloudflared.list
apt-get update
apt-get install -y cloudflared
cloudflared --version
