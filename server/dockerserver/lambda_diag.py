"""Debugging AWS Lambda nodes (admin Settings and the admin MCP; admins only).

* diagnose(): boto3 -> credentials (STS GetCallerIdentity) -> the function -> reserved
  concurrency -> async retries -> S3 put/get/delete -> pre-signed PUT/GET over HTTPS (what
  the function does). Each step has its own try/except and short AWS timeouts (connect 5 s,
  read 10 s, 1 retry); a step whose prerequisite failed is skipped. Common botocore errors
  become plain hints. Messages never contain secrets or pre-signed URLs.
* AdminTestJobs: a fixed diagnostic command (never admin-supplied) run through the node's
  backend (LambdaBackend.start/status/logs/cleanup) in a thread. Not a user Job, not billed;
  it holds one concurrency slot while it runs. The last 20 are kept in memory.
* function_logs(): CloudWatch Logs FilterLogEvents on /aws/lambda/<function>.
boto3 is imported only when one of these runs (it is large; the server has little memory).
"""
import copy
import importlib.metadata
import importlib.util
import os
import re
import secrets
import shlex
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .backends import BackendError, JobSpec
from .lambda_backend import MAX_VCPUS, MB_PER_VCPU, TIMEOUT_MARGIN, function_name
from .services import ServiceError

TEST_TIMEOUT = 120                  # test job limit (s), or the node's longest job if shorter
TEST_LOG_TAIL = 64 * 1024
TEST_KEEP = 20
INTERNET_PROBE = ("1.1.1.1", 443)   # the test job's internet probe (TCP connect)
NET_PROBE_TIMEOUT = 3.0
LOG_MAX_EVENTS = 300
LOG_MAX_MINUTES = 1440
LOG_MAX_PAGES = 20

BOTO3_HINT = ("install it on the server: cd server && .venv/bin/pip install -r requirements.txt, "
              "then restart dockerserver")
DEPLOY_HINT = "run lambda-node/deploy.sh again"
ROOT_HINT = ("these are the AWS account's root credentials: create an IAM user with only the "
             "fast-compute policy (deploy.sh makes one) and use its access key instead")
REDEPLOY_LOGS_HINT = ("the server's IAM user may not read the function's logs (logs:FilterLogEvents "
                      "on its log group); deploy.sh grants it now: " + DEPLOY_HINT)
NO_LOGS_HINT = ("the function has written no logs yet: either it has not run since it was deployed, or "
                "its execution role may not write logs (a NETWORK=internet function has no permissions "
                "unless it was deployed with LOGS=1, on purpose; NETWORK=none functions always may). "
                "To get logs: LOGS=1 sh lambda-node/deploy.sh (with the same other settings), then run "
                "a test job")

TITLES = {"boto3": "boto3 library", "credentials": "AWS credentials", "function": "Lambda function",
          "concurrency": "Reserved concurrency", "retries": "Async retries", "s3": "S3 bucket access",
          "presigned": "Pre-signed URLs over HTTPS"}      # the labels of /admin-ui (admin-i18n.js)

DENIED = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "AuthorizationError",
          "AllAccessDisabled"}
BAD_KEY = {"InvalidClientTokenId", "UnrecognizedClientException", "InvalidAccessKeyId"}
BAD_SIGNATURE = {"SignatureDoesNotMatch", "IncompleteSignature", "InvalidSignatureException"}
EXPIRED = {"ExpiredToken", "ExpiredTokenException", "RequestExpired", "RequestTimeTooSkewed",
           "InvalidToken"}
NOT_FOUND = {"ResourceNotFoundException"}
WRONG_REGION = {"PermanentRedirect", "AuthorizationHeaderMalformed", "IllegalLocationConstraintException",
                "301", "Redirect"}
UNREACHABLE = {"EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError",
               "ConnectionClosedError", "ProxyConnectionError", "ConnectError", "ConnectTimeout",
               "ReadTimeout", "WriteTimeout", "PoolTimeout", "RemoteProtocolError"}


class SecretUnreadable(RuntimeError):
    """A secret saved in the admin settings cannot be decrypted (DOCKERSERVER_SECRET_KEY changed?)."""


# ---------------------------------------------------------------- small helpers
def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mask_key_id(v: str | None) -> str | None:
    """AKIAABCDEFGHIJKLMNOP -> AKIA…MNOP."""
    if not v:
        return None
    return f"{v[:4]}…{v[-4:]}" if len(v) >= 12 else "…"


