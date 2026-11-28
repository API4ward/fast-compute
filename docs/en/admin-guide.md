# Admin guide

[中文](../zh/admin-guide.md) · [User guide](user-guide.md) · [Deployment](deployment.md)

Admins manage users, limits, credits and tokens. By design, **admins cannot open, stop or read the
logs of an individual user's job** — not via the web page, REST or MCP. They see job *metadata*
only (status, node, size, times, cost, CPU/memory). The only way to affect a user's running jobs
is to **suspend** the user. An admin's own jobs work like any user's (via `/mcp` or `/jobs`).

## Tokens

- Tokens are random (`ds_…`), shown **once** when issued, and stored only as a SHA-256 hash —
  they cannot be recovered, only re-issued.
- A user can have several tokens (e.g. laptop, CI); revoke them individually.
- Disabling a user makes all of their tokens stop working.
- The first admin is created on the server with the CLI (below).

## Web page — https://compute.llmfreedomforge.com/admin-ui

Sign in with an admin token (kept only in that browser tab).

- **Users**: create (token shown once), open a user to edit limits and role, issue / revoke
  tokens, **Disable account** / **Enable account**, **Suspend (kill all jobs)**.
- **Credits** (inside a user): top-up or adjust with hours (negative deducts) and a note; the
  user's ledger (top-ups, adjustments, job charges, balance after each).
- **Jobs**: read-only list across users (filter by status).
- **Nodes**: every compute node from `DOCKERSERVER_NODES` (AWS Lambda nodes are marked "AWS Lambda" and show
  concurrency slots, per-job size, function, network and longest job instead of CPU/memory): online / offline / draining, when it was
  last seen and its latency, CPU and memory used / capacity as the node reports them (`/v1/info`),
  features, allowed images, the jobs this server has running there (metadata only) and 24 h counts
  (ok / failed / lost, CPU-hours). The page checks every node on open and every 20 s while it is
  shown; the server also checks every 30 s in the background (`DOCKERSERVER_NODE_HEARTBEAT`), so
  "unreachable since" is right even when nobody is looking. A warning appears when the node's own
  job count differs from this server's (e.g. a container left behind). **Drain** stops new jobs
  going to a node while its running jobs finish (use it before rebooting or updating a node);
  **Resume** puts it back. The draining state is kept in `server/data/nodes.json`. Admin MCP:
  `list_nodes`, `set_node_draining`.
- **Usage**: per-user jobs, run time, CPU-hours, GB-hours and cost for a date range.
- **Sites**: site containers per user (sites, awake containers, storage, requests, bytes served,
  awake hours, cost) and every site in detail (container `fc-site-<id>`, awake/asleep, files, last
  visit). Read-only; file contents are not shown. A user's page also lists their sites and has a
  **Delete user** button.
- **Caddy**: the HTTPS front door. Shows whether Caddy's admin API is up, whether the running
  config matches `server/deploy/Caddyfile`, and the running sites (hosts, upstreams; a ⚠ marks a
  host missing from `DOCKERSERVER_ALLOWED_HOSTS`, where connector sign-in would not answer). Edit
  the Caddyfile in place: **Validate** only parses it; **Apply** loads it into Caddy first (Caddy
  keeps the old config if the new one fails) and only then writes the file, keeping the previous
  file under **Backups** (last 30, in `server/data/caddy/backups/`). To roll back, **Load into
  editor** on a backup, then Apply. If the file was edited by hand on the host, **Load file into
  Caddy** applies it without sudo. Changing the main host still needs the matching
  `DOCKERSERVER_ALLOWED_HOSTS` / `PUBLIC_URL` edit in `.env` and a service restart.
  Validate and Apply always work on Caddy's whole root config (`/etc/caddy/Caddyfile`, setting
  `DOCKERSERVER_CADDY_ROOT`): the edit replaces only the `import` of our file, so sites imported from
  other projects' Caddyfiles stay. If the root file does not import our file, Apply is refused.

## Disable vs. suspend

| Action | Tokens | New jobs | Running jobs |
|---|---|---|---|
| Disable (`set_user_status active=false`, `POST /admin/users/{id}/disable`) | rejected (401) | blocked | **keep running** |
| Suspend (`suspend_user`, `POST /admin/users/{id}/suspend`) | rejected (401) | blocked | **killed** → `killed_by_admin`, billed for time run |
| Enable (`set_user_status active=true`, `POST /admin/users/{id}/enable`) | work again | allowed | — |

Admins cannot disable or suspend themselves.

**Delete user** (`delete_user`, `DELETE /admin/users/{id}`; the web page asks you to type the name):
kills the user's running jobs, stops and removes all their site containers and site files, deletes
their saved variables and revokes all tokens. Jobs and ledger entries are kept for accounting; the
user disappears from the list and the name can be reused. **Cannot be undone.** Admins cannot
delete themselves.

