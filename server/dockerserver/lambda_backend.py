"""AWS Lambda as a compute node (one Lambda invocation = one job).

Configured as a DOCKERSERVER_NODES entry with "type": "lambda" (see config.py). The function
is the image built from lambda-node/ (handler.py); deploy it with lambda-node/deploy.sh.

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


class LambdaBackend(Backend):
    kind = "lambda"

    def __init__(self, name: str, *, function: str, bucket: str, region: str | None = None,
                 prefix: str = "fast-compute/jobs/", images: list[str] | None = None,
                 max_concurrency: int = 10, network: str = "none", profile: str | None = None,
                 start_timeout: float = 300, stale_after: float = 60, poll_interval: float = 2.0,
                 fresh_sandbox: bool = True, max_log_bytes: int = 1024 * 1024,
                 s3_endpoint_url: str | None = None, lambda_endpoint_url: str | None = None,
                 aws_access_key_id: str | None = None, aws_secret_access_key: str | None = None,
                 lambda_client=None, s3_client=None, invoker=None):
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
        # keys set by an admin in /admin-ui (lambda_admin.py); None = the standard boto3 chain
        self._creds = ({"aws_access_key_id": aws_access_key_id, "aws_secret_access_key": aws_secret_access_key}
                       if aws_access_key_id and aws_secret_access_key else {})
        self._s3_endpoint, self._lambda_endpoint = s3_endpoint_url, lambda_endpoint_url
        self._invoker = invoker          # tests: callable(event) instead of a real invoke
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

    # ---- AWS clients (credentials: the standard boto3 chain, e.g. AWS_* in server/.env) ----
    def _session(self):
        import boto3
        return boto3.session.Session(profile_name=None if self._creds else self._profile,
                                     region_name=self.region, **self._creds)

    def client(self, service: str):
        """Another AWS client with this node's credentials and region (diagnostics: sts)."""
        from botocore.config import Config
        return self._session().client(service, config=Config(
            retries={"max_attempts": 2, "mode": "standard"}, connect_timeout=5, read_timeout=15))

    @property
    def credential_source(self) -> str:
        if self._creds:
            return "admin"
        return f"profile {self._profile}" if self._profile else "environment"

    @property
    def lam(self):
        if self._lambda is None:
            from botocore.config import Config
            self._lambda = self._session().client(
                "lambda", endpoint_url=self._lambda_endpoint,
                config=Config(retries={"max_attempts": 3, "mode": "standard"},
                              connect_timeout=5, read_timeout=30))
        return self._lambda

    @property
    def s3(self):
        if self._s3 is None:
            from botocore.config import Config
            endpoint = self._s3_endpoint or (f"https://s3.{self.region}.amazonaws.com" if self.region else None)
            self._s3 = self._session().client(
                "s3", endpoint_url=endpoint,
                config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"},
                              retries={"max_attempts": 3, "mode": "standard"},
                              connect_timeout=5, read_timeout=30))
        return self._s3

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

    def _get(self, job: str, name: str) -> tuple[bytes | None, str | None]:
        """(body, etag); (None, None) if the object does not exist. Raises BackendError."""
        try:
            r = self.s3.get_object(Bucket=self.bucket, Key=self._key(job, name))
            return r["Body"].read(), r.get("ETag")
        except Exception as e:
            code = getattr(e, "response", {}).get("Error", {}).get("Code") if hasattr(e, "response") else None
            if code in ("NoSuchKey", "404", "NotFound"):
                return None, None
            raise BackendError(f"lambda node {self.name}: S3 {type(e).__name__}: {e}") from e

    def _poll(self, h: Handle) -> ExecStatus | None:
        now = time.time()
        invoked = h.extra.get("invoked", now)
        try:
            body, etag = self._get(h.id, "status.json")
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

    def cleanup(self, h: Handle) -> None:
        try:
            for name in ("workspace.tar", "status.json", "output.log", "kill"):
                self.s3.delete_object(Bucket=self.bucket, Key=self._key(h.id, name))
        except Exception as e:
            log.warning("cleanup %s on %s: %s", h.id, self.name, e)
        self._forget(h.id)

    def adopt(self, job: str):
        """recover(): a job started by an earlier server process counts against the slots."""
        with self._lock:
            self._inflight.add(job)
