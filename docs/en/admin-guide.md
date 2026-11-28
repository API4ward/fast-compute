# Admin guide

[中文](../zh/admin-guide.md) · [User guide](user-guide.md) · [Deployment](deployment.md)

Admins manage users, limits, credits and tokens, and the server's settings (AWS Lambda compute
nodes, the homepage advisor). By design, **admins cannot open, stop or read the
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

Sign in with an admin token (kept only in that browser tab). The tabs are in a sidebar on a wide
screen and in a scrollable bar at the top on a phone.

**Language**: the page is in English and Chinese. The **中文 / EN** button (on the sign-in card,
and next to **Sign out**) switches it and reloads the page; you stay signed in. The choice
is saved in the browser and shared with the homepage and the user page `/ui`; the first visit
follows the browser's language. Messages that come from the server (errors, diagnostic details
and hints) stay in English.

- **Users**: create (token shown once), open a user to edit limits and role, issue / revoke
  tokens, **Disable account** / **Enable account**, **Suspend (kill all jobs)**.
- **Credits** (inside a user): top-up or adjust with hours (negative deducts) and a note; the
  user's ledger (top-ups, adjustments, job charges, balance after each).
- **Jobs**: read-only list across users (filter by status).
- **Nodes**: every compute node, from `DOCKERSERVER_NODES` or added under **Settings** (a small tag
  shows `.env` or `admin`; AWS Lambda nodes are marked "AWS Lambda", show concurrency slots,
  per-job size, function, network and longest job instead of CPU/memory, and have a **Settings**
  button that opens their settings): online / offline / draining, when it was
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
- **Settings**: AWS account, Lambda nodes (with Diagnose, test job and function logs), Deploy
  helper and Homepage advisor — see the next section.

## Settings

Set up and debug AWS Lambda compute nodes and the homepage advisor without editing `server/.env`
or restarting: every change applies at once, and the cards show who changed what last and when.
What is saved lives in `server/data/settings.json` (mode 600); the AWS secret access key and the
advisor's API key are encrypted with `DOCKERSERVER_SECRET_KEY` (the key in `server/.env` that
also encrypts users' variables), are never shown again, never returned by the API and never
logged. Back up `.env` together with `data/`: without that key, saved secrets must be entered
again. The `server/.env` settings keep working; where both exist, Settings wins (except a Lambda
node's own `profile`, below).

**Upgrading from the interim "AWS Lambda" tab**: on the first start after the upgrade, the server
carries over the AWS access key and the Lambda nodes saved on that tab (`server/data/lambda.json`),
once. For 30 days a notice at the top of Settings lists what was carried over and what was not,
and why (for example, an access key was already saved here). `lambda.json` is left in place and
can be deleted afterwards.

### AWS account

The access key the server uses for every Lambda node: invoking the function, job files in S3,
reading the function's logs. `lambda-node/deploy.sh` creates an IAM user for it
(`fast-compute-server`) with only those permissions: a managed policy `fast-compute-<function>`
for each function it deploys (a re-run replaces the inline policy that an older `deploy.sh`
used). Create the user's key with `aws iam create-access-key --user-name fast-compute-server`.

- The card shows where the credentials come from — **Saved here**, **server/.env** or **Not
  configured** — with the key ID masked (`AKIA…WXYZ`), and whether boto3 is installed (if not:
  `.venv/bin/pip install -r requirements.txt` in `server/`, then restart).
- Form: *Access key ID*, *Secret access key* (never pre-filled; enter both together, or leave both
  empty to change only the region), *Default region* (suggested for new nodes). **Save**;
  **Remove saved credentials** (asks first) falls back to `server/.env` / the server's default AWS
  credentials.
- **Check credentials** asks AWS who the key belongs to (STS GetCallerIdentity, needs no
  permission): account and ARN, or the error with a plain hint (e.g. unknown key ID, wrong secret,
  clock skew). It warns if the key is the account's root key.
- Order for every Lambda node: the node's `profile` (`.env` nodes only) > the key saved here >
  boto3's default chain (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in `server/.env`, an
  instance role, …).

### Lambda nodes

One card per Lambda node: function, bucket, region, network, max concurrency, images, timeouts,
active jobs and draining. Nodes from `DOCKERSERVER_NODES` are marked **server/.env · read-only**
(change them there and restart); nodes added here have **Edit** and **Delete**.