def boto3_info() -> dict:
    """Whether boto3 is installed, without importing it."""
    try:
        if importlib.util.find_spec("boto3") is None:
            return {"installed": False, "version": None}
    except (ImportError, ValueError):
        return {"installed": False, "version": None}
    try:
        version = importlib.metadata.version("boto3")
    except importlib.metadata.PackageNotFoundError:
        version = None
    return {"installed": True, "version": version}


def env_credentials_source() -> str:
    """"env" if boto3's default chain has something configured here (AWS_* variables, e.g. from
    server/.env, a profile or credentials file), else "none". An instance role can't be seen
    without asking AWS; the credentials check finds it."""
    if any(os.environ.get(k, "").strip() for k in (
            "AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_WEB_IDENTITY_TOKEN_FILE")):
        return "env"
    try:
        f = Path(os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or Path.home() / ".aws" / "credentials")
        return "env" if f.is_file() else "none"
    except (OSError, RuntimeError):
        return "none"


_URL_QUERY = re.compile(r"(https?://[^\s?\"'<>]+)\?[^\s\"'<>]*")
_KEY_ID = re.compile(r"\b((?:AKIA|ASIA)[A-Z0-9]{12,124})\b")


def clean(text: str, limit: int = 500) -> str:
    """One line, no pre-signed URL query strings, access key IDs masked, at most `limit` chars."""
    text = _URL_QUERY.sub(r"\1?…", " ".join(str(text).split()))
    text = _KEY_ID.sub(lambda m: mask_key_id(m.group(1)), text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def error_code(e: BaseException) -> tuple[str | None, str]:
    """(AWS error code or None, message) of a boto3/botocore exception."""
    resp = getattr(e, "response", None)
    if isinstance(resp, dict) and isinstance(resp.get("Error"), dict):
        err = resp["Error"]
        return (str(err["Code"]) if err.get("Code") else None), str(err.get("Message") or "")
    return None, str(e)


def _bucket_region(e: BaseException) -> str | None:
    resp = getattr(e, "response", None) or {}
    hdrs = (resp.get("ResponseMetadata") or {}).get("HTTPHeaders") or {}
    err = resp.get("Error") or {}
    return hdrs.get("x-amz-bucket-region") or err.get("Region") or err.get("BucketRegion")


def explain(e: BaseException, step: str, cfg: dict) -> tuple[str, str | None]:
    """(detail, hint) in plain words for an exception from boto3, botocore or httpx."""
    code, msg = error_code(e)
    name = type(e).__name__
    detail = f"{code}: {msg}" if code else (f"{name}: {msg}" if msg else name)
    key = code or name
    fn, bucket, region = cfg.get("function"), cfg.get("bucket"), cfg.get("region")
    hint = None
    if isinstance(e, SecretUnreadable):
        detail, hint = str(e), "save the access key again in Settings → AWS account"
    elif name in ("NoCredentialsError", "PartialCredentialsError"):
        hint = ("no AWS credentials found: save an access key in Settings → AWS account (or set "
                "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in server/.env and restart)")
    elif name == "ProfileNotFound":
        hint = "the node's AWS profile is not defined on this server (~/.aws/config); fix it in server/.env"
    elif name == "NoRegionError":
        hint = "no AWS region: set the node's region"
    elif key in BAD_KEY:
        hint = ("AWS does not know this access key ID: it was deleted, deactivated or mistyped. Create "
                "a new key (aws iam create-access-key --user-name fast-compute-server) and save it in "
                "Settings → AWS account")
    elif key in BAD_SIGNATURE:
        hint = ("the secret access key does not belong to this access key ID (or this server's clock "
                "is wrong): paste the secret again")
    elif key in EXPIRED:
        hint = "the credentials or the request expired: check this server's clock (timedatectl)"
    elif key in DENIED:
        hint = {
            "function": (f"the server's IAM user may not use this function: its policy must allow "
                         f"lambda:GetFunctionConfiguration and lambda:InvokeFunction on {fn} "
                         f"({DEPLOY_HINT}; check the function name and region)"),
            "s3": (f"the server's IAM user may not use the job files: its policy must allow "
                   f"s3:PutObject, s3:GetObject and s3:DeleteObject on arn:aws:s3:::{bucket}/fast-compute/* "
                   f"({DEPLOY_HINT}); also check the bucket name and region"),
            "logs": REDEPLOY_LOGS_HINT,
        }.get("s3" if step == "presigned" else step)
    elif key in NOT_FOUND:
        hint = {"function": f"no function {fn} in {region}: check the function name and the region "
                            "(deploy.sh prints both)",
                "logs": NO_LOGS_HINT}.get(step)
    elif key == "NoSuchBucket":
        hint = f"no bucket {bucket}: check the bucket name (deploy.sh prints it)"
    elif key in WRONG_REGION:
        where = _bucket_region(e)
        hint = (f"the bucket is in {where}, not in {region}: set the node's region to {where}" if where
                else f"the bucket is probably not in {region}: set the node's region to the bucket's region")
    elif name in UNREACHABLE or "SSL" in msg or "CERTIFICATE" in msg.upper():
        if step == "presigned" and bucket and "." in bucket and ("SSL" in msg or "CERTIFICATE" in msg.upper()):
            hint = ("bucket names with dots break HTTPS URLs of the form <bucket>.s3.<region>.amazonaws.com: "
                    "use a bucket without dots")
        else:
            hint = ("cannot reach AWS from this server: check the region and the server's outbound "
                    "HTTPS access to *.amazonaws.com")
    return clean(detail), hint


# ---------------------------------------------------------------- AWS clients
class AwsClients:
    """boto3 clients for one node's diagnostics (or the credentials check): timeouts connect
    5 s / read 10 s, 1 retry; credentials as the node uses them; made on first use.
    factory (tests): callable(service, region) -> client, used instead of boto3."""

    def __init__(self, region: str | None, *, profile: str | None = None, credentials=None,
                 endpoints: dict | None = None, factory=None, source: str = "env"):
        self.region, self.profile = region, profile
        self.credentials = None if profile else credentials
        self.endpoints = {k: v for k, v in (endpoints or {}).items() if v}
        self.factory, self.source = factory, source
        self._session = None
        self._clients: dict = {}
        self._secrets: list[str] = []

    def session(self):
        if self._session is None:
            import boto3
            if self.profile:
                self._session = boto3.session.Session(profile_name=self.profile, region_name=self.region)
            else:
                creds = self.credentials() if self.credentials is not None else None
                if creds:
                    self._secrets.append(creds["aws_secret_access_key"])
                self._session = boto3.session.Session(region_name=self.region, **(creds or {}))
        return self._session

    def __call__(self, service: str):
        c = self._clients.get(service)
        if c is None:
            if self.factory is not None:
                c = self.factory(service, self.region)
            else:
                from botocore.config import Config
                cfg = Config(connect_timeout=5, read_timeout=10,
                             retries={"total_max_attempts": 2, "mode": "standard"})
                endpoint = self.endpoints.get(service)
                if service == "s3":   # as LambdaBackend.s3 makes them (pre-signed URL style)
                    cfg = cfg.merge(Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
                    endpoint = endpoint or (f"https://s3.{self.region}.amazonaws.com" if self.region else None)
                c = self.session().client(service, endpoint_url=endpoint, config=cfg)
            self._clients[service] = c
        return c

    def check_readable(self):
        """Raise SecretUnreadable now if the saved secret can't be decrypted."""
        if self.credentials is not None:
            creds = self.credentials()
            if creds:
                self._secrets.append(creds["aws_secret_access_key"])

    def via(self) -> str | None:
        """How boto3 found the credentials ("env", "iam-role", "shared-credentials-file", ...)."""
        if self.factory is not None:
            return None
        try:
            c = self.session().get_credentials()
            return getattr(c, "method", None) if c is not None else None
        except Exception:  # noqa: BLE001 - only informative
            return None

    def scrub(self, text: str | None) -> str | None:
        if not text:
            return text
        for s in [*self._secrets, os.environ.get("AWS_SECRET_ACCESS_KEY", "")]:
            if s and len(s) >= 8:
                text = text.replace(s, "***")
        return text


SOURCE_TEXT = {"profile": "AWS profile from server/.env", "admin": "the access key saved in Settings",
               "env": "boto3's default chain (AWS_* in server/.env, instance role, ...)",
               "none": "none configured"}


def check_credentials(clients: AwsClients) -> dict:
    """POST /admin/settings/aws/check: who am I (STS GetCallerIdentity needs no permission)."""
    out = {"ok": False, "source": clients.source, "account": None, "arn": None, "ms": None,
           "error": None, "hint": None}
    t0 = time.monotonic()
    try:
        import boto3  # noqa: F401
    except ImportError:
        out.update(error="boto3 is not installed", hint=BOTO3_HINT, ms=0.0)
        return out
    try:
        clients.check_readable()
        r = clients("sts").get_caller_identity()
        out.update(ok=True, account=r.get("Account"), arn=r.get("Arn"))
        via = clients.via()
        if via:
            out["via"] = via
        if out["source"] == "none":       # e.g. an instance role
            out["source"] = "env"
        if str(r.get("Arn") or "").endswith(":root"):
            out["hint"] = ROOT_HINT
    except Exception as e:  # noqa: BLE001
        detail, hint = explain(e, "credentials", {})
        out.update(error=clients.scrub(detail), hint=hint)
        if type(e).__name__ in ("NoCredentialsError", "PartialCredentialsError"):
            out["source"] = "none"
    out["ms"] = round((time.monotonic() - t0) * 1000, 1)
    return out


# ---------------------------------------------------------------- diagnostics
def _s3_error(r: httpx.Response) -> str:
    m = re.search(r"<Code>([^<]{1,100})</Code>", r.text or "")
    return m.group(1) if m else ""


def diagnose(cfg: dict, clients: AwsClients, node: str | None = None) -> dict:
    """Check one Lambda node's settings (saved or not) step by step.
    -> {"ok", "node", "checked_at", "steps": [{"id", "title", "status", "detail", "ms", "hint"}]}"""
    steps: list[dict] = []
    passed: dict[str, bool] = {}
    fn = cfg.get("function") or ""
    bucket = cfg.get("bucket")
    prefix = cfg.get("prefix") or "fast-compute/jobs/"
    probe = secrets.token_hex(8)

    def run(sid: str, fn_, needs: tuple = ()):
        blocked = next((n for n in needs if not passed.get(n)), None)
        if blocked:
            steps.append({"id": sid, "title": TITLES[sid], "status": "skip",
                          "detail": f"skipped: \"{TITLES[blocked]}\" did not pass", "ms": None, "hint": None})
            passed[sid] = False
            return
        t0 = time.monotonic()
        try:
            status, detail, hint = fn_()
        except Exception as e:  # noqa: BLE001 - every step reports its own failure
            status = "fail"
            detail, hint = explain(e, sid, cfg)
        steps.append({"id": sid, "title": TITLES[sid], "status": status,
                      "detail": clients.scrub(clean(detail)), "ms": round((time.monotonic() - t0) * 1000, 1),
                      "hint": clients.scrub(hint)})
        passed[sid] = status in ("ok", "warn")

    def s_boto3():
        try:
            import boto3
            import botocore
        except ImportError as e:
            return "fail", f"boto3 is not installed ({e})", BOTO3_HINT
        return "ok", f"boto3 {boto3.__version__}, botocore {botocore.__version__}", None

    def s_credentials():
        clients.check_readable()
        r = clients("sts").get_caller_identity()
        arn = str(r.get("Arn") or "?")
        via = clients.via()
        src = SOURCE_TEXT.get(clients.source, clients.source)
        if clients.source == "profile":
            src = f"AWS profile {cfg.get('profile')!r} from server/.env"
        detail = f"account {r.get('Account')}, {arn}; credentials: {src}" + (f" [{via}]" if via else "")
        if arn.endswith(":root"):
            return "warn", detail, ROOT_HINT
        return "ok", detail, None

    def s_function():
        c = clients("lambda").get_function_configuration(FunctionName=fn)
        state, upd = c.get("State") or "Active", c.get("LastUpdateStatus") or "Successful"
        mem, timeout = int(c.get("MemorySize") or 0), int(c.get("Timeout") or 0)
        vcpus = round(min(mem / MB_PER_VCPU, MAX_VCPUS), 2)
        vpc = c.get("VpcConfig") or {}
        in_vpc = bool(vpc.get("SubnetIds"))
        tmp = (c.get("EphemeralStorage") or {}).get("Size")
        detail = (f"{c.get('FunctionName') or function_name(fn)}: {state} / {upd}; {mem} MB = {vcpus} vCPU; "
                  f"timeout {timeout} s, so jobs up to {max(timeout - TIMEOUT_MARGIN, 0)} s; "
                  f"{'/'.join(c.get('Architectures') or ['x86_64'])}; package {c.get('PackageType') or 'Zip'}; "
                  + (f"/tmp {tmp} MB; " if tmp else "")
                  + (f"in VPC {vpc.get('VpcId') or '?'} ({len(vpc['SubnetIds'])} subnet(s))" if in_vpc
                     else "not in a VPC"))
        if state != "Active" or upd != "Successful":
            reason = c.get("StateReason") or c.get("LastUpdateStatusReason") or ""
            busy = state == "Pending" or upd == "InProgress"
            return "fail", detail + (f" ({reason})" if reason else ""), (
                "the function is being created or updated: wait a minute and run Diagnose again" if busy
                else f"the function cannot run: fix it ({DEPLOY_HINT}) and run Diagnose again")
        if timeout <= TIMEOUT_MARGIN:
            return "warn", detail, "the function timeout leaves no time for jobs: deploy with TIMEOUT=900"
        net = cfg.get("network") or "none"
        if net == "none" and not in_vpc:
            return "warn", detail, ("network is \"none\" but the function is not in a VPC, so jobs DO have "
                                    "internet access: deploy with NETWORK=none (isolated VPC) or set this "
                                    "node's network to \"internet\"")
        if net == "internet" and in_vpc:
            return "warn", detail, ("network is \"internet\" but the function is in a VPC, so jobs most likely "
                                    "have NO internet access (unless the VPC has a NAT gateway): deploy with "
                                    "NETWORK=internet or set this node's network to \"none\"")
        return "ok", detail, None

    def s_concurrency():
        mc = int(cfg.get("max_concurrency") or 1)
        try:
            r = clients("lambda").get_function_concurrency(FunctionName=fn)
        except Exception as e:
            if error_code(e)[0] in DENIED:
                return ("skip", "not allowed to read the reserved concurrency (lambda:GetFunctionConcurrency)",
                        f"the server's IAM policy from deploy.sh allows it from now on: {DEPLOY_HINT}")
            raise
        rc = r.get("ReservedConcurrentExecutions")
        if rc is None:
            return ("warn", f"no reserved concurrency; this node starts up to {mc} jobs at once",
                    f"other functions in the account can use up the concurrency: reserve {mc} "
                    f"(CONCURRENCY={mc} sh lambda-node/deploy.sh)")
        if rc == 0:
            return ("fail", "reserved concurrency is 0: the function is throttled and runs nothing",
                    f"set it to {mc}: CONCURRENCY={mc} sh lambda-node/deploy.sh")
        if rc < mc:
            return ("warn", f"reserved concurrency {rc} is lower than this node's max_concurrency {mc}",
                    f"jobs beyond {rc} at once are throttled and start late: set max_concurrency to {rc} "
                    f"or deploy with CONCURRENCY={mc}")
        return "ok", f"reserved concurrency {rc}; this node starts up to {mc} jobs at once", None

    def s_retries():
        fix = ("a retried job could run twice: set MaximumRetryAttempts to 0 (deploy.sh does it; or "
               f"aws lambda put-function-event-invoke-config --function-name {function_name(fn)} "
               "--maximum-retry-attempts 0)")
        try:
            r = clients("lambda").get_function_event_invoke_config(FunctionName=fn)
        except Exception as e:
            code = error_code(e)[0]
            if code in DENIED:
                return ("skip", "not allowed to read the async invoke settings "
                                "(lambda:GetFunctionEventInvokeConfig)",
                        f"the server's IAM policy from deploy.sh allows it from now on: {DEPLOY_HINT}")
            if code in NOT_FOUND:
                return "warn", "no async invoke settings: Lambda retries a failed invocation up to 2 times", fix
            raise
        n, age = r.get("MaximumRetryAttempts"), r.get("MaximumEventAgeInSeconds")
        detail = f"MaximumRetryAttempts {n}" + (f", MaximumEventAgeInSeconds {age}" if age is not None else "")
        if n == 0:
            return "ok", detail + " (a failed invocation is never retried)", None
        return "warn", detail, fix

    def s_s3():
        s3 = clients("s3")
        key = f"{prefix}_diagnostics/{probe}"
        body = f"fast-compute diagnostics {probe}".encode()
        s3.put_object(Bucket=bucket, Key=key, Body=body)
        got = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        if got != body:
            return "fail", "the probe object came back with different content", None
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except Exception as e:  # noqa: BLE001
            d, _ = explain(e, "s3", cfg)
            return ("warn", f"put and get work in s3://{bucket}/{prefix}, but delete failed ({d})",
                    "the server's IAM policy must allow s3:DeleteObject on fast-compute/* (job files are "
                    "deleted after each job); the probe object expires with the bucket's 2-day rule")
        # a job file the function has not written yet must read as missing (404), not 403
        try:
            s3.get_object(Bucket=bucket, Key=f"{key}-missing")
        except Exception as e:  # noqa: BLE001 - other errors: not this check's business
            if error_code(e)[0] in DENIED | {"403"}:
                return ("warn", f"put, get and delete work in s3://{bucket}/{prefix}, but a job file that "
                                "is not written yet reads as 403 AccessDenied instead of 404",
                        "the server's IAM policy lacks s3:ListBucket on the bucket (deploy.sh before "
                        "Settings): until the function reports, jobs log warnings, and a job whose "
                        f"function never starts ends as lost instead of with a clear error; {DEPLOY_HINT}")
        return "ok", f"put, get and delete work in s3://{bucket}/{prefix}", None

    def s_presigned():
        s3 = clients("s3")
        key = f"{prefix}_diagnostics/{probe}-url"
        body = f"fast-compute pre-signed probe {probe}".encode()
        params = {"Bucket": bucket, "Key": key}
        put_url = s3.generate_presigned_url("put_object", Params=params, ExpiresIn=300)
        get_url = s3.generate_presigned_url("get_object", Params=params, ExpiresIn=300)
        host = urlsplit(put_url).netloc
        try:
            with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0)) as http:
                for method, url in (("PUT", put_url), ("GET", get_url)):
                    r = http.request(method, url, content=body if method == "PUT" else None,
                                     headers={"Content-Type": "application/json"} if method == "PUT" else None)
                    if r.status_code >= 300:
                        code = _s3_error(r) or str(r.status_code)
                        e = _HttpS3Error(code, r)
                        _, hint = explain(e, "presigned", cfg)
                        if code == "SignatureDoesNotMatch":
                            hint = ("S3 rejected the URL's signature: the secret key does not match, or "
                                    "this server's clock is wrong")
                        return "fail", f"{method} through a pre-signed URL ({host}): HTTP {r.status_code} {code}", hint
                    if method == "GET" and r.content != body:
                        return "fail", f"GET through a pre-signed URL ({host}) returned other content", None
        finally:
            try:
                s3.delete_object(Bucket=bucket, Key=key)
            except Exception:  # noqa: BLE001 - expires with the bucket's lifecycle rule
                pass
        return "ok", f"PUT and GET through pre-signed URLs work ({host}), as the function will use them", None

    run("boto3", s_boto3)
    run("credentials", s_credentials, ("boto3",))
    run("function", s_function, ("credentials",))
    run("concurrency", s_concurrency, ("function",))
    run("retries", s_retries, ("function",))
    run("s3", s_s3, ("credentials",))
    run("presigned", s_presigned, ("s3",))
    return {"ok": not any(s["status"] == "fail" for s in steps), "node": node, "checked_at": iso(),
            "steps": steps}


