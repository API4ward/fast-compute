"""AWS Lambda as a compute node (one Lambda invocation = one job).

Configured as a DOCKERSERVER_NODES entry with "type": "lambda" (see config.py), or added live
in /admin-ui -> Settings (admin_settings.py). The function is the image built from lambda-node/
(handler.py); deploy it with lambda-node/deploy.sh.

AWS credentials: the node's `profile` (.env nodes only) > the access key saved in /admin-ui ->
Settings (the `credentials` callable) > boto3's default chain (AWS_* in server/.env, an
instance role, ...).

How a job runs:
  start   -> workspace tar to s3://<bucket>/<prefix><job>/workspace.tar, then an async
             (InvocationType=Event) invoke whose event holds the job and pre-signed URLs
  status  <- the handler PUTs status.json (with a heartbeat every ~10 s) and the log tail
  kill    -> this side PUTs a "kill" object; the handler polls it and kills the job
  cleanup -> the job's S3 objects are deleted
The handler needs no AWS permissions (it only uses the pre-signed URLs), so the execution
role's credentials, which job code can read in a Lambda, give access to nothing.

Capacity, as the scheduler sees it: "capacity" is what ONE invocation offers (the function's
memory, and vCPUs = memory / 1769 MB as Lambda allocates them); "free" is the same while fewer
than max_concurrency jobs run here, else 0. So a job fits if it fits one invocation, and a
Docker node with more free room is preferred while it has room.

Per-job conditions (rejects()): the job's image must be one this function stands in for, its
timeout must fit the function timeout (max 15 min) minus a margin, and its network flag must
match the function's network ("none": function in a VPC without internet; "internet": no VPC).

Run time for billing is measured on this side: from the invoke until this server first sees
the final status (cold start included, plus up to one poll interval), never from what the
job's environment reports.
"""
import json
import logging
import threading
import time

from .backends import Backend, BackendError, ExecStatus, Handle, JobSpec, NoCapacity, node_workspace

log = logging.getLogger("dockerserver.lambda")

MB_PER_VCPU = 1769          # Lambda gives 1 vCPU per 1769 MB of memory
MAX_VCPUS = 6.0
TIMEOUT_MARGIN = 30         # function timeout minus this = longest job timeout accepted
# settings that can change while jobs run (reconfigure()); they apply to new jobs
LIVE_KEYS = ("max_concurrency", "images", "network", "start_timeout", "stale_after", "fresh_sandbox")


def function_name(function: str) -> str:
    """Function name from a name or an ARN (arn:aws:lambda:<region>:<account>:function:<name>[:q])."""
    return function.split(":")[6] if function.startswith("arn:") and function.count(":") >= 6 else function


