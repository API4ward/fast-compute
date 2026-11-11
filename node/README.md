# dockerserver-node

Compute node for DockerServer. Runs on mini (tailnet 100.126.20.127) and executes jobs that
the bridge (aws-london, 100.82.2.33) forwards to it, each in an isolated Docker container.

## Start

    ./start.sh            # creates .venv and ~/.computenode/config.env (with a fresh token) on first run

Docker comes from Docker Desktop running under `gentle`; `coding` reaches it through
`unix:///Users/gentle/.docker/run/docker.sock` (ACL granted by gentle; docker context `gentle`).
Print the token with `.venv/bin/python -m computenode token`.

## API (Bearer token, tailnet IP only, port 8900, callers limited to COMPUTENODE_ALLOWED_CLIENTS)

| Method | Path | |
|---|---|---|
| GET | /v1/info | capacity / used / free cpus and mem_mb, active_jobs |
| POST | /v1/jobs | multipart: `spec` JSON `{id,image,command,cpus,mem_mb,timeout_seconds,network}` + optional `workspace` tar; 201, 409 if full |
| GET | /v1/jobs/{id} | status, exit_code, oom, cpu/mem avg/peak, started_at/finished_at |
| GET | /v1/jobs/{id}/logs?tail=N | combined stdout/stderr |
| POST | /v1/jobs/{id}/kill | |
| DELETE | /v1/jobs/{id} | remove container + volume |

Status: preparing, running, succeeded, failed, timeout, killed, error.

## Isolation (enforced here, whatever the bridge sends)

Image allow-list; user nobody; read-only root fs; cap_drop ALL; no-new-privileges; pids, cpu and
memory limits (no swap); noexec /tmp tmpfs; workspace is a per-job Docker volume, never a host
path; no Docker socket; network `none` unless COMPUTENODE_ALLOW_NETWORK=true; local wall-clock timeout.

Config lives in `~/.computenode/config.env` (see `computenode/config.py` for all keys).
