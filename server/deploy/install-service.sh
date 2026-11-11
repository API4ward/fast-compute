#!/bin/sh
# Install DockerServer as a systemd service (run on the host: sudo sh deploy/install-service.sh).
# Only the unit file goes to /etc/systemd/system (where systemd requires it); all configuration
# stays in the project's .env. A leftover /etc/dockerserver.env is no longer used and can be deleted.
set -eu
cd "$(dirname "$0")/.."
install -m 644 deploy/dockerserver.service /etc/systemd/system/dockerserver.service
# Configuration lives in the project: .env (mode 600, owned by ubuntu), created from .env.example.
[ -f .env ] || install -m 600 -o ubuntu -g ubuntu .env.example .env
# stop a manually started (nohup) instance, if any
pkill -u ubuntu -f '^/home/ubuntu/projects/dockerserver/.venv/bin/python3 .venv/bin/uvicorn' || true
sleep 1
systemctl daemon-reload
systemctl enable --now dockerserver
sleep 3
systemctl restart dockerserver
sleep 3
systemctl --no-pager --lines=5 status dockerserver
curl -fsS http://127.0.0.1:8800/health && echo