class LambdaBackend(Backend):
    kind = "lambda"

    def __init__(self, name: str, *, function: str, bucket: str, region: str | None = None,
                 prefix: str = "fast-compute/jobs/", images: list[str] | None = None,
                 max_concurrency: int = 10, network: str = "none", profile: str | None = None,
                 start_timeout: float = 300, stale_after: float = 60, poll_interval: float = 2.0,
                 fresh_sandbox: bool = True, max_log_bytes: int = 1024 * 1024,
                 s3_endpoint_url: str | None = None, lambda_endpoint_url: str | None = None,
                 lambda_client=None, s3_client=None, invoker=None, credentials=None,
                 client_factory=None, source: str = "env"):
        if network not in ("none", "internet"):
            raise ValueError(f"lambda node {name}: network must be 'none' or 'internet'")
        self.name, self.function, self.bucket, self.region = name, function, bucket, region
        self.prefix = prefix if prefix.endswith("/") or not prefix else prefix + "/"
        self.images = list(images or ["python:3.12-slim"])
        self.max_concurrency = max(int(max_concurrency), 1)
        self.network = network
        self.start_timeout, self.stale_after = start_timeout, stale_after
        self.poll_interval, self.fresh_sandbox = poll_interval, fresh_sandbox
        self.max_log_bytes = max_log_bytes
        self.url = f"lambda://{region or 'default'}/{function}"
        self._lambda, self._s3 = lambda_client, s3_client
        self._profile = profile
        self._s3_endpoint, self._lambda_endpoint = s3_endpoint_url, lambda_endpoint_url
        self._invoker = invoker          # tests: callable(event) instead of a real invoke
        # callable -> {"aws_access_key_id", "aws_secret_access_key"} or None (admin Settings)
        self.credentials = credentials
        self._client_factory = client_factory   # tests: callable(service, region) -> client
        self._injected = {"lambda": lambda_client is not None, "s3": s3_client is not None}
        self.source = source             # "env" (DOCKERSERVER_NODES) or "admin" (Settings)
        self._retired = False            # replaced or removed in Settings: takes no new job
        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self._fn: dict | None = None     # last GetFunctionConfiguration answer (subset)
        self._info: dict | None = None
        self._info_at = 0.0
        self.last_ok: float | None = None
        self.last_check: float | None = None
        self.last_error: str | None = None
        self.down_since: float | None = None
        self.latency_ms: float | None = None

    def __repr__(self):
        return f"LambdaBackend({self.name!r}, {self.url!r})"

    # ---- AWS clients (credentials: see the module docstring) ----
    def credentials_source(self) -> str:
        """"profile", "admin" (saved in Settings) or "env" (boto3's default chain)."""
        if self._profile:
            return "profile"
        has = getattr(self.credentials, "available", None)
        return "admin" if callable(has) and has() else "env"

    def _session(self):
        import boto3
        if self._profile:
            return boto3.session.Session(profile_name=self._profile, region_name=self.region)
        creds = self.credentials() if self.credentials is not None else None
        return boto3.session.Session(region_name=self.region, **(creds or {}))

    @property
    def lam(self):
        c = self._lambda
        if c is None:
            if self._client_factory is not None:
                c = self._client_factory("lambda", self.region)
            else:
                from botocore.config import Config
                c = self._session().client(
                    "lambda", endpoint_url=self._lambda_endpoint,
                    config=Config(retries={"max_attempts": 3, "mode": "standard"},
                                  connect_timeout=5, read_timeout=30))
            self._lambda = c
        return c

    @property
    def s3(self):
        c = self._s3
        if c is None:
            if self._client_factory is not None:
                c = self._client_factory("s3", self.region)
            else:
                from botocore.config import Config
                endpoint = self._s3_endpoint or (f"https://s3.{self.region}.amazonaws.com" if self.region else None)
                c = self._session().client(
                    "s3", endpoint_url=endpoint,
                    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"},
                                  retries={"max_attempts": 3, "mode": "standard"},
                                  connect_timeout=5, read_timeout=30))
            self._s3 = c
        return c

    def reset_clients(self):
        """The saved AWS credentials changed: build new clients on next use (injected ones stay;
        a call in progress finishes with the old client)."""
        if not self._injected["lambda"]:
            self._lambda = None
        if not self._injected["s3"]:
            self._s3 = None

    # ---- live changes (admin Settings) ----
    def config(self) -> dict:
        """This node's settings (no credentials), e.g. for diagnostics."""
        return {"name": self.name, "function": self.function, "bucket": self.bucket,
                "region": self.region, "prefix": self.prefix, "images": list(self.images),
                "max_concurrency": self.max_concurrency, "network": self.network,
                "start_timeout": self.start_timeout, "stale_after": self.stale_after,
                "fresh_sandbox": self.fresh_sandbox, "profile": self._profile,
                "s3_endpoint_url": self._s3_endpoint, "lambda_endpoint_url": self._lambda_endpoint}

    def reconfigure(self, **opts):
        """Change settings that running jobs do not depend on (LIVE_KEYS); new jobs use them."""
        bad = set(opts) - set(LIVE_KEYS)
        if bad:
            raise ValueError(f"lambda node {self.name}: {sorted(bad)} cannot change while jobs run")
        if opts.get("network", self.network) not in ("none", "internet"):
            raise ValueError(f"lambda node {self.name}: network must be 'none' or 'internet'")
        with self._lock:
            if "max_concurrency" in opts:
                self.max_concurrency = max(int(opts["max_concurrency"]), 1)
            if "images" in opts:
                self.images = list(opts["images"])
            for k in ("network", "start_timeout", "stale_after", "fresh_sandbox"):
                if k in opts:
                    setattr(self, k, opts[k])

    def active_count(self) -> int:
        """Invocations this server has in flight here (jobs and admin test jobs)."""
        with self._lock:
            return len(self._inflight)

    def retire_if_idle(self) -> bool:
        """Take this backend out of service if nothing runs on it (it is being replaced or
        removed): a later start() raises NoCapacity, so the scheduler moves on. False if busy."""
        with self._lock:
            if self._inflight:
                return False
            self._retired = True
            return True

    def unretire(self):
        with self._lock:
            self._retired = False

    def _key(self, job: str, name: str) -> str:
        return f"{self.prefix}{job}/{name}"

    # ---- capacity / health ----
    @staticmethod
    def vcpus(memory_mb: int) -> float:
        return round(min(memory_mb / MB_PER_VCPU, MAX_VCPUS), 2)

    def info(self, max_age: float = 0) -> dict | None:
        if self._info is not None and time.monotonic() - self._info_at <= max_age:
            return self._info_with_slots()
        return self._fetch_info()

    def probe(self, timeout: float = 4.0) -> dict | None:
        return self._fetch_info()

    def _fetch_info(self) -> dict | None:
        t0 = time.monotonic()
        self.last_check = time.time()
        try:
            c = self.lam.get_function_configuration(FunctionName=self.function)
            state, upd = c.get("State", "Active"), c.get("LastUpdateStatus", "Successful")
            if state != "Active":
                raise BackendError(f"function state is {state}: {c.get('StateReason') or ''}".strip())
            self._fn = {"memory_mb": int(c["MemorySize"]), "timeout": int(c["Timeout"]),
                        "state": state, "last_update": upd,
                        "architectures": c.get("Architectures") or ["x86_64"],
                        "in_vpc": bool((c.get("VpcConfig") or {}).get("SubnetIds"))}
            self._info = {"capacity": {"cpus": self.vcpus(self._fn["memory_mb"]),
                                       "mem_mb": self._fn["memory_mb"]}}
            self._info_at = time.monotonic()
            self.latency_ms = round((time.monotonic() - t0) * 1000, 1)
            self.last_ok, self.last_error, self.down_since = time.time(), None, None
        except Exception as e:  # botocore errors, missing credentials, BackendError
            msg = f"{type(e).__name__}: {e}" if not isinstance(e, BackendError) else str(e)
            if self.last_error is None:
                log.warning("lambda node %s: %s", self.name, msg)
            self._info, self._info_at = None, time.monotonic()
            self.last_error = msg[:500]
            self.down_since = self.down_since or time.time()
            return None
        return self._info_with_slots()

    def _info_with_slots(self) -> dict:
        cap = self._info["capacity"]
        with self._lock:
            busy = len(self._inflight)
        full = busy >= self.max_concurrency
        free = {"cpus": 0.0, "mem_mb": 0} if full else dict(cap)
        return {"kind": self.kind, "capacity": dict(cap), "free": free,
                "used": {"cpus": round(cap["cpus"] - free["cpus"], 2), "mem_mb": cap["mem_mb"] - free["mem_mb"]},
                "active_jobs": busy, "slots": {"max": self.max_concurrency, "used": busy},
                "features": ["env"], "allowed_images": list(self.images),
                "allow_network": self.network == "internet", "network": self.network,
                "function": self.function, "region": self.region,
                "timeout_seconds": self._fn["timeout"] if self._fn else None,
                "max_job_seconds": self.max_job_seconds(),
                "in_vpc": self._fn.get("in_vpc") if self._fn else None,
                "architectures": self._fn.get("architectures") if self._fn else None}

    def health(self) -> dict:
        return {"name": self.name, "url": self.url, "kind": self.kind,
                "online": self._info is not None,
                "info": self._info_with_slots() if self._info is not None else None,
                "last_ok": self.last_ok, "last_check": self.last_check,
                "last_error": self.last_error, "down_since": self.down_since,
                "latency_ms": self.latency_ms}

    def invalidate(self):
        pass        # slots are counted locally; function config changes rarely

    def max_job_seconds(self) -> int | None:
        return max(self._fn["timeout"] - TIMEOUT_MARGIN, 1) if self._fn else None

    def rejects(self, image: str | None, timeout_seconds: int | None, network: bool | None) -> str | None:
        if image is not None and image not in self.images:
            return f"image {image} is not available on Lambda ({', '.join(self.images)})"
        if network is not None and bool(network) != (self.network == "internet"):
            return ("network jobs need a Lambda node with internet" if network
                    else "jobs without network need a Lambda node without internet")
        mx = self.max_job_seconds()
        if timeout_seconds is not None and mx is not None and timeout_seconds > mx:
            return f"timeout {timeout_seconds} s is longer than Lambda allows here ({mx} s)"
        return None

    # ---- jobs ----
    def start(self, spec: JobSpec, payload: bytes) -> Handle:
        why = self.rejects(spec.image, spec.timeout_seconds, spec.network)
        if why:
            raise BackendError(f"lambda node {self.name}: {why}")
        with self._lock:
            if self._retired:
                raise NoCapacity(f"lambda node {self.name} was reconfigured")
            if len(self._inflight) >= self.max_concurrency:
                raise NoCapacity(f"lambda node {self.name}: all {self.max_concurrency} slots busy")
            job = f"ds{spec.job_id}-{int(time.time())}"
            self._inflight.add(job)
        try:
            expires = int(spec.timeout_seconds + self.start_timeout + 900)
            ws = node_workspace(payload)
            if ws:
                self.s3.put_object(Bucket=self.bucket, Key=self._key(job, "workspace.tar"), Body=ws)

            def url(method, name):
                return self.s3.generate_presigned_url(
                    method, Params={"Bucket": self.bucket, "Key": self._key(job, name)},
                    ExpiresIn=expires)
            event = {"job": job, "command": spec.command, "timeout_seconds": spec.timeout_seconds,
                     "env": spec.env, "max_log_bytes": self.max_log_bytes,
                     "fresh_sandbox": self.fresh_sandbox,
                     "workspace_url": url("get_object", "workspace.tar") if ws else None,
                     "status_url": url("put_object", "status.json"),
                     "logs_url": url("put_object", "output.log"),
                     "kill_url": url("get_object", "kill")}
            body = json.dumps(event).encode()
            if self._invoker is not None:
                self._invoker(event)
            else:
                r = self.lam.invoke(FunctionName=self.function, InvocationType="Event", Payload=body)
                if r.get("StatusCode") != 202:
                    raise BackendError(f"lambda node {self.name}: invoke returned {r.get('StatusCode')}")
        except BackendError:
            self._forget(job)
            raise
        except Exception as e:
            self._forget(job)
            raise BackendError(f"lambda node {self.name}: {type(e).__name__}: {e}") from e
        now = time.time()
        self.last_ok = now
        return Handle(self, job, self.name, extra={"invoked": now, "etag": None, "changed": now})

    def _forget(self, job: str):
        with self._lock:
            self._inflight.discard(job)

    def _get(self, job: str, name: str, denied_is_missing: bool = False) -> tuple[bytes | None, str | None]:
        """(body, etag); (None, None) if the object does not exist. Raises BackendError.
        denied_is_missing: also (None, None) for AccessDenied, which is what S3 answers for a
        missing object when the caller may not s3:ListBucket (the server's IAM policy from
        deploy.sh before Settings existed); the caller decides how long to believe that."""
        try:
            r = self.s3.get_object(Bucket=self.bucket, Key=self._key(job, name))
            return r["Body"].read(), r.get("ETag")
        except Exception as e:
            code = getattr(e, "response", {}).get("Error", {}).get("Code") if hasattr(e, "response") else None
            if code in ("NoSuchKey", "404", "NotFound"):
                return None, None
            if denied_is_missing and code in ("AccessDenied", "403", "Forbidden"):
                return None, None
            raise BackendError(f"lambda node {self.name}: S3 {type(e).__name__}: {e}") from e

    def _poll(self, h: Handle) -> ExecStatus | None:
        now = time.time()
        # a job recovered after a restart has no "invoked": its start timeout counts from now
        invoked = h.extra.setdefault("invoked", now)
        try:
            # until the invocation first reports, a 403 may only mean "no status.json yet"
            body, etag = self._get(h.id, "status.json", denied_is_missing="first_seen" not in h.extra)
        except BackendError as e:
            h.unreachable_since = h.unreachable_since or now
            log.warning("job %s: %s", h.id, e)
            return None
        h.unreachable_since, h.last_seen = None, now
        self.last_ok = now
        if h.extra.get("final") is not None:
            return h.extra["final"]
        if body is None:
            if now - invoked > self.start_timeout:
                return self._final(h, ExecStatus(
                    finished=True, node_status="error",
                    error=f"the Lambda function did not start the job within {int(self.start_timeout)} s"))
            st = ExecStatus(finished=False, node_status="preparing")
            h.last_status = st
            return st
        h.extra.setdefault("first_seen", now)    # the invocation reported (cold start over)
        if etag != h.extra.get("etag"):
            h.extra["etag"], h.extra["changed"] = etag, now
        try:
            d = json.loads(body)
        except ValueError:
            d = {}
        status = d.get("status") or "preparing"
        stats = dict(stat_samples=d.get("stat_samples") or 0, cpu_avg_pct=d.get("cpu_avg_pct"),
                     cpu_peak_pct=d.get("cpu_peak_pct"), mem_avg_mb=d.get("mem_avg_mb"),
                     mem_peak_mb=d.get("mem_peak_mb"))
        if status in ("preparing", "running"):
            if now - h.extra.get("changed", now) > self.stale_after:
                return self._final(h, ExecStatus(
                    finished=True, node_status="error", **stats,
                    error=(f"the Lambda invocation stopped reporting for {int(self.stale_after)} s "
                           "(out of memory, crashed, or hit the function timeout)")))
            st = ExecStatus(finished=False, node_status=status, **stats)
            h.last_status = st
            return st
        return self._final(h, ExecStatus(
            finished=True, exit_code=d.get("exit_code"), node_status=status,
            error=d.get("error"), oom=False, **stats))

    def _final(self, h: Handle, st: ExecStatus) -> ExecStatus:
        # billed run time: our own clock, invoke -> first sight of the end
        st.duration_seconds = max(time.time() - h.extra.get("invoked", time.time()), 0.0)
        h.extra["final"] = h.last_status = st
        return st

    def wait(self, h: Handle, timeout: float) -> bool:
        end = time.monotonic() + max(timeout, 0)
        while True:
            st = self._poll(h)
            if st is not None and st.finished:
                return True
            left = end - time.monotonic()
            if left <= 0:
                return False
            time.sleep(min(self.poll_interval, left))

    def status(self, h: Handle) -> ExecStatus:
        st = self._poll(h) or h.last_status
        if st is None:
            raise BackendError(f"lambda node {self.name}: status unavailable")
        return st

    def kill(self, h: Handle) -> None:
        try:
            self.s3.put_object(Bucket=self.bucket, Key=self._key(h.id, "kill"), Body=b"kill")
        except Exception as e:
            log.warning("kill %s on %s: %s", h.id, self.name, e)

    def logs(self, h: Handle, tail: int | None = None) -> bytes:
        try:
            body, _ = self._get(h.id, "output.log")
        except BackendError:
            return b""
        body = body or b""
        if tail:
            body = b"".join(body.splitlines(keepends=True)[-tail:])
        return body

    def cleanup(self, h: Handle) -> bool:
        """Delete the job's files in S3 and free its slot. False if a delete failed (the files
        then expire with the bucket's lifecycle rule)."""
        ok = True
        try:
            for name in ("workspace.tar", "status.json", "output.log", "kill"):
                self.s3.delete_object(Bucket=self.bucket, Key=self._key(h.id, name))
        except Exception as e:
            ok = False
            log.warning("cleanup %s on %s: %s", h.id, self.name, e)
        self._forget(h.id)
        return ok

    def adopt(self, job: str):
        """recover(): a job started by an earlier server process counts against the slots."""
        with self._lock:
            self._inflight.add(job)