class _HttpS3Error(Exception):
    """An S3 error answer to a pre-signed URL request, shaped like a botocore ClientError."""

    def __init__(self, code: str, r: httpx.Response):
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": f"HTTP {r.status_code}",
                                   "Region": r.headers.get("x-amz-bucket-region")},
                         "ResponseMetadata": {"HTTPHeaders": {k.lower(): v for k, v in r.headers.items()}}}


# ---------------------------------------------------------------- function logs
def function_logs(cfg: dict, clients: AwsClients, minutes: int = 60) -> dict:
    """The function's CloudWatch log events of the last `minutes` (1..1440), newest last, at
    most 300. -> {"log_group", "minutes", "events": [{"ts", "stream", "message"}], "error", "hint"}"""
    minutes = max(1, min(int(minutes or 60), LOG_MAX_MINUTES))
    group = f"/aws/lambda/{function_name(cfg.get('function') or '')}"
    out = {"log_group": group, "minutes": minutes, "events": [], "error": None, "hint": None}
    end = int(time.time() * 1000)
    kw = {"logGroupName": group, "startTime": end - minutes * 60_000, "endTime": end, "limit": 1000}
    kept: deque = deque(maxlen=LOG_MAX_EVENTS)
    try:
        logs = clients("logs")
        for _ in range(LOG_MAX_PAGES):
            r = logs.filter_log_events(**kw)
            for ev in r.get("events") or []:
                t = int(ev.get("timestamp") or 0)
                kept.append((t, {"ts": iso(t / 1000), "stream": ev.get("logStreamName"),
                                 "message": str(ev.get("message") or "").rstrip("\n")[:4000]}))
            if not r.get("nextToken"):
                break
            kw["nextToken"] = r["nextToken"]
        else:
            out["truncated"] = True
            out["hint"] = "more events than can be read at once: the newest may be missing; choose fewer minutes"
    except Exception as e:  # noqa: BLE001
        code = error_code(e)[0]
        detail, hint = explain(e, "logs", cfg)
        if code in DENIED:
            hint = REDEPLOY_LOGS_HINT
        elif code in NOT_FOUND:
            detail, hint = f"no log group {group}", NO_LOGS_HINT
        out["error"], out["hint"] = clients.scrub(detail), hint
    out["events"] = [ev for _, ev in sorted(kept, key=lambda x: x[0])]
    return out


