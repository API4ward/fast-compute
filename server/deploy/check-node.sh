#!/bin/sh
# Check a compute node's API from this host:  sh deploy/check-node.sh [URL] [TOKEN_ENV_VAR]
# Defaults: URL http://100.126.20.127:8900 (mini), token from $DOCKERSERVER_NODE_TOKEN_MINI,
# falling back to that variable's line in /etc/dockerserver.env. The token is never printed
# and is passed to curl on stdin (not visible in the process list).
set -eu
URL="${1:-http://100.126.20.127:8900}"
VAR="${2:-DOCKERSERVER_NODE_TOKEN_MINI}"
TOKEN="$(printenv "$VAR" || true)"
if [ -z "$TOKEN" ] && [ -r /etc/dockerserver.env ]; then
  TOKEN="$(sed -n "s/^${VAR}=//p" /etc/dockerserver.env | tail -n 1 | sed "s/^[\"']//; s/[\"']\$//")"
fi
if [ -z "$TOKEN" ]; then
  echo "No token: set $VAR in the environment or in /etc/dockerserver.env" >&2
  exit 2
fi
echo "GET $URL/v1/info"
printf 'Authorization: Bearer %s\n' "$TOKEN" \
  | curl -sS --max-time 10 -H @- -w '\nHTTP %{http_code}\n' "$URL/v1/info"
