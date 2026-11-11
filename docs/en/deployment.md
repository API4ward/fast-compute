# Deployment

[中文](../zh/deployment.md) · [User guide](user-guide.md) · [Admin guide](admin-guide.md)

## Architecture

```
users / AI assistants ──HTTPS──▶ Cloudflare ──▶ Caddy :443 ──▶ DockerServer 127.0.0.1:8800
                                                (aws-london, the bridge: API, MCP, web, billing)
                                                         │  HTTP + Bearer token, tailnet only
                                                         ▼
                                     compute node agent :8900  (e.g. mini, 100.126.20.127)
                                     └─ one locked-down Docker container per job
```

- **Bridge** (`server/`, runs on aws-london): users, tokens, credits, scheduling, MCP, web pages.
  It listens only on 127.0.0.1:8800; Caddy terminates HTTPS. Port 8800 must not be public.
- **Compute nodes** (`node/`): run jobs. They listen only on their Tailscale IP and accept only the
  bridge's IP plus a per-node token. Container isolation is enforced on the node itself.
- Without a usable node, the bridge runs jobs on its own Docker (fallback / development).

## Bridge (`server/`)

The live bridge currently runs from `/home/ubuntu/projects/dockerserver` (same code as `server/`);
`deploy/dockerserver.service` and `deploy/Caddyfile` contain that path — adjust them if you deploy
from another directory.

```bash
cd server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env        # then edit .env
.venv/bin/python -m dockerserver.cli create-admin admin   # prints the first admin token
sudo sh deploy/install-service.sh             # systemd unit (only the unit file goes to /etc)
```

The service user needs access to Docker only if local execution is used (`docker` group).

### Configuration — `server/.env`

All settings live in the project, in `.env` (git-ignored, mode 600). The app reads it at startup
(path override: `DOCKERSERVER_ENV_FILE`); its values override inherited environment variables, and
the log shows `config file: …/.env (loaded)`. Template with every key: `server/.env.example`.

