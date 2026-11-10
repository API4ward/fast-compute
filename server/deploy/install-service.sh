#!/bin/sh
# Install DockerServer as a systemd service (run on the host: sudo sh deploy/install-service.sh).
set -eu
cd "$(dirname "$0")/.."
install -m 644 deploy/dockerserver.service /etc/systemd/system/dockerserver.service
[ -f /etc/dockerserver.env ] || install -m 640 -o root -g ubuntu deploy/dockerserver.env.example /etc/dockerserver.env
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