# ---------------------------------------------------------------- test jobs
_PROBE_SCRIPT = '''\
import os, platform, shutil, socket, sys, time
print("fast-compute test job")
print("python:", sys.version.split()[0])
print("platform:", platform.platform(), platform.machine())
print("vcpus:", os.cpu_count())
mem = None
for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
    try:
        v = open(path).read().strip()
        if v.isdigit() and int(v) < 1 << 50:
            mem = int(v) // 1048576
            break
    except OSError:
        pass
if mem is None:
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal:"):
                mem = int(line.split()[1]) // 1024
                break
    except (OSError, ValueError):
        pass
print("memory_mb:", mem if mem is not None else "unknown")
try:
    print("tmp_free_mb:", shutil.disk_usage("/tmp").free // 1048576)
except OSError as e:
    print("tmp_free_mb: unknown", e)
for name, host, port in TARGETS:
    t0 = time.monotonic()
    try:
        socket.create_connection((host, port), timeout=TIMEOUT).close()
        print(f"net {name} {host}:{port}: ok ({(time.monotonic() - t0) * 1000:.0f} ms)")
    except OSError as e:
        print(f"net {name} {host}:{port}: failed ({type(e).__name__}: {e})")
'''


def probe_command(targets: list[tuple[str, str, int]]) -> str:
    """The test job's fixed command: python version, platform, vCPUs, memory, free /tmp, and a
    TCP connect to the internet and to S3 (so the admin sees whether network matches)."""
    head = f"TARGETS = {targets!r}\nTIMEOUT = {float(NET_PROBE_TIMEOUT)!r}\n"
    return "python3 -c " + shlex.quote(head + _PROBE_SCRIPT)


