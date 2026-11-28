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

The live bridge runs from `/home/ubuntu/projects/fast-compute/server`. The deploy scripts use
whatever directory they are run from (`install-service.sh` fills the path into the unit file;
`install-caddy.sh` imports that directory's `deploy/Caddyfile`), and a relative
`DOCKERSERVER_DATA_DIR` is resolved against `server/`.

```bash
cd server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env        # then edit .env
.venv/bin/python -m dockerserver.cli create-admin admin   # prints the first admin token
sudo sh deploy/install-service.sh             # systemd unit (only the unit file goes to /etc)
```

The service user needs access to the local Docker (`docker` group): static-site containers run on the bridge, and local job execution needs it too.

### Configuration — `server/.env`

All settings live in the project, in `.env` (git-ignored, mode 600). The app reads it at startup
(path override: `DOCKERSERVER_ENV_FILE`); its values override inherited environment variables, and
the log shows `config file: …/.env (loaded)`. Template with every key: `server/.env.example`.

| Setting | Default | |
|---|---|---|
| `DOCKERSERVER_DATA_DIR` | `data` (under `server/`) | SQLite DB, saved job logs, admin settings (`settings.json`) |
| `DOCKERSERVER_ALLOWED_IMAGES` / `DEFAULT_IMAGE` | `python:3.12-slim` | image allow-list / default |
| `DOCKERSERVER_MAX_UPLOAD_BYTES` | 50 MB | upload / workspace cap |
| `DOCKERSERVER_DEFAULT_CREDIT_HOURS` | 1000 | grant for new users |
| `DOCKERSERVER_MONITOR_INTERVAL` | 10 | seconds between usage samples / credit checks |
| `DOCKERSERVER_MIN_START_SECONDS` | 60 | balance needed to start (at the job's size) |
| `DOCKERSERVER_DEFAULT_MAX_CPUS` / `_MAX_MEM_MB` / `_MAX_RUN_SECONDS` / `_MAX_CONCURRENT_JOBS` | 1 / 256 / 600 / 2 | limits for new users |
| `DOCKERSERVER_ALLOWED_HOSTS` | `127.0.0.1:8800,localhost:8800,compute.llmfreedomforge.com` | Host headers accepted by `/mcp`, `/mcp-admin` |
| `DOCKERSERVER_AUTH_FAIL_LIMIT` / `_WINDOW` | 20 / 300 | bad tokens per client IP per window → 429 |
| `DOCKERSERVER_PUBLIC_URL` | `https://compute.llmfreedomforge.com` | OAuth issuer / resource URLs |
| `DOCKERSERVER_OAUTH` | `true` | OAuth for MCP custom connectors (claude.ai / Desktop) |
| `DOCKERSERVER_OAUTH_REDIRECT_HOSTS` | `claude.ai,claude.com` | allowed https redirect hosts at client registration (plus `http://localhost`) |
| `DOCKERSERVER_OAUTH_ACCESS_TTL` / `_REFRESH_TTL` | 3600 / 2592000 | OAuth token lifetimes (s) |
| `DOCKERSERVER_SECRET_KEY` | generated on first start | Fernet key encrypting users' environment variables and the secrets saved in `/admin-ui` → Settings (AWS secret access key, advisor API key in `data/settings.json`). Written into `.env` automatically if missing. **Back it up with `.env`; if it is lost, stored variables and saved secrets can't be decrypted** (users must set their variables again, admins re-enter the keys). |
| `DOCKERSERVER_NODES` | — | compute nodes (below) |
| `DOCKERSERVER_LOCAL_EXECUTION` | auto | also use local Docker (default: only when no node is usable) |
| `DOCKERSERVER_NODE_LOST_GRACE` | 120 | seconds unreachable before a job becomes `lost` |
| `DOCKERSERVER_QUEUE_RETRY` / `_QUEUE_TIMEOUT` | 5 / 3600 | retry interval / max wait for capacity |
| `DOCKERSERVER_NODE_INFO_TTL` / `_NODE_POLL_INTERVAL` | 5 / 2 | capacity cache / status poll (s) |
| `DOCKERSERVER_NODE_HEARTBEAT` | 30 | health probe of every node for the admin Nodes tab (s, 0 = off) |

Apply changes: `sudo systemctl restart dockerserver`. What admins save in `/admin-ui` → **Settings**
(AWS credentials, Lambda nodes, homepage advisor) applies without a restart and is kept in
`server/data/settings.json` (mode 600).

### Static sites

Site files live on the bridge in `server/data/sites/u<user id>/<site>/`. Each site is served by a
small `nginx:alpine` container on the bridge's own Docker (`fc-site-<id>`: read-only root, uid 101,
no capabilities, 32 MB, 0.25 CPU, the site directory mounted read-only) on the internal network
`fc-sites` (`internal`, inter-container traffic off): no internet, only the server process can reach
it. A request to `/sites/...` starts the container if needed and is proxied to it; after an idle
hour it is stopped. So the service user needs access to the bridge's Docker (`docker` group). The
first use pulls `nginx:alpine`.

User pages must not share an origin with `/ui` and `/admin-ui` (their JS could read same-origin
tokens), so sites get a host of their own: `DOCKERSERVER_SITES_URL`. That host serves only
`/sites/...`; `/sites/...` on any other host redirects (307) there.

| Setting | Default | |
|---|---|---|
| `DOCKERSERVER_SITES_URL` | empty (= `PUBLIC_URL`, not recommended) | public base of the sites, e.g. `https://sites-18-171-232-21.sslip.io` |
| `DOCKERSERVER_SITE_IDLE_SECONDS` | 3600 | stop a container after this many seconds without requests |
| `DOCKERSERVER_SITE_MAX_AWAKE` | 20 | containers awake at once (least recently used are stopped first) |
| `DOCKERSERVER_SITE_MAX_FILE_BYTES` / `_MAX_BYTES` / `_MAX_FILES` | 5 MB / 50 MB / 1000 | per file / per site / files per site |
| `DOCKERSERVER_DEFAULT_MAX_SITES` | 3 | sites per new user |
| `DOCKERSERVER_SITE_CREDIT_PER_HOUR` | 0 | credit-hours charged per awake hour (0 = free; usage still recorded) |
| `DOCKERSERVER_SITE_IMAGE` / `_NETWORK` | `nginx:alpine` / `fc-sites` | image / Docker network |

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

#### AWS Lambda node

A Lambda node sends jobs to an AWS Lambda function (one invocation per job) instead of a node
agent. `lambda-node/deploy.sh` creates the function, its bucket and roles and an IAM user for the
server (`LOGS=1` also lets the function write CloudWatch logs). The easiest way to connect it is
`/admin-ui` → **Settings**: save the AWS access key, add the node, run Diagnose and a test job;
it takes jobs at once, no restart (see the [admin guide](admin-guide.md#settings)). In
`server/.env` it is a `DOCKERSERVER_NODES` entry with `"type": "lambda"` (shown read-only in
Settings): `{"name":"aws-lambda","type":"lambda","function":"fast-compute-job",
"bucket":"…","region":"eu-west-2","max_concurrency":10,"network":"none"}`, with
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (or `"profile"`). A key saved in Settings takes
precedence over the `AWS_*` variables, a node's `profile` over both. The server needs `boto3`
(in `requirements.txt`). Lambda nodes get jobs only when no Docker compute node has room (this
host's own Docker, when it runs jobs, comes after all nodes, Lambda included);
a job must fit one invocation (function memory, 1 vCPU per 1769 MB), its timeout must be at most
the function timeout − 30 s (≤ 870 s), its image `python:3.12-slim`, and its network flag must
match the function (`none` = isolated VPC, `internet`). Set-up script, design, limits and
troubleshooting: [`lambda-node/README.md`](../../lambda-node/README.md).

### OAuth for MCP connectors

claude.ai and Claude Desktop custom connectors authenticate with MCP OAuth 2.1 instead of a header.
The server implements it with the MCP SDK's auth handlers: `/.well-known/oauth-protected-resource/mcp`
(and `/mcp-admin`), `/.well-known/oauth-authorization-server`, `/register` (dynamic registration;
redirect URIs limited to `OAUTH_REDIRECT_HOSTS`), `/authorize` (PKCE S256) → `/oauth/consent` (the
user pastes a `ds_` token; CSRF-protected, rate-limited), `/token`, `/revoke`. 401 responses from
`/mcp` carry `WWW-Authenticate: Bearer resource_metadata=…`. OAuth tokens (`dsa_` access 1 h,
`dsr_` refresh 30 days, rotating) are stored hashed, bound to user + approving `ds_` token + client +
resource, and are revoked when the user is disabled/suspended or that `ds_` token is revoked. They
only work on the MCP endpoint they were issued for, not on the REST API. `Bearer ds_…` keeps working.

Jobs that carry environment variables are sent only to nodes whose `/v1/info` lists
`"features": ["env"]` (node agent with env support); if no node supports it, such jobs are rejected with a
clear error. Update every node's agent (`git pull` in `node/` and restart `./start.sh`).

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
- Edit it from `/admin-ui` → **Caddy** (validate, apply, backups; uses Caddy's local admin API at
  `127.0.0.1:2019`, no sudo). By hand: edit the file, then `sudo systemctl reload caddy` or the
  admin page's **Load file into Caddy**. Settings: `DOCKERSERVER_CADDYFILE` (default
  `deploy/Caddyfile`), `DOCKERSERVER_CADDY_ADMIN` (default `http://127.0.0.1:2019`, loopback only),
  `DOCKERSERVER_CADDY_MANAGE=false` turns the feature off. Edits made in the page leave
  `deploy/Caddyfile` modified in the git checkout; commit them (or `git stash`) before `git pull`.
- DNS: the domain is proxied by Cloudflare. The Caddyfile trusts Cloudflare's IP ranges and takes
  the visitor IP from `CF-Connecting-IP` (needed for the per-IP auth-failure limit). If the
  Cloudflare proxy is turned off, point the A record at the bridge's public IP and remove those lines.
- The sites host (`DOCKERSERVER_SITES_URL`) has its own block in the Caddyfile that forwards only
  `/sites/*`; Caddy gets its certificate automatically. The default uses sslip.io
  (`sites-<ip-with-dashes>.sslip.io`, no DNS needed); if the public IP changes, update the Caddyfile and
  `.env`. To use your own name (e.g. `sites.llmfreedomforge.com`): add an A record (Cloudflare proxy is
  fine), change the Caddyfile site name and `DOCKERSERVER_SITES_URL`, reload Caddy, restart the service.
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
bills anything unbilled. Back up `server/data/` (the SQLite database `dockerserver.db`,
`settings.json`, …) together with `server/.env`, whose `DOCKERSERVER_SECRET_KEY` decrypts the
saved secrets.
