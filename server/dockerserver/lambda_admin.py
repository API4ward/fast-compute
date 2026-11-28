"""AWS Lambda nodes managed from /admin-ui (no .env edit, no restart).

Kept in <data_dir>/lambda.json (mode 600):
  {"credentials": {"access_key_id": <enc>, "secret_access_key": <enc>, "updated_at", "updated_by"},
   "nodes": [{"name", "function", "bucket", "region", ...LAMBDA_KEYS}]}
The two keys are encrypted with DOCKERSERVER_SECRET_KEY (Fernet, like users' env vars) and are
never returned by the API: the admin sees only the last 4 characters of the key id.

Saved nodes are added to the scheduler's pool at start and whenever they change. Nodes from
DOCKERSERVER_NODES in server/.env stay as they are (read-only here, listed for reference) and
use the standard boto3 credential chain; nodes saved here use the saved keys when there are any.

diagnose() checks a node step by step - boto3, credentials (STS), the function, S3 read/write,
optionally a real test job - so an admin can see which part is wrong.
"""
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken  # noqa: F401

from .backends import JobSpec
from .config import LAMBDA_KEYS

log = logging.getLogger("dockerserver.lambda_admin")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
FUNCTION_RE = re.compile(r"^[A-Za-z0-9_:.-]{1,140}$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
KEY_ID_RE = re.compile(r"^[A-Z0-9]{16,128}$")
EDITABLE = LAMBDA_KEYS - {"profile"}       # a profile makes no sense when keys are saved here
MAX_TEST_SECONDS = 60          # + 30 s for the cold start: stays under proxy timeouts (100 s)


class LambdaConfigError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_node(name: str, raw: dict) -> dict:
    """Validate one node's settings (as sent by the admin UI). Returns the stored dict."""
    if not isinstance(raw, dict):
        raise LambdaConfigError("node settings must be an object")
    if not NAME_RE.match(name or "") or name == "local":
        raise LambdaConfigError("name: letters, digits, _ . - (max 40), not 'local'")
    unknown = set(raw) - EDITABLE - {"name", "type"}
    if unknown:
        raise LambdaConfigError(f"unknown settings: {', '.join(sorted(unknown))}")
    out: dict = {"name": name}
    fn, bucket = str(raw.get("function") or "").strip(), str(raw.get("bucket") or "").strip()
    if not FUNCTION_RE.match(fn):
        raise LambdaConfigError("function: the Lambda function name or ARN is required")
    if not BUCKET_RE.match(bucket):
        raise LambdaConfigError("bucket: a valid S3 bucket name is required")
    out["function"], out["bucket"] = fn, bucket
    region = str(raw.get("region") or "").strip()
    if region and not REGION_RE.match(region):
        raise LambdaConfigError("region: e.g. eu-west-2")
    if region:
        out["region"] = region
    network = raw.get("network") or "none"
    if network not in ("none", "internet"):
        raise LambdaConfigError("network must be 'none' or 'internet'")
    out["network"] = network
    prefix = str(raw.get("prefix") or "fast-compute/jobs/").strip().lstrip("/")
    if not re.match(r"^[A-Za-z0-9_./-]{1,100}$", prefix) or ".." in prefix:
        raise LambdaConfigError("prefix: letters, digits, _ . / - only")
    out["prefix"] = prefix if prefix.endswith("/") else prefix + "/"
    images = raw.get("images") or ["python:3.12-slim"]
    if isinstance(images, str):
        images = [s.strip() for s in images.split(",")]
    images = [i for i in images if i]
    if not images or not all(isinstance(i, str) and re.match(r"^[A-Za-z0-9_./:@-]{1,200}$", i) for i in images):
        raise LambdaConfigError("images: comma-separated image names")
    out["images"] = images
    try:
        def num(k, d):
            v = raw.get(k)
            return d if v is None or v == "" else v
        out["max_concurrency"] = int(num("max_concurrency", 10))
        for k, d, lo, hi in (("start_timeout", 300, 10, 900), ("stale_after", 60, 10, 900)):
            v = float(num(k, d))
            if not lo <= v <= hi:
                raise LambdaConfigError(f"{k} must be between {lo} and {hi} s")
            out[k] = v
    except (TypeError, ValueError) as e:
        if isinstance(e, LambdaConfigError):
            raise
        raise LambdaConfigError(f"invalid number: {e}")
    if not 1 <= out["max_concurrency"] <= 1000:
        raise LambdaConfigError("max_concurrency must be 1..1000")
    out["fresh_sandbox"] = bool(raw.get("fresh_sandbox", True))
    for k in ("s3_endpoint_url", "lambda_endpoint_url"):
        v = str(raw.get(k) or "").strip()
        if v:
            if not re.match(r"^https?://[A-Za-z0-9.:_-]+/?$", v):
                raise LambdaConfigError(f"{k}: an http(s)://host[:port] URL")
            out[k] = v
    return out


class LambdaAdmin:
    def __init__(self, settings, pool, fernet: Fernet):
        self.settings, self.pool, self.f = settings, pool, fernet
        self.path: Path = settings.data_dir / "lambda.json"
        self._lock = threading.Lock()
        self.load_error: str | None = None
        self.data = self._load()
        self.env_nodes = {n.name for n in getattr(settings, "nodes", []) or []}

    # ---- storage ----
    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text())
            if not isinstance(d, dict):
                raise ValueError("not an object")
        except FileNotFoundError:
            return {"credentials": None, "nodes": []}
        except (OSError, ValueError) as e:
            self.load_error = f"cannot read {self.path.name}: {e}"
            log.warning(self.load_error)
            return {"credentials": None, "nodes": []}
        d.setdefault("credentials", None)
        d.setdefault("nodes", [])
        return d

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(self.data, fh, indent=1)
            fh.write("\n")
        os.replace(tmp, self.path)

    def _keys(self) -> tuple[str | None, str | None]:
        c = self.data.get("credentials") or {}
        try:
            return (self.f.decrypt(c["access_key_id"].encode()).decode(),
                    self.f.decrypt(c["secret_access_key"].encode()).decode())
        except (KeyError, InvalidToken, AttributeError):
            return None, None

    # ---- backends ----
    def _backend(self, node: dict, **extra):
        from .lambda_backend import LambdaBackend
        kid, secret = self._keys()
        opts = {k: v for k, v in node.items() if k in EDITABLE}
        return LambdaBackend(node["name"], poll_interval=self.settings.node_poll_interval,
                             max_log_bytes=self.settings.max_log_bytes,
                             aws_access_key_id=kid, aws_secret_access_key=secret, **opts, **extra)

    def _install(self, node: dict):
        """Put the node into the pool. Slot accounting is shared with the replaced backend so
        jobs still running on the old object keep counting against max_concurrency."""
        try:
            b = self._backend(node)
        except Exception as e:      # e.g. bad option combination
            log.warning("lambda node %s not installed: %s", node.get("name"), e)
            return
        old = self.pool.by_name(node["name"])
        if old is not None and getattr(old, "kind", None) == "lambda":
            b._lock, b._inflight = old._lock, old._inflight
        self.pool.put_node(b)

    def install_all(self):
        for n in self.data["nodes"]:
            if n.get("name") in self.env_nodes:
                log.warning("lambda node %s is also in DOCKERSERVER_NODES; .env wins", n["name"])
                continue
            self._install(n)

    # ---- views ----
    def status(self) -> dict:
        try:
            import boto3  # noqa: F401
            boto = True
        except ImportError:
            boto = False
        c = self.data.get("credentials")
        kid, _ = self._keys()
        env_creds = bool(os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"))
        health = {h["name"]: h for h in self.pool.health()}
        nodes = []
        for n in self.data["nodes"]:
            h = health.get(n["name"]) or {}
            nodes.append({**n, "managed": True, "shadowed_by_env": n["name"] in self.env_nodes,
                          "online": h.get("online"), "error": h.get("last_error"),
                          "draining": h.get("draining")})
        env_nodes = []
        for b in self.pool.nodes:
            if b.name in self.env_nodes and getattr(b, "kind", None) == "lambda":
                h = health.get(b.name) or {}
                env_nodes.append({"name": b.name, "function": b.function, "bucket": b.bucket,
                                  "region": b.region, "network": b.network,
                                  "credentials": b.credential_source, "online": h.get("online"),
                                  "error": h.get("last_error")})
        return {"boto3": boto, "load_error": self.load_error,
                "credentials": {"set": bool(kid), "key_id_hint": ("…" + kid[-4:]) if kid else None,
                                "updated_at": (c or {}).get("updated_at"),
                                "updated_by": (c or {}).get("updated_by"),
                                "environment_keys": env_creds,
                                "in_use": "saved keys" if kid else ("AWS_* in server/.env" if env_creds
                                                                    else "boto3 default chain")},
                "nodes": nodes, "env_nodes": env_nodes,
                "defaults": {"prefix": "fast-compute/jobs/", "images": ["python:3.12-slim"],
                             "max_concurrency": 10, "network": "none", "start_timeout": 300,
                             "stale_after": 60, "fresh_sandbox": True}}

    # ---- changes ----
    def set_credentials(self, key_id: str, secret: str, by: str):
        key_id, secret = (key_id or "").strip(), (secret or "").strip()
        if not KEY_ID_RE.match(key_id):
            raise LambdaConfigError("access key id: upper-case letters and digits, e.g. AKIA…")
        if not 16 <= len(secret) <= 256 or any(c.isspace() for c in secret):
            raise LambdaConfigError("secret access key looks wrong (16-256 characters, no spaces)")
        with self._lock:
            self.data["credentials"] = {"access_key_id": self.f.encrypt(key_id.encode()).decode(),
                                        "secret_access_key": self.f.encrypt(secret.encode()).decode(),
                                        "updated_at": _now(), "updated_by": by}
            self._save()
            self.install_all()
        log.warning("lambda: AWS keys set by %s (key id …%s)", by, key_id[-4:])

    def clear_credentials(self, by: str):
        with self._lock:
            self.data["credentials"] = None
            self._save()
            self.install_all()
        log.warning("lambda: AWS keys removed by %s", by)

    def put_node(self, name: str, raw: dict, by: str) -> dict:
        if name in self.env_nodes:
            raise LambdaConfigError(f"{name} is defined in server/.env (DOCKERSERVER_NODES); "
                                    "pick another name or edit .env")
        existing = self.pool.by_name(name)
        if existing is not None and getattr(existing, "kind", None) != "lambda":
            raise LambdaConfigError(f"a Docker node is already called {name}")
        node = clean_node(name, raw)
        with self._lock:
            self.data["nodes"] = [n for n in self.data["nodes"] if n.get("name") != name] + [node]
            self._save()
            self._install(node)
        log.warning("lambda node %s saved by %s (function %s)", name, by, node["function"])
        return node

    def delete_node(self, name: str, by: str, running_jobs: int) -> bool:
        if name in self.env_nodes:
            raise LambdaConfigError(f"{name} is defined in server/.env; remove it there")
        if not any(n.get("name") == name for n in self.data["nodes"]):
            return False
        if running_jobs:
            raise LambdaConfigError(f"{running_jobs} job(s) still running on {name}; "
                                    "drain the node and wait for them to finish")
        with self._lock:
            self.data["nodes"] = [n for n in self.data["nodes"] if n.get("name") != name]
            self._save()
            self.pool.remove_node(name)
        log.warning("lambda node %s deleted by %s", name, by)
        return True

    # ---- debugging ----
    def diagnose(self, name: str | None = None, node: dict | None = None, *, run_job: bool = False,
                 command: str | None = None, timeout_seconds: int = 30) -> dict:
        """Step-by-step check of a saved node (name) or of unsaved settings (node). Each step:
        {step, ok, ms, detail}; a failed step skips the ones that depend on it."""
        steps: list[dict] = []

        def step(label, fn):
            t0 = time.monotonic()
            try:
                detail = fn()
                ok = True
            except Exception as e:     # botocore errors, timeouts, our own checks
                detail, ok = _err(e), False
            steps.append({"step": label, "ok": ok, "ms": round((time.monotonic() - t0) * 1000),
                          "detail": detail})
            return ok

        def result(target):
            return {"target": target, "ok": all(s["ok"] for s in steps), "steps": steps, "at": _now()}

        try:
            import boto3
            steps.append({"step": "boto3", "ok": True, "ms": 0, "detail": f"boto3 {boto3.__version__}"})
        except ImportError:
            steps.append({"step": "boto3", "ok": False, "ms": 0,
                          "detail": "boto3 is not installed: pip install -r requirements.txt in server/"})
            return result(name or (node or {}).get("name"))

        if node is not None:
            try:
                cfg = clean_node(node.get("name") or name or "test", node)
            except LambdaConfigError as e:
                steps.append({"step": "settings", "ok": False, "ms": 0, "detail": str(e)})
                return result(node.get("name"))
            b = self._backend(cfg)
        else:
            b = self.pool.by_name(name)
            if b is None or getattr(b, "kind", None) != "lambda":
                steps.append({"step": "settings", "ok": False, "ms": 0, "detail": f"no Lambda node named {name}"})
                return result(name)
        target = b.name
        steps.append({"step": "settings", "ok": True, "ms": 0,
                      "detail": f"function {b.function}, bucket {b.bucket}, region {b.region or '(default)'}, "
                                f"network {b.network}, credentials: {b.credential_source}"})

        def creds():
            ident = b.client("sts").get_caller_identity()
            return f"account {ident.get('Account')}, {ident.get('Arn')}"
        step("credentials (sts:GetCallerIdentity)", creds)

        def function():
            c = b.lam.get_function_configuration(FunctionName=b.function)
            in_vpc = bool((c.get("VpcConfig") or {}).get("SubnetIds"))
            notes = []
            if c.get("State") != "Active":
                raise RuntimeError(f"function state {c.get('State')}: {c.get('StateReason') or ''}")
            if c.get("LastUpdateStatus") not in (None, "Successful"):
                notes.append(f"last update {c.get('LastUpdateStatus')}")
            if b.network == "none" and not in_vpc:
                notes.append("WARNING: network=none but the function is not in a VPC (jobs would have internet)")
            if b.network == "internet" and in_vpc:
                notes.append("WARNING: network=internet but the function is in a VPC (may have no internet)")
            mem = int(c.get("MemorySize") or 0)
            return (f"{c.get('PackageType', 'Zip')} {', '.join(c.get('Architectures') or ['x86_64'])}, "
                    f"{mem} MB ≈ {b.vcpus(mem)} vCPU, timeout {c.get('Timeout')} s "
                    f"(longest job {max(int(c.get('Timeout') or 0) - 30, 1)} s), "
                    f"{'in a VPC' if in_vpc else 'no VPC'}, modified {c.get('LastModified', '?')[:19]}"
                    + ("; " + "; ".join(notes) if notes else ""))
        fn_ok = step("function (lambda:GetFunctionConfiguration)", function)

        def retries():
            try:
                c = b.lam.get_function_event_invoke_config(FunctionName=b.function)
            except Exception as e:
                code = getattr(e, "response", {}).get("Error", {}).get("Code", "") if hasattr(e, "response") else ""
                if code == "ResourceNotFoundException":
                    raise RuntimeError("no async invoke config: AWS retries failed jobs twice; "
                                       "deploy.sh sets MaximumRetryAttempts=0")
                if code == "AccessDeniedException":
                    return "not allowed to read (optional check, skipped)"
                raise
            n = c.get("MaximumRetryAttempts")
            if n not in (0, None):
                raise RuntimeError(f"MaximumRetryAttempts={n}: a failed job would run again; set it to 0")
            return f"async retries {n}, max event age {c.get('MaximumEventAgeInSeconds', '?')} s"
        if fn_ok:
            step("async invoke settings", retries)

        probe_key = f"{b.prefix}_diag/{int(time.time() * 1000)}.txt"

        def s3_rw():
            body = f"fast-compute diagnostics {_now()}".encode()
            b.s3.put_object(Bucket=b.bucket, Key=probe_key, Body=body)
            got = b.s3.get_object(Bucket=b.bucket, Key=probe_key)["Body"].read()
            b.s3.delete_object(Bucket=b.bucket, Key=probe_key)
            if got != body:
                raise RuntimeError("read back different content")
            return f"put/get/delete s3://{b.bucket}/{b.prefix}… ok"
        s3_ok = step("S3 read/write (job files)", s3_rw)

        if run_job and fn_ok and s3_ok:
            timeout_seconds = max(5, min(int(timeout_seconds or 30), MAX_TEST_SECONDS))
            cmd = (command or "").strip() or (
                "python -c \"import os,platform,sys;print('hello from', platform.node(), sys.version.split()[0]);"
                "print('cpus', os.cpu_count())\" && cat /proc/meminfo | head -2 && "
                "python -c \"import urllib.request as u;print('internet:', u.urlopen('https://example.com',timeout=5).status)\" "
                "|| echo 'internet: none'")
            steps.append(self._test_job(b, cmd, timeout_seconds))
        elif run_job:
            steps.append({"step": "test job", "ok": False, "ms": 0, "detail": "skipped: an earlier step failed"})
        return result(target)

    def _test_job(self, b, cmd: str, timeout_seconds: int) -> dict:
        t0 = time.monotonic()
        mx = b.max_job_seconds()
        if mx is not None:
            timeout_seconds = min(timeout_seconds, mx)
        spec = JobSpec(job_id=0, image=b.images[0], command=cmd, cpus=0.1, mem_mb=128,
                       timeout_seconds=timeout_seconds, network=b.network == "internet")
        h = None
        try:
            h = b.start(spec, b"")
            done = b.wait(h, timeout_seconds + 30)
            st = b.status(h)
            out = b.logs(h, tail=60).decode(errors="replace")
            if not done:
                b.kill(h)
            ok = done and st.node_status == "succeeded" and st.exit_code == 0
            summary = (f"status {st.node_status}, exit {st.exit_code}"
                       + (f", ran {st.duration_seconds:.1f} s (invoke → result)" if st.duration_seconds else "")
                       + (f", error: {st.error}" if st.error else "")
                       + ("" if done else f", no result after {timeout_seconds} s: kill requested"))
            return {"step": "test job", "ok": ok, "ms": round((time.monotonic() - t0) * 1000),
                    "detail": summary, "command": cmd, "logs": out[-8000:]}
        except Exception as e:
            return {"step": "test job", "ok": False, "ms": round((time.monotonic() - t0) * 1000),
                    "detail": _err(e), "command": cmd}
        finally:
            if h is not None:
                b.cleanup(h)


def _err(e: Exception) -> str:
    resp = getattr(e, "response", None)
    if isinstance(resp, dict) and resp.get("Error"):
        er = resp["Error"]
        return f"{er.get('Code')}: {er.get('Message')}"[:600]
    name = type(e).__name__
    return (str(e) if name in ("RuntimeError", "BackendError", "NoCapacity") else f"{name}: {e}")[:600]
