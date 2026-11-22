# dockerserver-node

Compute node for DockerServer. Runs on mini (tailnet 100.126.20.127) and executes jobs that
the bridge (aws-london, 100.82.2.33) forwards to it, each in an isolated Docker container.

## Start

    ./start.sh            # creates .venv and config.env (with a fresh token, mode 600, git-ignored) on first run

Docker comes from Docker Desktop running under `gentle`; `coding` reaches it through
`unix:///Users/gentle/.docker/run/docker.sock` (ACL granted by gentle; docker context `gentle`).
Print the token with `.venv/bin/python -m computenode token`.

## API (Bearer token, tailnet IP only, port 8900, callers limited to COMPUTENODE_ALLOWED_CLIENTS)

| Method | Path | |
|---|---|---|
| GET | /v1/info | capacity / used / free cpus and mem_mb, active_jobs, `features` (e.g. `["env"]`) |
| POST | /v1/jobs | multipart: `spec` JSON `{id,image,command,cpus,mem_mb,timeout_seconds,network,env?}` + optional `workspace` tar; 201, 409 if full. `env` = `{NAME: value}`, validated here too (names, reserved names, sizes), set on the container, never returned or logged |
| GET | /v1/jobs/{id} | status, exit_code, oom, cpu/mem avg/peak, started_at/finished_at |
| GET | /v1/jobs/{id}/logs?tail=N | combined stdout/stderr |
| POST | /v1/jobs/{id}/kill | |
| DELETE | /v1/jobs/{id} | remove container + volume |

Status: preparing, running, succeeded, failed, timeout, killed, error.

## Isolation (enforced here, whatever the bridge sends)

Image allow-list; user nobody; read-only root fs; cap_drop ALL; no-new-privileges; pids, cpu and
memory limits (no swap); noexec /tmp tmpfs; workspace is a per-job Docker volume, never a host
path; no Docker socket; network `none` unless COMPUTENODE_ALLOW_NETWORK=true; local wall-clock timeout.

## Service jobs

A job spec with `service_port` (1024 to 65535) runs a long-lived app that the bridge can reach.
The job view adds `ingress_port` (this node's forward port) and `service_ready` (the app accepts
TCP). `/v1/info` lists the `service` feature only when COMPUTENODE_ALLOWED_CLIENTS is set, because
forward ports carry no token and rely on that source-IP allowlist.

Path: bridge -> forward port (9100 to 9199) -> relay container -> app container. The app sits on
its own Docker `internal` network with `inhibit_ipv4`, so it has no route out and no Docker-host
address to reach. The relay sits alone on a per-app edge network and listens only there. Service
jobs cannot request outbound network. Keys: `COMPUTENODE_INGRESS_PORT_MIN/MAX`, `SERVICE_SUBNET`
(default 10.213.0.0/16, must not overlap the LAN or tailnet), `SERVICE_RELAY_IMAGE`,
`MAX_SERVICE_SECONDS`, `SERVICE_MAX_CONNS`.

Config lives in `config.env` in this directory (see `computenode/config.py` for all keys).
