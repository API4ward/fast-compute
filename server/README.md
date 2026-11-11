# DockerServer

A small compute service: trusted users submit code + a command, and each job runs
in a fresh, locked-down Docker container. Step 1 of the project (users, tokens,
jobs, isolation). Credit billing, an MCP interface and an admin web page come later.

## Requirements

- Linux with Docker running; the service user must be in the `docker` group
  (`sudo usermod -aG docker $USER`, then log in again, or prefix commands with `sg docker -c "..."`).
- Python 3.11+ and `git` (used for `git_url` jobs).

## Setup

```bash
cd fast-compute/server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
docker pull python:3.12-slim

# create the first admin and print its token (shown once; only a hash is stored)
.venv/bin/python -m dockerserver.cli create-admin admin
```

## Run (localhost only)

```bash
.venv/bin/uvicorn dockerserver.main:app --host 127.0.0.1 --port 8800
```

Interactive API docs: http://127.0.0.1:8800/docs

Environment variables (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `DOCKERSERVER_DATA_DIR` | `./data` | SQLite DB + saved job logs |
| `DOCKERSERVER_DEFAULT_IMAGE` | `python:3.12-slim` | image when none given |
| `DOCKERSERVER_ALLOWED_IMAGES` | `python:3.12-slim` | comma-separated allowlist (`*` = any) |
| `DOCKERSERVER_MAX_UPLOAD_BYTES` | 50 MB | upload / workspace size cap |
| `DOCKERSERVER_MAX_LOG_BYTES` | 1 MB | saved log tail per job |
| `DOCKERSERVER_PIDS_LIMIT` | 256 | max processes per container |
| `DOCKERSERVER_TMP_SIZE` | `64m` | size of the `/tmp` tmpfs |
| `DOCKERSERVER_DEFAULT_CREDIT_HOURS` | `1000` | credits granted to each new user |
| `DOCKERSERVER_MONITOR_INTERVAL` | `10` | seconds between `docker stats` samples / credit checks |
| `DOCKERSERVER_MIN_START_SECONDS` | `60` | a job starts only if the balance covers this long at its size |
| `DOCKERSERVER_ALLOWED_HOSTS` | `127.0.0.1:8800,localhost:8800,compute.llmfreedomforge.com` | Host headers accepted by `/mcp` (`host:*` = any port) |
| `DOCKERSERVER_AUTH_FAIL_LIMIT` / `_WINDOW` | `20` / `300` | bad tokens per client IP per window before HTTP 429 |

## API

All endpoints except `/health` need `Authorization: Bearer <token>`.

**Admin**

| Method | Path | |
|---|---|---|
| POST | `/admin/users` | create user `{name, role?, max_cpus?, max_mem_mb?, max_run_seconds?, max_concurrent_jobs?, allow_network?}`; returns a first token |
| GET | `/admin/users` | list users |
| PATCH | `/admin/users/{id}` | change role / limits / `active` |
| POST | `/admin/users/{id}/disable` · `/enable` | disable/enable (disabled users' tokens stop working; running jobs continue) |
| POST | `/admin/users/{id}/suspend` | disable **and** kill all the user's running jobs (`killed_by_admin`) |
| GET | `/admin/jobs?user_id=&status=` | read-only job metadata across users (no command, code or logs) |
| GET/POST | `/admin/users/{id}/tokens` | list / issue tokens |
| DELETE | `/admin/tokens/{id}` | revoke a token |
| POST | `/admin/users/{id}/credits` | `{hours, reason: topup\|adjust, note}` (negative hours deduct) |
| GET | `/admin/users/{id}/ledger` | the user's balance history |
| GET | `/admin/usage?start=&end=` | per-user totals (jobs, run seconds, CPU-hours, GB-hours, cost) for jobs started in the range |

**Jobs** (everyone, admins included, sees and controls only their own jobs here)

| Method | Path | |
|---|---|---|
| POST | `/jobs` | multipart form: `command` (required), `file` (single file, .zip or .tar[.gz]), `git_url` (https), `image`, `cpus`, `mem_mb`, `timeout_seconds`, `network` |
| GET | `/jobs` | list my jobs (`?status=running`) |
| GET | `/jobs/{id}` | status, exit code, start/end times |
| GET | `/jobs/{id}/logs` | stdout+stderr (live while running) |
| POST | `/jobs/{id}/stop` | kill the job |
| GET | `/me` | my user, limits and `balance_hours` |
| GET | `/me/usage?start=&end=` | my jobs with cost + CPU/mem stats, totals and per-day totals |

Defaults per job: 1 CPU, 256 MB, the user's `max_run_seconds`; requests above
the user's limits are rejected. New users default to 1 CPU / 256 MB / 600 s / 2 concurrent jobs, no network.

Example:

```bash
TOKEN=ds_...
curl -H "Authorization: Bearer $TOKEN" \
     --form-string command="python main.py" \
     -F file=@main.py http://127.0.0.1:8800/jobs
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8800/jobs/1/logs
```

> Use `--form-string` for `command`: with `-F`, curl treats `;` as an option
> separator and silently truncates commands like `echo a; echo b`.

## Web pages

Served by the same app; plain HTML + vanilla JS (no build step), calling only the REST API.
Sign in by pasting a token (kept in the tab's `sessionStorage`, gone when the tab closes).

- **`/admin-ui`** (admin token): users (balance, limits, active; create → token shown once;
  disable/enable; edit limits/role; issue/revoke tokens), credit top-up/adjust with note,
  per-user ledger, suspend (disable + kill all the user's jobs), read-only recent jobs of all users
  (status/size/cost/CPU/memory — no commands or logs),
  usage report by date range (per-user CPU-hours, GB-hours, cost).
- **`/ui`** (any token): my balance, limits, jobs (with logs), usage per day.

## Configuration file

All configuration lives **in the project**, in `server/.env`
(git-ignored, owned by ubuntu, mode 600). The app reads it itself at startup
(`DOCKERSERVER_ENV_FILE` overrides the path) and its values override inherited environment
variables; the startup log prints `config file: ... (loaded)`. Every setting is documented in the
committed template `.env.example`:

```bash
cp .env.example .env && chmod 600 .env    # first time only; then edit .env
sudo systemctl restart dockerserver       # apply changes
```

## Run as a systemd service

Install (needs sudo): `sudo sh deploy/install-service.sh`

Only the unit file is copied to `/etc/systemd/system/` (systemd requires units there); it has no
`EnvironmentFile` — configuration stays in the project's `.env`, which the script creates from
`.env.example` if missing. The script stops a manually started instance, then
`systemctl enable --now dockerserver` and restarts it once as a check. The unit runs as `ubuntu`
with the `docker` group, on 127.0.0.1:8800, `Restart=on-failure`, `MemoryHigh=200M` /
`MemoryMax=350M`. Logs: `journalctl -u dockerserver -f`. (A leftover `/etc/dockerserver.env` from
an earlier version is no longer used and can be deleted.)

## Compute nodes (remote execution)

aws-london is the front door (API, MCP, web, billing); jobs can run on **compute nodes** — machines
running the `computenode` agent (its own repo), reachable over the tailnet only and
authenticated with a per-node Bearer token. The node enforces all container isolation itself.

Configure in the project's `.env` (see "Configuration file"). **Tokens go only there, never in git:**

```sh
DOCKERSERVER_NODES='[{"name":"mini","url":"http://100.126.20.127:8900","token_env":"DOCKERSERVER_NODE_TOKEN_MINI"}]'
DOCKERSERVER_NODE_TOKEN_MINI=<the node's COMPUTENODE_TOKEN>
```

Then check the node from this host and restart (`sudo systemctl restart dockerserver`):
`sh deploy/check-node.sh` (prints `/v1/info`; never prints the token).

Behaviour:

- **Mode:** with at least one node that has a token, jobs run only on nodes;
  `DOCKERSERVER_LOCAL_EXECUTION=true` adds this host's Docker as a last-resort fallback. With no
  usable node, jobs run locally (the original behaviour). A node whose token variable is unset is
  skipped with a warning.
- **Scheduling:** the node with the most free CPUs (then memory) that fits the job is tried first
  (`/v1/info`, cached `NODE_INFO_TTL`=5 s); a 409 moves on to the next node. If nothing fits now the
  job stays `queued` and is retried every `QUEUE_RETRY`=5 s for up to `QUEUE_TIMEOUT`=1 h. A job
  larger than every node is rejected at submit (HTTP 400). The job records which `node` ran it.
- **Monitoring / billing:** the node's job status supplies exit code, OOM and CPU/memory
  stats; billing uses the node-measured run time. Credit exhaustion, `stop_job` and
  `suspend_user` kill via `POST /v1/jobs/{id}/kill`; logs come from the node; after final billing
  the job is removed with `DELETE`. If a node can't be reached for `NODE_LOST_GRACE`=120 s the job
  becomes `lost` and is billed up to when the node was last seen.
- **Default limits for new users** are configurable: `DOCKERSERVER_DEFAULT_MAX_CPUS`,
  `_MAX_MEM_MB`, `_MAX_RUN_SECONDS`, `_MAX_CONCURRENT_JOBS` (existing users keep theirs; change
  them per user on the admin page / `set_user_limits`).

## Public access (HTTPS via Caddy)

aws-london has a public IP, so no tunnel is needed. DockerServer stays bound to
**127.0.0.1:8800**; [Caddy](https://caddyserver.com) terminates HTTPS on ports 80/443 (automatic
Let's Encrypt certificate) and reverse-proxies to it.

1. **DNS:** A record `compute.llmfreedomforge.com` → the host's public IPv4 (13.43.223.101 at the
   time of writing; check with `curl -s https://checkip.amazonaws.com`). Use an Elastic IP if the
   instance's IP may change.
2. **AWS security group:** inbound TCP **80** and **443** from anywhere (80 is needed for the
   certificate challenge and the HTTP→HTTPS redirect). Port **8800 must NOT be open**.
3. **Install Caddy** (official apt repo): `sudo sh deploy/install-caddy.sh`. The site config lives
   in the project, **`deploy/Caddyfile`**; `/etc/caddy/Caddyfile` only contains
   `import <path to server/>/deploy/Caddyfile` (the script also adds the `caddy`
   user to the `ubuntu` group so it can read it; `.env` stays 600). Edit `deploy/Caddyfile` and run
   `sudo systemctl reload caddy`. It reverse-proxies to 127.0.0.1:8800, keeps the Host header,
   sets HSTS / nosniff / no-referrer, caps request bodies at 60 MB and allows 120 s responses.
4. **Check:** `curl -sI https://compute.llmfreedomforge.com/health`; logs: `journalctl -u caddy -f`.

**Behind Cloudflare's proxy (orange cloud):** the domain resolves to Cloudflare, which forwards to
Caddy. `deploy/Caddyfile` trusts Cloudflare's published IP ranges and takes the visitor's address
from `CF-Connecting-IP`, passing exactly that IP to the app — otherwise all visitors would appear
as a few Cloudflare IPs and the per-IP auth-failure limit would lump them together.

The app is proxy-aware: uvicorn runs with `--proxy-headers --forwarded-allow-ips=127.0.0.1`, so the
client IP from Caddy's `X-Forwarded-For` is used for the auth-failure rate limit, and `/mcp`'s
host check accepts `compute.llmfreedomforge.com` (`DOCKERSERVER_ALLOWED_HOSTS`). Pages use only
relative URLs and a same-origin CSP, so they work unchanged over HTTPS.

Public endpoints:

- Web: https://compute.llmfreedomforge.com/ui and https://compute.llmfreedomforge.com/admin-ui
- REST: `https://compute.llmfreedomforge.com/jobs` etc. (`/docs` for the OpenAPI UI)
- MCP (users): `https://compute.llmfreedomforge.com/mcp`
- MCP (admins): `https://compute.llmfreedomforge.com/mcp-admin`

```bash
# everyone (run and manage your own jobs)
claude mcp add --transport http dockerserver https://compute.llmfreedomforge.com/mcp \
  --header "Authorization: Bearer ds_YOUR_TOKEN"
# admins only (users, limits, credits, usage, suspend)
claude mcp add --transport http dockerserver-admin https://compute.llmfreedomforge.com/mcp-admin \
  --header "Authorization: Bearer ds_ADMIN_TOKEN"
```

Only give tokens to people you trust: a token lets its holder run code (within their limits)
on this server.

## MCP interface

The server exposes two separate [MCP](https://modelcontextprotocol.io) services (Streamable
HTTP, stateless, JSON responses), using the same access tokens and the same service layer as
the REST API (identical permissions, limits and credit checks). Requests without a valid,
active token get HTTP 401; the host allowlist and auth-failure rate limit apply to both.

**`/mcp` — user service** (any token). Every caller, admins included, only ever sees and
controls their **own** jobs here.

| Tool | What |
|---|---|
| `submit_job` | `command` + either `files` (`{path: text}`) or `git_url`; optional `image`, `cpus`, `mem_mb`, `timeout_seconds`, `network`, `wait` (0–60 s: block and return status + log tail) |
| `list_jobs`, `get_job`, `get_job_logs` (`tail`), `stop_job` | my own jobs |
| `get_balance`, `get_usage` | my credits, limits, usage totals / per day |

**`/mcp-admin` — admin service** (admin tokens only; other tokens get HTTP 403).

| Tool | What |
|---|---|
| `list_users`, `create_user` (returns token once) | users |
| `set_user_status` | enable/disable an account (disabled: tokens rejected, no new jobs; running jobs continue) |
| `set_user_limits` | max CPUs / MB / run seconds / concurrent jobs, network, role |
| `suspend_user` | disable the account **and kill all of the user's running jobs** (status `killed_by_admin`, billed for the time they ran) |
| `list_user_jobs` | read-only job metadata (status, size, times, cost, CPU/mem) — no commands, code or logs |
| `add_credits`, `get_ledger`, `usage_report` | credits and usage |
| `issue_token`, `revoke_token` | tokens |

Admins cannot open, stop or read the logs of an individual user's job — not via MCP, REST or
the admin page. The only way to affect a user's running jobs is `suspend_user`.

**Claude Code** (replace HOST with `127.0.0.1` locally; publicly use https://compute.llmfreedomforge.com, see "Public access" above):

```bash
claude mcp add --transport http dockerserver http://HOST:8800/mcp \
  --header "Authorization: Bearer ds_YOUR_TOKEN"
claude mcp add --transport http dockerserver-admin http://HOST:8800/mcp-admin \
  --header "Authorization: Bearer ds_ADMIN_TOKEN"
```

**Claude Desktop** — add to `claude_desktop_config.json` (Settings → Developer → Edit Config).
Desktop talks to remote HTTP servers through the `mcp-remote` bridge (needs Node.js):

```json
{
  "mcpServers": {
    "dockerserver": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://HOST:8800/mcp",
               "--header", "Authorization: Bearer ${DOCKERSERVER_TOKEN}"],
      "env": { "DOCKERSERVER_TOKEN": "ds_YOUR_TOKEN" }
    }
  }
}
```

(If the server is reachable over HTTPS from the internet, it can instead be added in
Claude Desktop / claude.ai under Settings → Connectors → Add custom connector — but connectors
there authenticate with OAuth, not a static header, so the bridge above is the simple route
for now.) Use plain `http://` only on localhost or a private network; publicly, use the HTTPS URL
(see above).

Quick check without a client:

```bash
curl -s http://127.0.0.1:8800/mcp -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Isolation

Each job gets a new container and a new named volume, both deleted afterwards:

- `nano_cpus`, `mem_limit`, `memswap_limit = mem_limit` (no swap), `pids_limit`
- `cap_drop=ALL`, `no-new-privileges`, not privileged, runs as `nobody` (65534)
- read-only root filesystem; writable `/workspace` (job's own volume, holding its code) and a small `noexec` tmpfs on `/tmp`
- no host bind mounts, no Docker socket; network mode `none` unless the user has `allow_network` and the job asks for `network=true`
- wall-clock timeout, after which the container is killed (status `timeout`)
- uploads are re-packed: only regular files/dirs, no links, no absolute or `..` paths
- `git_url` jobs are shallow-cloned on the host (https only, hooks disabled) and copied in like an upload

Known limits: plain Docker isolation (shared kernel), so it is meant for trusted
users. Workspace volumes have no disk quota (only the upload is capped).

## Job statuses

`queued → preparing → running → succeeded | failed | timeout | stopped | out_of_credits | killed_by_admin | lost | error`.
On restart, the server marks jobs that were still active as `error` and removes leftover job containers/volumes.

## Credits

- **Unit:** 1 credit-hour = 1 CPU core + 256 MB RAM for 1 hour of wall-clock run time.
  A job costs `run_seconds / 3600 * max(cpus, mem_mb / 256)` hours, so it is charged for its
  *reserved* size (whichever dimension is larger), not its actual use.
- Balances are integers in micro-hours (`users.balance_uh`) and always equal the sum of the
  `ledger` table (`topup`, `adjust`, `job` entries, with admin id / job id / note).
- New users get `DEFAULT_CREDIT_HOURS` (1000) as an initial `topup`.
- **Before start:** the job is refused (HTTP 402) unless *available* credit — balance minus what the
  user's running jobs have used so far — covers `MIN_START_SECONDS` (60 s) at the requested size.
- **While running:** every `MONITOR_INTERVAL` seconds the runner samples `docker stats`
  (stored on the job as avg/peak CPU % — 100 = one core — and avg/peak memory MB) and checks the
  available credit; at zero the container is killed with status `out_of_credits`. Because of the
  check interval a balance can dip slightly below zero.
- **On finish:** the job is charged once (`billed` flag, claimed atomically → idempotent). On startup
  the server bills any finished-but-unbilled jobs and jobs that were left running across a restart
  (charged up to the restart time).
- Jobs shorter than one monitor interval have no stats samples.
- CLI: `python -m dockerserver.cli credits <name> <hours> [--note ...]`.

## Tests

```bash
sg docker -c ".venv/bin/python -m pytest -q"
```

Docker tests (a hello-world job that checks the container can't see host files, a timeout, a stop, a job stopped when credits run out)
are skipped automatically if Docker isn't reachable.

## Layout

```
dockerserver/
  config.py     settings from env
  models.py     SQLAlchemy models: User, Token, Job, LedgerEntry (+ tiny SQLite auto-migration)
  billing.py    credit math, ledger, idempotent job billing
  auth.py       token issue / hash / lookup
  workspace.py  upload/git → sanitized tar for /workspace
  runner.py     job orchestration: schedule, monitor, credits, finish, bill
  backends.py   execution backends: local Docker, remote compute node (HTTP)
  scheduler.py  node selection (most free capacity, 409 fallthrough, queueing)
  schemas.py    pydantic request/response models
  services.py   business logic shared by REST and MCP (permissions, limits, credits)
  api.py        FastAPI app (admin + jobs) + mounts /mcp
  mcp_server.py the two MCP services (/mcp user, /mcp-admin admin), bearer auth
  static/       admin.html (/admin-ui), user.html (/ui), common.js/css
deploy/         systemd unit, Caddyfile, install scripts, check-node.sh
.env.example    configuration template (copy to .env, git-ignored)
  main.py       ASGI entry point
  cli.py        create-admin / issue-token / list-users
tests/          pytest (API, billing, MCP-over-HTTP, web pages, real Docker tests)
```