def _s3_target(b) -> tuple[str, str, int]:
    """The host the function talks to for job files (from a pre-signed URL; nothing is sent)."""
    try:
        url = b.s3.generate_presigned_url("get_object", ExpiresIn=60, Params={
            "Bucket": b.bucket, "Key": f"{b.prefix}_diagnostics/probe"})
        u = urlsplit(url)
        return "s3", u.hostname, u.port or (443 if u.scheme == "https" else 80)
    except Exception:  # noqa: BLE001
        return "s3", f"s3.{b.region or 'us-east-1'}.amazonaws.com", 443


def _summary(text: str, network: str) -> list[str]:
    vals = dict(m.groups() for m in re.finditer(r"^(python|vcpus|memory_mb|tmp_free_mb): (.+)$", text, re.M))
    lines = []
    if vals:
        lines.append("environment: " + ", ".join(f"{k} {v}" for k, v in vals.items()))
    nets = {m.group(1): m.group(2) == "ok" for m in re.finditer(r"^net (\w+) \S+: (ok|failed)", text, re.M)}
    if "internet" in nets:
        want = network == "internet"
        if nets["internet"] == want:
            lines.append(f"network matches network={network}: the internet is "
                         + ("reachable" if want else "not reachable"))
        else:
            lines.append(f"WARNING: the internet is {'reachable' if nets['internet'] else 'NOT reachable'} "
                         f"although this node's network is \"{network}\"")
    if "s3" in nets:
        lines.append("S3 is reachable from the function" if nets["s3"]
                     else "WARNING: S3 is NOT reachable from the function (job files cannot be read or written)")
    return lines


