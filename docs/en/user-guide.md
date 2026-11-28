# User guide

[中文](../zh/user-guide.md) · [Admin guide](admin-guide.md) · [Deployment](deployment.md)

fast-compute runs your code in an isolated, throw-away Docker container on a compute node and
charges credits for the time it runs. Use it from an AI assistant via **MCP**, from the
**REST API**, or look at your jobs on the **web page**.

- Address: **https://compute.llmfreedomforge.com**
- You need an **access token** (starts with `ds_`) — ask the administrator. Keep it secret:
  whoever has it can run jobs and spend your credits. It is shown only once when issued.

## 1. Concepts

| Term | Meaning |
|---|---|
| Job | One shell command run in a fresh container, e.g. `python main.py`. |
| Workspace | Your files, in `/workspace` (also the working directory). Writable; deleted after the job. |
| Image | Only `python:3.12-slim` is allowed. |
| Size | `cpus` (may be fractional, e.g. `0.5`) and `mem_mb`. Default **1 CPU, 256 MB**. |
| Timeout | `timeout_seconds`; the job is killed when it is reached. Default = your maximum run time. |
| Credit-hour | Billing unit: **1 CPU core + 256 MB RAM for 1 hour**. |

### Billing

```
cost (credit-hours) = run_seconds / 3600 × max(cpus, mem_mb / 256)
```

You pay for the **reserved** size × wall-clock run time, not actual usage.

| Size | Run time | Cost |
|---|---|---|
| 1 CPU, 256 MB | 1 h | 1 |
| 2 CPU, 256 MB | 30 min | 1 |
| 1 CPU, 1024 MB | 1 h | 4 (memory is the larger dimension) |
| 0.5 CPU, 128 MB | 10 min | ≈ 0.083 |

- New accounts start with **1000 credit-hours** (unless the admin configured otherwise).
- A job is **refused (HTTP 402)** if your available credit can't cover 60 s at the requested size.
- If credit runs out **while** a job runs, it is stopped with status `out_of_credits`.
- Each job is charged once, when it ends; the charge appears as `cost_hours` on the job.

### Limits

Each account has limits set by the admin: max CPUs, max memory, max run time per job, max jobs
at the same time, and whether network is allowed. New accounts default to 1 CPU, 256 MB, 600 s,
2 concurrent jobs, no network. Requests above your limits are rejected (HTTP 400/403/429). See
your limits with `get_balance` (MCP), `GET /me`, or on the web page.

### Isolation — what a job can and cannot do

- ✅ Read/write `/workspace`; use `/tmp` (64 MB, no executables); print to stdout/stderr (= logs).
- ❌ **No network** unless your account allows it, you pass `network=true`, *and* the compute node
  permits network. So `pip install` normally does not work: use the standard library or upload
  pure-Python dependencies with your code (e.g. a `vendor/` folder added to `sys.path`).
- ❌ Read-only system: no `apt`, no writes outside `/workspace` and `/tmp`.
- ❌ Cannot see the host, other jobs, or a Docker socket. Runs as user `nobody`, max 256 processes,
  memory limit without swap (exceeding it kills the job: "out of memory").

### Job statuses

| Status | Meaning |
|---|---|
| `queued` | Waiting for free capacity on a compute node (retried automatically). |
| `preparing` | Copying your files / cloning the git repo. |
| `running` | Running. |
| `succeeded` | Exit code 0. |
| `failed` | Non-zero exit code — see `exit_code` and the logs. |
| `timeout` | Killed at the timeout. |
| `stopped` | You stopped it. |
| `out_of_credits` | Credit ran out (or too little to start). |
| `killed_by_admin` | Your account was suspended by an admin. |
| `lost` | The compute node became unreachable; billed up to the last contact. |
| `error` | See the `error` field (e.g. bad git URL, git clone failed). |

## 2. Connect an AI assistant (MCP) — recommended

**Claude Code:**

```bash
claude mcp add --transport http dockerserver https://compute.llmfreedomforge.com/mcp \
  --header "Authorization: Bearer ds_YOUR_TOKEN"
```

**Claude Desktop** (needs Node.js; uses the `mcp-remote` bridge). Settings → Developer → Edit
Config, add to `claude_desktop_config.json`, then restart Claude Desktop:

```json
{
  "mcpServers": {
    "dockerserver": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://compute.llmfreedomforge.com/mcp",
               "--header", "Authorization: Bearer ${DOCKERSERVER_TOKEN}"],
      "env": { "DOCKERSERVER_TOKEN": "ds_YOUR_TOKEN" }
    }
  }
}
```

Then just ask, e.g. *"Run this script on dockerserver with 2 CPUs and show me the output."*

**claude.ai / Claude Desktop custom connector (no Node.js needed):** Settings → Connectors →
*Add custom connector* → URL `https://compute.llmfreedomforge.com/mcp` (leave the OAuth client
fields empty) → *Connect*. A fast-compute page opens: paste your `ds_…` token and click
**Approve**. The app receives its own token (valid 1 h, renewed automatically); your `ds_` token is
not shared with it. Revoking your `ds_` token, or your account being disabled, disconnects it.