## Admin MCP — `/mcp-admin` (admin tokens only; others get 403)

```bash
claude mcp add --transport http dockerserver-admin https://compute.llmfreedomforge.com/mcp-admin \
  --header "Authorization: Bearer ds_ADMIN_TOKEN"
```

As a claude.ai / Claude Desktop custom connector: URL `https://compute.llmfreedomforge.com/mcp-admin`;
on the approval page paste an **admin** token (a user token is refused). The connector's OAuth token
works only on `/mcp-admin`.

| Tool | Purpose |
|---|---|
| `list_users` | all users with role, limits, active, balance |
| `create_user` | `name`, `role` (user/admin), optional `max_cpus`, `max_mem_mb`, `max_run_seconds`, `max_concurrent_jobs`, `allow_network` → includes the token |
| `set_user_status` | `user_id`, `active` |
| `set_user_limits` | `user_id` + any of the limits above, `role` |
| `suspend_user` | `user_id` → disabled + killed job ids |
| `list_user_jobs` | `user_id?`, `status?`, `limit` — metadata only |
| `add_credits` | `user_id`, `hours` (negative deducts), `reason` (`topup`/`adjust`), `note` |
| `get_ledger` | `user_id`, `limit` |
| `usage_report` | `start?`, `end?` (UTC) |
| `issue_token` | `user_id`, `name` → new token (shown once) |
| `revoke_token` | `token_id` |
| `delete_user` | `user_id` → killed jobs, deleted sites |
| `list_sites` | `user_id?` → every site's container state and usage (no file contents) |

## Admin REST API

| Method | Path | |
|---|---|---|
| POST | `/admin/users` | `{name, role?, max_cpus?, max_mem_mb?, max_run_seconds?, max_concurrent_jobs?, allow_network?}` → user + `token` |
| GET | `/admin/users`, `/admin/users/{id}` | list / one |
| PATCH | `/admin/users/{id}` | limits, `role`, `active` |
| POST | `/admin/users/{id}/disable` · `/enable` · `/suspend` | account status |
| GET / POST | `/admin/users/{id}/tokens` | list / issue (`{name}`) |
| DELETE | `/admin/tokens/{id}` | revoke |
| POST | `/admin/users/{id}/credits` | `{hours, reason: "topup"|"adjust", note}` |
| GET | `/admin/users/{id}/ledger?limit=` | ledger |
| GET | `/admin/jobs?user_id=&status=&limit=` | job metadata (no command, code or logs) |
| GET | `/admin/usage?start=&end=` | usage report |
| GET | `/admin/sites?user_id=` | sites, container state and usage |
| DELETE | `/admin/users/{id}` | delete a user with their sites and containers |

```bash
export DS=https://compute.llmfreedomforge.com ADMIN=ds_ADMIN_TOKEN
# create a user with bigger limits (response contains the token — give it to the user)
curl -s -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"name":"alice","max_cpus":4,"max_mem_mb":8192,"max_run_seconds":7200,"max_concurrent_jobs":3}' \
  $DS/admin/users
# top up 50 credit-hours
curl -s -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"hours":50,"reason":"topup","note":"November"}' $DS/admin/users/2/credits
# usage for November
curl -s -H "Authorization: Bearer $ADMIN" "$DS/admin/usage?start=2026-11-01T00:00:00&end=2026-12-01T00:00:00"
```

## Limits and defaults

- New users get `DOCKERSERVER_DEFAULT_CREDIT_HOURS` (1000) and the default limits from
  `server/.env`: `DOCKERSERVER_DEFAULT_MAX_CPUS` (1), `_MAX_MEM_MB` (256), `_MAX_RUN_SECONDS`
  (600), `_MAX_CONCURRENT_JOBS` (2). Changing defaults does not affect existing users.
- A job larger than every compute node is rejected even if the user's limits allow it.
- Network needs both the user's `allow_network` and `COMPUTENODE_ALLOW_NETWORK=true` on the node.
- Sites: at most `max_sites` per user (new users get `DOCKERSERVER_DEFAULT_MAX_SITES` = 3; 0 = no
  sites); change it on the user's page or with `set_user_limits`. Other site settings: see deployment.

## CLI (on the server, in `server/`)

```bash
.venv/bin/python -m dockerserver.cli create-admin [name]     # first admin; prints its token
.venv/bin/python -m dockerserver.cli issue-token <name>      # new token for an existing user
.venv/bin/python -m dockerserver.cli list-users              # users, limits, balances
.venv/bin/python -m dockerserver.cli credits <name> <hours> [--note TEXT]   # negative deducts
```