class AdminTestJobs:
    """Test jobs on Lambda nodes (see the module docstring). Thread-safe."""

    def __init__(self, keep: int = TEST_KEEP):
        self.keep = keep
        self._lock = threading.Lock()
        self._tests: "OrderedDict[str, dict]" = OrderedDict()

    def get(self, test_id: str) -> dict | None:
        with self._lock:
            st = self._tests.get(test_id)
            return copy.deepcopy(st) if st is not None else None

    def start(self, b) -> dict:
        """Refused (409) when the node is offline or all its slots are busy."""
        if b.active_count() >= b.max_concurrency:
            raise ServiceError(409, f"all {b.max_concurrency} slots of {b.name} are busy; try again "
                                    "when a job has finished")
        info = b.info(max_age=10)
        if info is None:
            raise ServiceError(409, f"lambda node {b.name} is offline ({b.last_error or 'not reachable'}); "
                                    "run Diagnose to see why")
        timeout = max(1, min(TEST_TIMEOUT, b.max_job_seconds() or TEST_TIMEOUT))
        image = b.images[0]
        tid = secrets.token_hex(6)
        spec = JobSpec(f"test-{tid}", image, probe_command([("internet", *INTERNET_PROBE), _s3_target(b)]),
                       info["capacity"]["cpus"], info["capacity"]["mem_mb"], timeout,
                       b.network == "internet", {})
        state = {"id": tid, "node": b.name, "phase": "starting", "status": None, "exit_code": None,
                 "error": None, "logs": "", "started_at": iso(), "finished_at": None,
                 "timings": {"invoke_ms": None, "first_status_s": None, "total_s": None},
                 "steps": [f"[  0.0 s] test job on {b.name}: function {b.function}, image {image}, "
                           f"network={b.network}, limit {timeout} s"]}
        with self._lock:
            self._tests[tid] = state
            while len(self._tests) > self.keep:
                self._tests.popitem(last=False)
        threading.Thread(target=self._run, args=(b, spec, state), name=f"lambda-test-{tid}",
                         daemon=True).start()
        return self.get(tid) or copy.deepcopy(state)

    def _note(self, state: dict, t0: float, step: str | None = None, lines=(), **fields):
        with self._lock:
            for k in ("invoke_ms", "first_status_s", "total_s"):
                if k in fields:
                    state["timings"][k] = fields.pop(k)
            stamp = f"[{time.monotonic() - t0:5.1f} s]"
            state["steps"].extend(f"{stamp} {s}" for s in ([step] if step else []) + list(lines))
            state.update(fields)

    def _run(self, b, spec: JobSpec, state: dict):
        t0 = time.monotonic()
        h = None
        # phase "finished"/"error" is set last, together with finished_at, so a client that
        # sees it has the whole result (including the clean-up line)
        final = {"phase": "error", "status": "error", "error": "the test job stopped unexpectedly"}
        try:
            self._note(state, t0, f"invoking {b.function} (asynchronous invoke)", phase="invoking")
            h = b.start(spec, b"")
            invoke_ms = round((time.monotonic() - t0) * 1000, 1)
            self._note(state, t0, f"invoked in {invoke_ms:.0f} ms; waiting for the invocation to report "
                                  "(cold start)", phase="waiting", invoke_ms=invoke_ms)
            deadline = time.monotonic() + spec.timeout_seconds + b.start_timeout + 30
            reported, st, warned = False, None, False
            while True:
                try:
                    st = b.status(h)
                except BackendError as e:
                    if not warned:
                        self._note(state, t0, f"status not readable yet ({clean(str(e), 200)})")
                        warned = True
                if not reported and h.extra.get("first_seen"):
                    reported = True
                    first = round(h.extra["first_seen"] - h.extra.get("invoked", h.extra["first_seen"]), 2)
                    self._note(state, t0, f"the invocation reported after {first:.1f} s (≈ cold start); "
                                          "running", phase="running", first_status_s=first)
                if st is not None and st.finished:
                    break
                if time.monotonic() > deadline:
                    b.kill(h)
                    raise BackendError(f"no result within {int(deadline - t0)} s; asked the invocation to stop")
                time.sleep(max(getattr(b, "poll_interval", 1.0), 0.05))
            text = b.logs(h)[-TEST_LOG_TAIL:].decode(errors="replace")
            status = st.node_status if st.node_status in ("succeeded", "failed", "timeout", "killed", "error") \
                else ("succeeded" if st.exit_code == 0 else "failed")
            total = round(time.monotonic() - t0, 2)
            line = f"finished: {status}" + (f" (exit code {st.exit_code})" if st.exit_code is not None else "") \
                + (f": {st.error}" if st.error else "") + f" after {total:.1f} s"
            self._note(state, t0, line, _summary(text, b.network), logs=text, total_s=total)
            final = {"phase": "finished", "status": status, "exit_code": st.exit_code, "error": st.error}
        except Exception as e:  # noqa: BLE001 - shown to the admin
            msg = clean(str(e) if isinstance(e, BackendError) else f"{type(e).__name__}: {e}")
            self._note(state, t0, f"error: {msg}", total_s=round(time.monotonic() - t0, 2))
            final = {"phase": "error", "status": "error", "error": msg}
        finally:
            if h is not None:
                try:
                    removed = b.cleanup(h)
                    self._note(state, t0, "removed the test's files from S3" if removed is not False else
                               "could not remove all of the test's files from S3 (they expire with the "
                               "bucket's 2-day rule)")
                except Exception:  # noqa: BLE001
                    pass
            self._note(state, t0, finished_at=iso(), **final)
