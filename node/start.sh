#!/bin/sh
# Start this machine as a DockerServer compute node. Safe to run repeatedly.
set -e
cd "$(dirname "$0")"
PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  python3 -m venv .venv
  .venv/bin/pip -q install -r requirements.txt
fi
[ -f "$HOME/.computenode/config.env" ] || $PY -m computenode init

# Docker Desktop runs under the desktop user (gentle); use its socket unless DOCKER_HOST is set.
export DOCKER_HOST="${DOCKER_HOST:-unix:///Users/gentle/.docker/run/docker.sock}"
if ! docker info >/dev/null 2>&1; then
  echo "Cannot reach Docker at $DOCKER_HOST (is Docker Desktop running, and does this user have access?)" >&2
  exit 1
fi
exec $PY -m computenode serve