### Job tools (`/mcp`) — always only your own jobs

Site tools: see section 5.

| Tool | Arguments | Returns |
|---|---|---|
| `submit_job` | `command` (required); code as `files` (`{"path": "text"}`) **or** `git_url` (public https); optional `cpus`, `mem_mb`, `timeout_seconds`, `network`, `image`, `wait` (0–60 s) | the job; with `wait`, also `logs_tail` once finished |
| `list_jobs` | `status`, `limit` (20) | your jobs, newest first |
| `get_job` | `job_id` | status, exit code, times, `cost_hours`, CPU/memory avg & peak |
| `get_job_logs` | `job_id`, `tail` (200; 0 = all) | stdout+stderr (live while running) |
| `stop_job` | `job_id` | the stopped job (charged for time run) |
| `get_balance` | — | `balance_hours`, `available_hours`, your limits |
| `get_usage` | `start`, `end` (ISO, UTC), `include_jobs` | totals and per-day CPU-hours, GB-hours, cost |
| `list_env` | `reveal` (false) | your saved environment variables (values masked unless `reveal`) |
| `set_env` | `name`, `value` | save / replace a variable (used by all your jobs) |
| `delete_env` | `name` | delete a variable |

Example `submit_job` arguments:

```json
{"command": "python main.py", "files": {"main.py": "print(sum(range(10**7)))"},
 "cpus": 1, "mem_mb": 256, "wait": 30}
```

## 3. REST API

Every request needs `Authorization: Bearer ds_YOUR_TOKEN`. Interactive docs:
https://compute.llmfreedomforge.com/docs

```bash
export DS=https://compute.llmfreedomforge.com TOKEN=ds_YOUR_TOKEN
```

### Submit — `POST /jobs` (multipart form)

| Field | Description |
|---|---|
| `command` | **required**; run with `sh -c` in `/workspace` |
| `file` | one file, or `.zip` / `.tar` / `.tar.gz` / `.tgz` (unpacked into `/workspace`); max 50 MB |
| `git_url` | public `https://` repository (shallow clone) |
| `cpus`, `mem_mb`, `timeout_seconds` | size / timeout |
| `network` | `true` to request network (see isolation) |
| `image` | default `python:3.12-slim` |

> With curl, pass `command` with **`--form-string`**. With `-F`, curl treats `;` as an option
> separator and silently truncates commands such as `echo a; echo b`.

```bash
curl -s -H "Authorization: Bearer $TOKEN" --form-string command="python main.py" \
  -F file=@main.py $DS/jobs
curl -s -H "Authorization: Bearer $TOKEN" --form-string command="python train.py" \
  -F file=@project.zip -F cpus=2 -F mem_mb=1024 -F timeout_seconds=1800 $DS/jobs
curl -s -H "Authorization: Bearer $TOKEN" --form-string command="python -m pytest -q" \
  -F git_url=https://github.com/OWNER/REPO $DS/jobs
```

### Follow, stop, list

```bash
curl -s -H "Authorization: Bearer $TOKEN" $DS/jobs/42                 # status, cost, usage
curl -s -H "Authorization: Bearer $TOKEN" "$DS/jobs/42/logs?tail=100"  # output
curl -s -H "Authorization: Bearer $TOKEN" -X POST $DS/jobs/42/stop     # stop
curl -s -H "Authorization: Bearer $TOKEN" "$DS/jobs?status=running&limit=20"
curl -s -H "Authorization: Bearer $TOKEN" $DS/me                       # limits, balance_hours
curl -s -H "Authorization: Bearer $TOKEN" "$DS/me/usage?start=2026-11-01T00:00:00"
```

### Python

```python
import time, requests
DS, H = "https://compute.llmfreedomforge.com", {"Authorization": "Bearer ds_YOUR_TOKEN"}
job = requests.post(f"{DS}/jobs", headers=H, data={"command": "python main.py", "mem_mb": "512"},
                    files={"file": ("main.py", b"print('hello')")}).json()
while job["status"] in ("queued", "preparing", "running"):
    time.sleep(2)
    job = requests.get(f"{DS}/jobs/{job['id']}", headers=H).json()
print(job["status"], job["exit_code"], job["cost_hours"])
print(requests.get(f"{DS}/jobs/{job['id']}/logs", headers=H).text)
```

## 4. Environment variables

Save variables once (API keys, settings) and every job you run gets them; a job's own `env`
overrides them for that job.

- **Where:** the *Environment variables* section on `/ui`, the MCP tools `list_env` / `set_env` /
  `delete_env`, or REST: `GET /me/env` (masked; `?reveal=true` shows values),
  `PUT /me/env/{NAME}` with `{"value": "..."}`, `DELETE /me/env/{NAME}`.
- **Per job:** `submit_job(..., env={"NAME": "value"})` (MCP) or the form field
  `env='{"NAME":"value"}'` (REST, JSON).
