#!/bin/sh
# Install DockerServer as a systemd service (run on the host: sudo sh deploy/install-service.sh).
# Only the unit file goes to /etc/systemd/system (where systemd requires it); all configuration
# stays in the project's .env. A leftover /etc/dockerserver.env is no longer used and can be deleted.
set -eu
cd "$(dirname "$0")/.."
REPO="$(pwd)"
sed "s#@REPO@#$REPO#g" deploy/dockerserver.service > /etc/systemd/system/dockerserver.service
chmod 644 /etc/systemd/system/dockerserver.service
# Configuration lives in the project: .env (mode 600, owned by ubuntu), created from .env.example.
[ -f .env ] || install -m 600 -o ubuntu -g ubuntu .env.example .env
# stop a manually started (nohup) instance, if any
pkill -u ubuntu -f 'bin/uvicorn dockerserver.main:app' || true
sleep 1
systemctl daemon-reload
systemctl enable --now dockerserver
sleep 3
systemctl restart dockerserver
sleep 3
systemctl --no-pager --lines=5 status dockerserver
# the app needs a few seconds to start
for i in $(seq 1 20); do curl -fsS http://127.0.0.1:8800/health 2>/dev/null && echo && exit 0; sleep 1; done
echo "health check failed: see journalctl -u dockerserver" >&2; exit 1