| Setting | Default | |
|---|---|---|
| `DOCKERSERVER_DATA_DIR` | `./data` | SQLite DB and saved job logs |
| `DOCKERSERVER_ALLOWED_IMAGES` / `DEFAULT_IMAGE` | `python:3.12-slim` | image allow-list / default |
| `DOCKERSERVER_MAX_UPLOAD_BYTES` | 50 MB | upload / workspace cap |
| `DOCKERSERVER_DEFAULT_CREDIT_HOURS` | 1000 | grant for new users |
| `DOCKERSERVER_MONITOR_INTERVAL` | 10 | seconds between usage samples / credit checks |
| `DOCKERSERVER_MIN_START_SECONDS` | 60 | balance needed to start (at the job's size) |
| `DOCKERSERVER_DEFAULT_MAX_CPUS` / `_MAX_MEM_MB` / `_MAX_RUN_SECONDS` / `_MAX_CONCURRENT_JOBS` | 1 / 256 / 600 / 2 | limits for new users |
| `DOCKERSERVER_ALLOWED_HOSTS` | `127.0.0.1:8800,localhost:8800,compute.llmfreedomforge.com` | Host headers accepted by `/mcp`, `/mcp-admin` |
| `DOCKERSERVER_AUTH_FAIL_LIMIT` / `_WINDOW` | 20 / 300 | bad tokens per client IP per window → 429 |
| `DOCKERSERVER_NODES` | — | compute nodes (below) |
| `DOCKERSERVER_LOCAL_EXECUTION` | auto | also use local Docker (default: only when no node is usable) |
| `DOCKERSERVER_NODE_LOST_GRACE` | 120 | seconds unreachable before a job becomes `lost` |
| `DOCKERSERVER_QUEUE_RETRY` / `_QUEUE_TIMEOUT` | 5 / 3600 | retry interval / max wait for capacity |
| `DOCKERSERVER_NODE_INFO_TTL` / `_NODE_POLL_INTERVAL` | 5 / 2 | capacity cache / status poll (s) |

Apply changes: `sudo systemctl restart dockerserver`.

### Compute nodes on the bridge

```sh
DOCKERSERVER_NODES='[{"name":"mini","url":"http://100.126.20.127:8900","token_env":"DOCKERSERVER_NODE_TOKEN_MINI"}]'
DOCKERSERVER_NODE_TOKEN_MINI=<the node's COMPUTENODE_TOKEN>
```

Keep the single quotes around the JSON. A node whose token variable is empty is skipped (warning
in the log). Check from the bridge: `sh deploy/check-node.sh` (prints `/v1/info`, never the token).
After a restart the log shows `execution: compute nodes=['mini@http://…'], local docker=off`.

Scheduling: most free CPUs (then memory) first among nodes that fit; HTTP 409 → next node; nothing
fits → `queued` and retried; larger than every node → rejected at submit. Billing uses the
node-measured run time; jobs are deleted on the node after billing.

## Compute node (`node/`)

Requirements: Docker, Python 3, Tailscale (the node binds to its tailnet IP).

```bash
cd node
./start.sh          # first run: creates .venv and config.env with a fresh token (mode 600)
.venv/bin/python -m computenode token    # print the token → put it in the bridge's .env
```

`node/config.env` (git-ignored; all keys in `node/computenode/config.py`):

| Setting | Default (template) | |
|---|---|---|
| `COMPUTENODE_TOKEN` | generated | Bearer token the bridge uses |
| `COMPUTENODE_LISTEN` / `_PORT` | `tailscale` / 8900 | bind to the tailnet IP |
| `COMPUTENODE_ALLOWED_CLIENTS` | `100.82.2.33` | source IPs allowed (the bridge) |
| `COMPUTENODE_ALLOWED_IMAGES` | `python:3.12-slim` | |
| `COMPUTENODE_ALLOW_NETWORK` | `false` | allow jobs to request network |
| `COMPUTENODE_RESERVE_CPUS` / `_RESERVE_MEM_MB` | 2 / 8192 | kept free for the machine itself |
| `COMPUTENODE_MAX_CPUS` / `_MAX_MEM_MB` | 0 = Docker total − reserve | capacity offered |
| `COMPUTENODE_MAX_TIMEOUT_SECONDS` | 86400 | |
| `COMPUTENODE_RETAIN_SECONDS` | 3600 | uncollected finished jobs are removed after this |

`start.sh` uses `DOCKER_HOST` if set (on mini it defaults to the Docker Desktop socket of the
desktop user). Node API: see `node/README.md`.

## HTTPS — Caddy

- Site config is in the project: `server/deploy/Caddyfile`. `/etc/caddy/Caddyfile` only contains
  `import <path>/deploy/Caddyfile`; `sudo sh deploy/install-caddy.sh` sets this up (installs Caddy
  from its official apt repo if needed and adds the `caddy` user to the owner's group to read it).
- After editing: `sudo systemctl reload caddy`.
- DNS: the domain is proxied by Cloudflare. The Caddyfile trusts Cloudflare's IP ranges and takes
  the visitor IP from `CF-Connecting-IP` (needed for the per-IP auth-failure limit). If the
  Cloudflare proxy is turned off, point the A record at the bridge's public IP and remove those lines.
- AWS security group: inbound TCP 80 and 443 only.

## Operations

| Task | Command |
|---|---|
| Status / logs (bridge) | `systemctl status dockerserver`, `journalctl -u dockerserver -f` |
| Caddy logs | `journalctl -u caddy -f` |
| Node reachable? | `sh deploy/check-node.sh` |
| Health | `curl https://compute.llmfreedomforge.com/health` |
| Update the bridge | `git pull`, `.venv/bin/pip install -r requirements.txt`, `sudo systemctl restart dockerserver` |
| Update a node | `git pull`, restart `./start.sh` (running jobs on that node are killed) |
| Tests | `cd server && .venv/bin/python -m pytest -q` (Docker tests are skipped without Docker access) |

On restart the bridge finishes jobs left active (kills them on the node, marks them `error`) and
bills anything unbilled. The SQLite database is `server/data/dockerserver.db` — back it up.