- **Add Lambda node**: name (letters, digits, `. _ -`; unique across all nodes), function (name or
  ARN), S3 bucket, region (as `deploy.sh` printed them), network (`none` = the function runs in an
  isolated VPC and the node takes only jobs **without** network; `internet` = no VPC, only jobs
  **with** network — it must match how the function was deployed), max concurrency (the
  function's reserved concurrency), images (comma separated, default `python:3.12-slim`).
  *Advanced*: job file prefix (must start with `fast-compute/`, which the IAM policy and the 2-day
  cleanup cover), start timeout (30–900 s, default 300), stale after (20–600 s, default 60), fresh
  sandbox per job (default on). **Test settings** runs the diagnostics on the unsaved values;
  **Add node** saves it and the node takes jobs at once.
- **Edit**: max concurrency, images, network, timeouts and fresh sandbox can change while jobs run
  (new jobs use them). Function, bucket, region and prefix change only on an idle node: **Drain**
  it on the Nodes tab, wait for its jobs to finish, save, then click **Resume** on the Nodes tab
  (the node stays drained after saving). The name cannot change (delete and add again).
  **Delete** is refused while jobs run; it does not touch the function or bucket in AWS.
- Add each function only once (here or in `.env`): every node counts its own concurrency slots.

Debugging tools, on every Lambda node (also the `.env` ones):

- **Diagnose** — a checklist: ✓ ok, **!** warning, ✕ failed, – skipped (an earlier step failed),
  each with details, a hint and the time taken. Steps: *boto3 library*; *AWS credentials* (STS:
  account, ARN, source); *Lambda function* (state, memory → vCPUs, timeout → longest job,
  architecture, VPC — warns when the VPC does not match the node's network); *Reserved
  concurrency* (set and not lower than max concurrency); *Async retries* (must be 0, or a job could
  run twice); *S3 bucket access* (put, get and delete a probe object under
  `<prefix>_diagnostics/`, and a file that does not exist must read as missing, not forbidden);
  *Pre-signed URLs over HTTPS* (what the function does). With the IAM policy of an older
  `deploy.sh` the server may not read the concurrency and retry settings, and S3 answers 403 for a
  job file that is not written yet: those steps are skipped or warn, with a hint to re-run
  `deploy.sh`.
- **Run test job** — runs a fixed diagnostic script on the function (never an admin-supplied
  command) the way a job runs; not billed, not stored as a job, holds one concurrency slot,
  stops after at most 120 s. Progress lines update every 1.5 s; then status, exit code, timings
  (invoke, cold start ≈ time to the first status, total) and the output: Python version, platform,
  vCPUs, memory limit, free `/tmp`, and whether the internet (`1.1.1.1:443`) and S3 are reachable,
  compared with the node's network setting. Refused while the node is offline or full.
- **Function logs** — the function's CloudWatch Logs events of the last 15 min / 1 h / 6 h / 24 h
  (newest last, at most 300): Lambda's `START` / `END` / `REPORT` lines (duration, max memory
  used), timeouts and crashes. Never a job's output, which goes only to its owner. The function
  writes them when deployed with `LOGS=1` (always with `NETWORK=none`); "no log group …" means
  it never wrote any, `AccessDenied` that the server's IAM policy is from an older `deploy.sh`
  (re-run it). With the fresh sandbox each invocation ends with a `Runtime exited …` line: that
  is expected.

After a change here the Nodes tab is up to date too. Details, permissions and a troubleshooting
table: [`lambda-node/README.md`](../../lambda-node/README.md).

### Deploy helper

A form (region, function name, memory MB, timeout, network, concurrency, function logs on/off)
that builds the command to run where the aws CLI v2 (signed in as an AWS admin) and Docker
buildx are, in a checkout of this repository, with **Copy**:

```sh
REGION=eu-west-2 FUNCTION=fast-compute-job MEMORY_MB=2048 TIMEOUT=900 NETWORK=none CONCURRENCY=10 LOGS=0 sh lambda-node/deploy.sh
```

It creates or updates the S3 bucket for job files, the ECR image, the function's execution role
(no permissions of its own), an isolated VPC for `NETWORK=none`, the function (reserved
concurrency, async retries 0) and the IAM user for this server, and prints the command that
creates the access key. Then: save the key under **AWS account**, add the node under **Lambda
nodes** (**Use these values for a new node** fills the form), run **Diagnose** and a test job.
`LOGS=1` lets Lambda write the function's log group; the trade-off: job code can read the
function role's credentials, so a job could then also write to (only) that log group.

### Homepage advisor

The product-advisor chat on `/` (see [below](#homepage-and-product-advisor)). The card shows the
source (**Saved here** / **server/.env** / **Not configured**) and the mode (LLM answers or
built-in FAQ only). Fields: *Base URL* (any OpenAI-compatible API), *Model* (empty = the `.env`
value; with no model anywhere the advisor uses the FAQ), *Max tokens per answer* (16–32000;
empty = the `.env` value), *API key* (never shown; leave empty to keep the current one, or remove
the saved one). Saved values override `DOCKERSERVER_SALES_BASE_URL`, `_MODEL`, `_MAX_TOKENS` and
`_API_KEY` field by field, at once (a value equal to the `.env` one is not saved, so later `.env`
changes still apply); **Reset to server/.env** drops them. **Test** asks one
question with the current settings (no visitor rate limit) and shows the reply, mode, time and
the provider's error if any.

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
| `list_nodes` | compute nodes: health, capacity, jobs there (metadata), 24 h counts |
| `set_node_draining` | `name`, `draining` |
| `lambda_settings` | read-only: AWS credentials in effect (no secrets), Lambda nodes, advisor settings |
| `diagnose_lambda_node` | `name` → the Diagnose checklist |
| `lambda_function_logs` | `name`, `minutes` (60, max 1440) → the function's CloudWatch events |

Settings can only be changed in the web page or the REST API, not over MCP.

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
| GET | `/admin/nodes` | compute nodes (`source`: `env` or `admin`) |
| POST | `/admin/nodes/{name}/drain` · `/undrain` | drain / resume a node |
| GET | `/admin/settings` | AWS credentials in effect (no secrets), Lambda nodes, defaults, advisor |
| PUT / DELETE | `/admin/settings/aws` | save `{access_key_id, secret_access_key, region}` (or only `{region}`) / remove |
| POST | `/admin/settings/aws/check` | `{ok, source, account, arn, ms, error, hint}` |
| POST | `/admin/settings/lambda/nodes` | add a Lambda node (201) |
| PUT / DELETE | `/admin/settings/lambda/nodes/{name}` | change / delete (`.env` nodes or running jobs → 409) |
| POST | `/admin/settings/lambda/validate` | diagnose unsaved node settings |
| POST | `/admin/settings/lambda/nodes/{name}/diagnose` | diagnose a node |
| POST | `/admin/settings/lambda/nodes/{name}/test-job` | start a test job (202) |
| GET | `/admin/settings/lambda/tests/{id}` | test job state |
| GET | `/admin/settings/lambda/nodes/{name}/logs?minutes=60` | function logs |
| PUT / DELETE | `/admin/settings/advisor` | save `{base_url, model, max_tokens, api_key}` / back to `.env` |
| POST | `/admin/settings/advisor/test` | `{question}` → `{ok, reply, mode, ms, error}` |
| GET | `/admin/leads` | access requests from the homepage |

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
# diagnose the Lambda node aws-lambda
curl -s -X POST -H "Authorization: Bearer $ADMIN" $DS/admin/settings/lambda/nodes/aws-lambda/diagnose
```

## Homepage and product advisor

`/` is the public product homepage (Chinese/English). Its chat widget is a product advisor:

- Without an LLM it answers from a built-in FAQ. To use an LLM, configure any OpenAI-compatible
  API in **Settings → Homepage advisor** (applies at once, with a test box), or in `server/.env`
  and restart: `DOCKERSERVER_SALES_BASE_URL` (default `https://api.openai.com/v1`; e.g.
  `https://api.deepseek.com/v1`, `https://api.x.ai/v1`), `DOCKERSERVER_SALES_MODEL` and
  `DOCKERSERVER_SALES_API_KEY` (`DOCKERSERVER_SALES_MAX_TOKENS` limits the answer length). Values
  saved in Settings win over `.env`. `GET /sales/info` shows `"mode": "llm"` once a model and a key
  are set.
- The model only sees the product facts in `server/dockerserver/sales.py` (`KNOWLEDGE`); edit them
  when the product changes. It never sees user data.
- Limits: `DOCKERSERVER_SALES_RATE_LIMIT` questions per visitor IP per hour (30), and
  `DOCKERSERVER_SALES_DAILY_LIMIT` LLM answers per day in total (500), after which the FAQ answers.
  The test in Settings is not counted against the visitor limit.
- The "Request access" form stores requests in `server/data/leads.jsonl`; read them with
  `GET /admin/leads` (admin token). `DOCKERSERVER_SALES_CONTACT` optionally shows a contact line.

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