- **Rules:** names `[A-Za-z_][A-Za-z0-9_]*` (≤ 64 chars); values ≤ 4096 chars; ≤ 50 variables,
  ≤ 32 KiB in total. Reserved (refused): `PATH`, `HOME`, `HOSTNAME`, `PWD`, `PYTHONHOME`,
  `PYTHONUNBUFFERED`, and names starting with `LD_` or `DOCKERSERVER_`.
- **Privacy:** values are stored encrypted, only you can see them (administrators can't), they are
  never logged, and per-job values are not stored at all — a job only records the variable *names*
  (`env_names`).

```bash
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"value":"sk-..."}' $DS/me/env/OPENAI_API_KEY
curl -s -H "Authorization: Bearer $TOKEN" --form-string command='python -c "import os; print(os.environ[\"MODE\"])"' \
  -F env='{"MODE":"test"}' $DS/jobs
```

## 5. Static sites (web pages, browser games)

Keep a set of front-end files (HTML, JS, CSS, images) on the server and serve them read-only at a
public HTTPS address. Nothing runs on the server, so this is for purely static pages such as
browser games.

| Tool | Arguments | Returns |
|---|---|---|
| `put_site_file` | `site`, `path`, `content`, optional `encoding` (`utf-8` default, or `base64` for images and other binary files) | writes one file; the site is created on the first write |
| `publish_site` | `site` | `{url, ...}`: the public address |
| `unpublish_site` | `site` | takes it offline (files kept) |
| `list_sites` | — | your sites: URL, published, container awake, files, size, requests, bytes served, awake time |
| `list_site_files` | `site` | files (path, size, modified) |
| `delete_site_file` | `site`, `path` | deletes one file |
| `delete_site` | `site` | takes it offline and deletes all files |

- **Address**: `https://<sites host>/sites/<user>/<site>/`; the root serves `index.html`. Sites live
  under a sub-path, so link files relatively (`game.js`, `img/a.png`), not as `/game.js`.
- **Rules**: site names are 1–40 chars of lowercase letters, digits and `-`. `path` must be relative
  to the site: `..`, `.`, empty segments and absolute paths are rejected. Max 5 MB per file, 50 MB
  and 1000 files per site; 3 sites per user by default (admins can change it).
- **Content-Type** comes from the extension, with `charset=utf-8` for text (`.html` →
  `text/html; charset=utf-8`, `.js` → `application/javascript; charset=utf-8`, plus css, json, svg,
  png, jpg, woff2, mp3 and more). No CSP is added, so external fonts (fonts.googleapis.com) and CDNs load.
- **Sleep**: each site is served by a small nginx container. After an hour without visitors the
  container is stopped; the next visit starts it again (the first load takes about a second longer).
  Files are kept; re-uploading a file updates the live site at once (browsers revalidate).
- **Cost**: sites are currently free (the server can be configured to charge for awake time); usage
  is recorded.

REST: `PUT /me/sites/{site}/files/{path}` (JSON `{"content": "...", "encoding": "utf-8"}`),
`POST /me/sites/{site}/publish`, `POST /me/sites/{site}/unpublish`, `GET /me/sites`,
`GET /me/sites/{site}/files`, `DELETE /me/sites/{site}/files/{path}`, `DELETE /me/sites/{site}`.

```bash
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  --data "$(python3 -c 'import json; print(json.dumps({"content": open("index.html").read()}))')" \
  $DS/me/sites/gun-fu/files/index.html
curl -s -X POST -H "Authorization: Bearer $TOKEN" $DS/me/sites/gun-fu/publish
```

## 6. Web page

https://compute.llmfreedomforge.com/ui — sign in with your token (it stays only in that browser
tab). **Welcome** explains how to connect and use fast-compute. **Dashboard** shows your balance,
the credit-hours available now, jobs run and credit-hours spent, your limits, your jobs (click one
for its details and logs, and to stop it), your environment variables, your web sites, and usage
per day (a chart of the last 30 days above the table).

The page is in English and Chinese: the **中文 / EN** button (on the sign-in card and in the top
bar) switches it. The choice is remembered in the browser and shared with the homepage; the first
visit follows the browser's language.

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| **401** | Token missing, wrong or revoked, or your account is disabled. Ask the admin. |
| **402** not enough credits | Balance can't cover 60 s at this size. Use a smaller size or ask for a top-up. |
| **400** "exceeds your limit" | Lower `cpus` / `mem_mb` / `timeout_seconds`, or ask the admin to raise your limits. |
| **400** "larger than any compute node" | No node is big enough for that size. Request less. |
| **403** network | Network is not enabled for your account. |
| **429** too many active jobs | Wait for running jobs to finish (or stop some). |
| **429** too many failed authentication attempts | Too many wrong tokens from your IP; wait ~5 min. |
| Job stays `queued` | All nodes are busy; it starts automatically when capacity frees (gives up after 1 h). |
| `failed` with no output | Check `exit_code`; 137 usually means killed (memory limit → `error` says out of memory). |
| Command cut off at `;` (curl) | Use `--form-string command=...`. |
| `ModuleNotFoundError` | No network → no `pip install`; bundle pure-Python deps with your code. |
