# lambda-node — AWS Lambda as a fast-compute compute node

The server (`server/dockerserver/lambda_backend.py`) can send jobs to an AWS Lambda function
instead of (or next to) Docker nodes such as mini. One Lambda invocation runs one job.

| File | What it is |
|---|---|
| `handler.py` | Runs one job inside Lambda: unpacks the workspace, runs `sh -c <command>`, uploads log tail and status, obeys kill requests. Standard library only. |
| `Dockerfile` | Function image: `python:3.12-slim` + the Lambda runtime client, so jobs see the same Python as on Docker nodes. |
| `deploy.sh` | Creates/updates everything in AWS: S3 bucket, ECR image, execution role, optional isolated VPC, the function, and an IAM user for the server. |

## How it works

```
server ──put workspace.tar──▶ S3 ◀──GET workspace / PUT status+log / GET kill── Lambda (handler.py)
   │                           ▲
   └──invoke (async, event: job + pre-signed URLs)──────────────────────────────▶
   └──poll status.json, read output.log, PUT kill, delete objects──▶ S3
```

- The handler uses only the pre-signed URLs in the event, so the function's execution role has
  **no permissions** (in `network=none` mode only the VPC networking policy Lambda needs). Job code
  can read the role's credentials in a Lambda, which is why they must not be able to do anything;
  the job's environment also never gets any `AWS_*` variable.
- **Fresh sandbox per job**: Lambda reuses execution environments. After reporting, the handler
  ends its process so Lambda throws the environment away; the next job starts cold and clean.
  (Async retries are set to 0, so this is never retried.) If an environment is ever reused anyway,
  the handler first kills every leftover process and wipes `/tmp`.
- **Network**: `NETWORK=none` puts the function in a VPC with no internet gateway or NAT; it reaches
  only S3 (gateway endpoint limited to the job files). Such a node takes only jobs **without**
  network. `NETWORK=internet` (no VPC) takes only jobs **with** network, and those have full
  outbound internet (Docker nodes allow only HTTP(S) through a proxy). Deploy twice under two
  function names if you want both.
- **Limits**: one invocation = the function's memory (`MEMORY_MB`) and the vCPUs Lambda gives it
  (1 vCPU per 1769 MB, max 6). A job fits if its cpus/mem_mb fit that. Longest job = function
  timeout − 30 s (max 870 s, since Lambda stops at 15 min). Only `python:3.12-slim` jobs. At most
  `max_concurrency` jobs at once. `/tmp` (the workspace) is 2 GB.
- **Scheduling**: Lambda nodes are tried after every Docker node with room, so they take the
  overflow. Draining works as for any node.
- **Billing**: users pay credits exactly as on Docker nodes (reserved size × time). Time is measured
  by the server, from the invoke until it sees the result, so cold start (about 1 s) is included.
  AWS charges you the function's memory × run time (e.g. 2 GB for 10 min ≈ $0.02 in eu-west-2)
  plus tiny S3 request costs.
- **Out of memory / crash**: Lambda ends the invocation; the status stops updating and after 60 s
  the job ends as `error` with that reason. A job that never starts within 5 min also ends as `error`.

## Deploy

Needs the aws CLI v2 signed in as an AWS admin, and Docker (buildx) — e.g. on mini.

```sh
REGION=eu-west-2 sh lambda-node/deploy.sh            # isolated, 2 GB, 10 concurrent
REGION=eu-west-2 FUNCTION=fast-compute-net NETWORK=internet sh lambda-node/deploy.sh
```

It prints the `DOCKERSERVER_NODES` entry and the command that creates the server's access key.
Then, on the bridge, in `server/.env` (never in git):

```sh
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
DOCKERSERVER_NODES='[{"name":"mini",...},{"name":"aws-lambda","type":"lambda","function":"fast-compute-job","bucket":"fast-compute-jobs-<account>-eu-west-2","region":"eu-west-2","max_concurrency":10,"network":"none"}]'
```

`pip install -r requirements.txt` in `server/` (adds boto3), restart dockerserver, and check
`/admin-ui` → Nodes. Optional node keys: `prefix`, `images`, `profile` (an AWS profile instead of
the `AWS_*` variables), `start_timeout` (300), `stale_after` (60), `fresh_sandbox` (true).

The server's IAM user may only invoke this function, read its configuration, and read/write/delete
objects under `fast-compute/` in the bucket. Objects left behind expire after 2 days.
