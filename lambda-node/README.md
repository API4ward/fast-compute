# lambda-node — AWS Lambda as a fast-compute compute node

The server (`server/dockerserver/lambda_backend.py`) can send jobs to an AWS Lambda function
instead of (or next to) Docker nodes such as mini. One Lambda invocation runs one job. Set it up
and debug it from the admin page, **`/admin-ui` → Settings**; `server/.env` still works as the
alternative.

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
  **no permissions** of its own (in `network=none` mode only the VPC networking policy Lambda
  needs; with `LOGS=1` writing its own log group, see [Function logs](#function-logs-logs1)). Job
  code can read the role's credentials in a Lambda, which is why they must not be able to do
  anything; the job's environment also never gets any `AWS_*` variable.
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
- **Scheduling**: Lambda nodes are tried after every Docker compute node with room, so they take
  the overflow. This host's own Docker, when it runs jobs (no usable node in `DOCKERSERVER_NODES`,
  or `DOCKERSERVER_LOCAL_EXECUTION=true`), comes after all nodes, Lambda included: a job that
  fits Lambda then runs there first. Draining works as for any node.
- **Billing**: users pay credits exactly as on Docker nodes (reserved size × time). Time is measured
  by the server, from the invoke until it sees the result, so cold start (about 1 s) is included.
  AWS charges you the function's memory × run time (e.g. 2 GB for 10 min ≈ $0.02 in eu-west-2)
  plus tiny S3 request costs.
- **Out of memory / crash**: Lambda ends the invocation; the status stops updating and after 60 s
  (the node's *stale after*) the job ends as `error` with that reason. A job that never starts
  within 5 min (*start timeout*) also ends as `error`.

## 1. Deploy the function

Needs the aws CLI v2 signed in as an AWS admin, and Docker (buildx) — e.g. on mini. Run it from
a checkout of this repository. **Settings → Deploy helper** in `/admin-ui` builds the command
from a form.

```sh
REGION=eu-west-2 sh lambda-node/deploy.sh            # isolated, 2 GB, 10 concurrent
REGION=eu-west-2 FUNCTION=fast-compute-net NETWORK=internet LOGS=1 sh lambda-node/deploy.sh
```

| Variable | Default | |
|---|---|---|
| `REGION` | — (required) | AWS region of the function and the bucket |
| `FUNCTION` | `fast-compute-job` | function name (also the ECR repository and `<FUNCTION>-exec` role) |
| `BUCKET` | `fast-compute-jobs-<account>-<region>` | S3 bucket for job files (private; objects under `fast-compute/` expire after 2 days) |
| `MEMORY_MB` | 2048 | memory per job, 128–10240 (1769 MB = 1 vCPU, max 6) |
| `TIMEOUT` | 900 | function timeout in s (≤ 900); longest job = `TIMEOUT` − 30 |
| `NETWORK` | `none` | `none` = isolated VPC without internet, `internet` = no VPC |
| `CONCURRENCY` | 10 | reserved concurrency; use it as the node's max concurrency |
| `LOGS` | 0 | 1 = Lambda may write the function's CloudWatch log group (see below) |
| `ARCH` | `x86_64` | or `arm64` |
| `SERVER_USER` | `fast-compute-server` | IAM user the server signs in as |

It is safe to run again: existing resources are reused, the code and configuration updated, and
the server's IAM policy rewritten, so an existing access key gets new permissions without any
change on the server. At the end it prints the command that creates the server's access key and
the values for the next step.

## 2. Connect it: `/admin-ui` → Settings

1. **AWS account**: create the server's access key (only once; an IAM user can have two)
   ```sh
   aws iam create-access-key --user-name fast-compute-server
   ```
   and paste `AccessKeyId` and `SecretAccessKey` into *Access key ID* / *Secret access key*,
   optionally with a default region for new nodes, then **Save** and **Check credentials** (shows
   the AWS account and the user's ARN, or the error with a hint). The secret is stored encrypted
   in `server/data/settings.json` (mode 600, Fernet key `DOCKERSERVER_SECRET_KEY` from
   `server/.env`) and is never shown again or logged; the key ID is shown masked (`AKIA…WXYZ`).
   A change applies at once to every Lambda node. **Remove saved credentials** falls back to
   `server/.env`. Which credentials a node uses: its `profile` (`.env` nodes only), else the key
   saved here, else boto3's default chain (`AWS_*` in `server/.env`, an instance role, …).
2. **Lambda nodes → Add Lambda node**: a name (e.g. `aws-lambda`; unique, shown on the Nodes tab
   and in jobs), the function (name or ARN), bucket and region that `deploy.sh` printed, the
   network (must match how the function was deployed), max concurrency (the reserved concurrency)
   and the images (default `python:3.12-slim`). *Advanced*: job file prefix (must start with
   `fast-compute/`, which the IAM policy and the 2-day cleanup cover), start timeout (300 s),
   stale after (60 s), fresh sandbox (on). **Test settings** runs the diagnostics below on the
   unsaved values; **Add node** saves it, and the node takes jobs at once, no restart.
3. Run **Diagnose** and **Run test job** on the node.

Changing a node: max concurrency, images, network, timeouts and fresh sandbox can change while
jobs run (new jobs use them). Function, bucket, region and prefix only change on an idle node:
**Drain** it on the Nodes tab, wait for its jobs, then save. A node with running jobs cannot be
deleted; the name cannot be changed (delete and add again). Deleting a node does not touch the
function or the bucket in AWS. Add each function only once: two nodes on one function would each
count their own concurrency slots.

### Alternative: `server/.env`

```sh
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
DOCKERSERVER_NODES='[{"name":"mini",...},{"name":"aws-lambda","type":"lambda","function":"fast-compute-job","bucket":"fast-compute-jobs-<account>-eu-west-2","region":"eu-west-2","max_concurrency":10,"network":"none"}]'
```

Restart dockerserver after editing. Optional node keys: `prefix`, `images`, `start_timeout` (300),
`stale_after` (60), `fresh_sandbox` (true), and the `.env`-only `profile` (an AWS profile
instead of the `AWS_*` variables), `s3_endpoint_url`, `lambda_endpoint_url`. Settings shows
these nodes as "server/.env · read-only" (Diagnose, test job and function logs work for them
too). To move a node into Settings: drain it, wait for its jobs, remove it from
`DOCKERSERVER_NODES`, restart, and add it in Settings under the same name.

The server needs `boto3` (in `requirements.txt`): `.venv/bin/pip install -r requirements.txt` in
`server/`, then restart. Settings warns when it is missing.

## Function logs (`LOGS=1`)

Lambda can record each invocation in CloudWatch Logs (`/aws/lambda/<function>`): `START`, `END`
and `REPORT` lines (duration, memory size, max memory used, init duration), timeouts and crashes
(e.g. `Runtime exited with error: signal: killed` when out of memory). Settings → **Function logs**
shows them. A job's output never goes there: the handler sends it to S3 only, for the job's owner.
With the fresh sandbox, each invocation also ends with an error line (`Runtime exited …`,
`Runtime.ExitError`): that is the handler ending its process on purpose, not a failure.

- `LOGS=1` gives the execution role an inline policy `fast-compute-logs` that allows creating and
  writing the log group `/aws/lambda/<function>` and nothing else. `deploy.sh` creates the group
  if it is missing (events kept 30 days). `LOGS=0` removes the policy; the group and its events stay.
- **Trade-off**: job code can read the execution role's credentials, so with `LOGS=1` a job (on a
  `NETWORK=internet` function) could also write to that log group, and only to it. It cannot read
  any logs. Treat what you read there as possibly written by a job.
- With `NETWORK=none` Lambda writes these logs anyway: the AWS-managed VPC policy the function
  needs (`AWSLambdaVPCAccessExecutionRole`) includes writing logs, and jobs there have no route
  to CloudWatch. `LOGS` matters for `NETWORK=internet` functions.

## Debugging (`/admin-ui` → Settings → Lambda nodes)

**Diagnose** checks a node step by step (each ✓ ok, ! warning, ✕ failed or – skipped when an
earlier step failed, with the time taken and a hint):

| Step | Checks |
|---|---|
| boto3 library | installed on the server (else: `pip install -r requirements.txt`, restart) |
| AWS credentials | STS GetCallerIdentity: account, ARN and where the credentials come from; warns on root-account keys |
| Lambda function | state and last update, memory → vCPUs, timeout → longest job, architecture, package type, VPC; warns when the VPC does not match the node's network |
| Reserved concurrency | set, and not lower than the node's max concurrency (0 = throttled = failed) |
| Async retries | `MaximumRetryAttempts` is 0 (otherwise a job could run twice) |
| S3 bucket access | put, get and delete a probe object under `<prefix>_diagnostics/`; a file that does not exist reads as 404, not 403 (needs `s3:ListBucket`) |
| Pre-signed URLs over HTTPS | PUT and GET through pre-signed URLs from the server, as the function uses them |

**Run test job** runs a fixed diagnostic script on the function (never a command you type),
through the same path as a real job, but it is not billed and not stored as a job; it holds one
concurrency slot while it runs (refused when the node is offline or full) and stops after 120 s
(or the node's longest job, if shorter).
The page shows its progress, then status, exit code, timings (invoke, cold start ≈ time to the
first status, total) and the output: Python version, platform, vCPUs, memory limit, free `/tmp`,
and whether `1.1.1.1:443` (internet) and the S3 endpoint are reachable, compared with the node's
network setting. The last 20 tests are kept until a restart.

**Function logs** shows the function's CloudWatch events of the last 15 min, 1 h, 6 h or 24 h
(newest last, at most 300).

Admin MCP (`/mcp-admin`), read-only: `lambda_settings`, `diagnose_lambda_node(name)`,
`lambda_function_logs(name, minutes=60)`. REST: see `docs/en/admin-guide.md`.

| Symptom | Cause / fix |
|---|---|
| AWS credentials: `InvalidClientTokenId` / `UnrecognizedClientException` | The key was deleted, deactivated or mistyped: create a new one and save it. |
| AWS credentials: `SignatureDoesNotMatch` | The secret does not belong to the key ID (paste it again), or the server's clock is wrong. |
| Lambda function: `ResourceNotFoundException` | Wrong function name or region. |
| Lambda function / S3: `AccessDenied` | The server's IAM policy does not cover this function or bucket: check the names, re-run `deploy.sh`. |
| Reserved concurrency / async retries skipped (not allowed) | The server's IAM policy is from an older `deploy.sh`: re-run it. |
| S3 bucket access warns: a job file not written yet reads as 403 | The server's IAM policy is from an older `deploy.sh` (no `s3:ListBucket`): re-run it. Until then jobs log warnings while the function starts. |
| Lambda function warns about the network | `network=none` but the function is not in a VPC (jobs would have internet), or the reverse: redeploy with the matching `NETWORK` or change the node. |
| S3: `NoSuchBucket`, `PermanentRedirect` | Wrong bucket name, or the bucket is in another region than the node. |
| Test job: "did not start within … s" | The function fails before the handler runs: look at Function logs (needs `LOGS=1` for `NETWORK=internet`), the image architecture, the account's concurrency. |
| Function logs: `AccessDenied…` | The server's IAM policy is from an older `deploy.sh`: re-run it. |
| Function logs: "no log group …" | The function never wrote logs: deploy with `LOGS=1` (or it has not run yet). |
| `deploy.sh` stops at `put-function-concurrency` ("UnreservedConcurrentExecution below its minimum value of [100]") | The account's Lambda concurrency quota is too low (new accounts may get 10): request more in Service Quotas, then run it again. |

## Permissions

- **Server's IAM user** (`fast-compute-server`, inline policy `fast-compute-<function>`):
  invoke this function and read its configuration, reserved concurrency and async settings;
  put/get/delete objects under `fast-compute/` in the bucket and list the bucket (so S3 answers
  404, not 403, for a job file that is not written yet); read the function's log group
  (`FilterLogEvents`, `GetLogEvents`, `DescribeLogStreams`). Nothing else.
- **Execution role** (`<function>-exec`): no permissions of its own; `AWSLambdaVPCAccessExecutionRole`
  with `NETWORK=none`; `fast-compute-logs` with `LOGS=1`.
- **VPC endpoint** (`NETWORK=none`): only `GetObject` / `PutObject` under `fast-compute/` in the bucket.

Objects left behind expire after 2 days (bucket lifecycle rule on `fast-compute/`).
