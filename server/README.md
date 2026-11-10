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
cd /home/ubuntu/projects/dockerserver
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

## API

All endpoints except `/health` need `Authorization: Bearer <token>`.

**Admin**

| Method | Path | |
|---|---|---|
| POST | `/admin/users` | create user `{name, role?, max_cpus?, max_mem_mb?, max_run_seconds?, max_concurrent_jobs?, allow_network?}`; returns a first token |
| GET | `/admin/users` | list users |
| PATCH | `/admin/users/{id}` | change role / limits / `active` |
| POST | `/admin/users/{id}/disable` · `/enable` | disable/enable (disabled users' tokens stop working) |
| GET/POST | `/admin/users/{id}/tokens` | list / issue tokens |
| DELETE | `/admin/tokens/{id}` | revoke a token |

**Jobs** (users see only their own; admins can pass `?all=true` and open any job)

| Method | Path | |
|---|---|---|
| POST | `/jobs` | multipart form: `command` (required), `file` (single file, .zip or .tar[.gz]), `git_url` (https), `image`, `cpus`, `mem_mb`, `timeout_seconds`, `network` |
| GET | `/jobs` | list my jobs (`?status=running`) |
| GET | `/jobs/{id}` | status, exit code, start/end times |
| GET | `/jobs/{id}/logs` | stdout+stderr (live while running) |
| POST | `/jobs/{id}/stop` | kill the job |
| GET | `/me` | my user + limits |

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

`queued → preparing → running → succeeded | failed | timeout | stopped | error`.
On restart, the server marks jobs that were still active as `error` and removes leftover job containers/volumes.

## Billing data (for step 2)

The `jobs` table stores `user_id, image, cpus, mem_mb, started_at, finished_at, exit_code, status`
plus a `billed` flag. Planned rate: 1 credit-hour = 1 CPU core + 2 GB RAM for one hour, larger containers charged proportionally.

## Tests

```bash
sg docker -c ".venv/bin/python -m pytest -q"
```

Docker tests (a hello-world job that checks the container can't see host files, a timeout, a stop)
are skipped automatically if Docker isn't reachable.

## Layout

```
dockerserver/
  config.py     settings from env
  models.py     SQLAlchemy models: User, Token, Job
  auth.py       token issue / hash / lookup
  workspace.py  upload/git → sanitized tar for /workspace
  runner.py     Docker job runner (limits, isolation, timeout, logs)
  api.py        FastAPI app (admin + jobs)
  main.py       ASGI entry point
  cli.py        create-admin / issue-token / list-users
tests/          pytest (API tests + real Docker tests)
```
