"""Admin Settings (/admin-ui -> Settings, /admin/settings/*): AWS credentials, AWS Lambda nodes
managed by the admin, and the homepage advisor's LLM. Kept in <data_dir>/settings.json:

    {"version": 1,
     "aws": {"access_key_id", "secret_access_key_enc", "region", "updated_at", "updated_by"},
     "lambda_nodes": [{"name", "function", "bucket", "region", "prefix", "images",
                       "max_concurrency", "network", "start_timeout", "stale_after",
                       "fresh_sandbox", "created_at", "updated_at", "updated_by"}],
     "advisor": {"base_url", "model", "max_tokens", "api_key_enc", "updated_at", "updated_by"}}

* The file has mode 600 and is replaced atomically (tmp file + os.replace) under a lock.
  Secrets (the AWS secret access key, the advisor's API key) are Fernet-encrypted with
  DOCKERSERVER_SECRET_KEY, the key that also encrypts users' variables. They are never returned
  by the API or logged; access key IDs are shown masked (first 4 + "…" + last 4).
* AWS credentials of every Lambda node: the node's `profile` (.env nodes only) > the key saved
  here > boto3's default chain (AWS_* in server/.env, an instance role, ...).
* Lambda nodes saved here join the NodePool at startup next to DOCKERSERVER_NODES (source
  "admin"; .env nodes are read-only here), before jobs are recovered, and are added, changed
  and removed live. A node with jobs in flight keeps its backend: only settings running jobs
  don't depend on change in place; function/bucket/region/prefix need an idle node.
* The advisor's base_url / model / max_tokens / api_key saved here override the
  DOCKERSERVER_SALES_* values field by field; removing them falls back to .env.
* Once, at startup: the access key and nodes that the interim "AWS Lambda" tab of /admin-ui
  saved in <data_dir>/lambda.json are carried over (import_legacy(); recorded under
  "imported" so it never runs twice; lambda.json is left in place).
Diagnostics, test jobs and function logs: lambda_diag.py.
"""
import copy
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select

from . import lambda_diag
from .config import LAMBDA_ADMIN_KEYS, NodeConfig
from .lambda_diag import SecretUnreadable, boto3_info, env_credentials_source, iso, mask_key_id
from .models import ACTIVE_STATUSES, Job
from .sales import SalesAgent
from .scheduler import make_backend
from .services import ServiceError

log = logging.getLogger("dockerserver.settings")

LAMBDA_DEFAULTS = {"prefix": "fast-compute/jobs/", "images": ["python:3.12-slim"], "max_concurrency": 10,
                   "network": "none", "start_timeout": 300, "stale_after": 60, "fresh_sandbox": True}
NODE_FIELDS = ("name", "function", "bucket", "region", *LAMBDA_DEFAULTS)
STRUCTURAL = {"function", "bucket", "region", "prefix"}      # changing them needs an idle node
ENV_ONLY = {"profile", "s3_endpoint_url", "lambda_endpoint_url"}
# keys of a GET row that may come back in a PUT body; ignored
ROW_KEYS = {"source", "editable", "active_jobs", "draining", "credentials", "online", "last_error",
            "error", "created_at", "updated_at", "updated_by"}
assert set(NODE_FIELDS) - {"name"} == LAMBDA_ADMIN_KEYS

_A = re.ASCII
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}", _A)
FUNCTION_RE = re.compile(r"[A-Za-z0-9_-]{1,64}", _A)
FUNCTION_ARN_RE = re.compile(r"arn:aws[a-z-]*:lambda:(?P<region>[a-z]{2}(?:-gov|-iso[a-z]*)?-[a-z]+-\d):"
                             r"\d{12}:function:[A-Za-z0-9_-]{1,64}(?::[A-Za-z0-9_$-]{1,128})?", _A)
BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", _A)
REGION_RE = re.compile(r"[a-z]{2}(?:-gov|-iso[a-z]*)?-[a-z]+-\d", _A)
PREFIX_RE = re.compile(r"[A-Za-z0-9!_.*'()/-]{0,200}", _A)
IMAGE_RE = re.compile(r"[a-z0-9][a-z0-9._/:@-]{0,199}", _A)
KEY_ID_RE = re.compile(r"[A-Z0-9]{16,128}", _A)
SECRET_RE = re.compile(r"[A-Za-z0-9/+=]{16,128}", _A)
MODEL_RE = re.compile(r"[A-Za-z0-9._:/@+-]{1,128}", _A)
API_KEY_RE = re.compile(r"[\x21-\x7e]{1,500}", _A)
LEGACY_FILE = "lambda.json"      # the interim "AWS Lambda" tab of /admin-ui (see import_legacy)
IMPORT_NOTICE_DAYS = 30          # how long GET /admin/settings reports what was carried over


def _bad(msg: str) -> ServiceError:
    return ServiceError(400, msg)


def _show(v) -> str:
    return repr(v if not isinstance(v, str) or len(v) <= 80 else v[:80] + "…")


def _text(d: dict, key: str, required: bool = True) -> str | None:
    v = d.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise _bad(f"{key} is required")
        return None
    if not isinstance(v, str):
        raise _bad(f"{key} must be a string")
    return v.strip()


def _whole(d: dict, key: str, lo: int, hi: int) -> int:
    v = d.get(key)
    if isinstance(v, str) and re.fullmatch(r"\s*\d{1,9}\s*", v, _A):
        v = int(v)
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise _bad(f"{key} must be a whole number from {lo} to {hi}")
    return v


def _flag(d: dict, key: str) -> bool:
    v = d.get(key)
    if isinstance(v, str) and v.strip().lower() in ("true", "false", "1", "0", "yes", "no", "on", "off"):
        v = v.strip().lower() in ("true", "1", "yes", "on")
    if not isinstance(v, bool):
        raise _bad(f"{key} must be true or false")
    return v


def validate_node(data, *, taken=(), require_name: bool = True) -> dict:
    """An admin's Lambda node settings -> the normalised node (NODE_FIELDS, defaults filled in).
    Raises ServiceError(400) with a clear message. `taken`: names of the other compute nodes."""
    if not isinstance(data, dict):
        raise _bad("the node settings must be a JSON object")
    unknown = set(data) - set(NODE_FIELDS) - ROW_KEYS
    if unknown & ENV_ONLY:
        raise _bad(f"{', '.join(sorted(unknown & ENV_ONLY))} can only be set in server/.env (DOCKERSERVER_NODES)")
    if unknown:
        raise _bad(f"unknown field(s): {', '.join(sorted(unknown))}")
    d = {**LAMBDA_DEFAULTS, **{k: v for k, v in data.items() if k in NODE_FIELDS and v is not None}}

    name = _text(d, "name", required=require_name)
    if name is not None:
        if not NAME_RE.fullmatch(name):
            raise _bad("name: 1-40 letters, digits, '_', '.' or '-', starting with a letter or digit")
        if name.lower() == "local":
            raise _bad('the name "local" is reserved (this host\'s Docker)')
        if name.lower() in {t.lower() for t in taken}:
            raise _bad(f"a compute node named {name} already exists (names are unique across server/.env and Settings)")

    region = _text(d, "region")
    if not REGION_RE.fullmatch(region):
        raise _bad(f"region {_show(region)} is not an AWS region name (e.g. eu-west-2)")
    function = _text(d, "function")
    arn = FUNCTION_ARN_RE.fullmatch(function)
    if arn and arn["region"] != region:
        raise _bad(f"the function ARN is in {arn['region']}, but the node's region is {region}")
    if not arn and not FUNCTION_RE.fullmatch(function):
        raise _bad("function: a Lambda function name (1-64 letters, digits, '-' or '_') or a function ARN")
    bucket = _text(d, "bucket")
    if not BUCKET_RE.fullmatch(bucket) or ".." in bucket:
        raise _bad("bucket: an S3 bucket name (3-63 lowercase letters, digits, '.' and '-', "
                   "starting and ending with a letter or digit, no '..')")

    prefix = d.get("prefix")
    if isinstance(prefix, str) and not prefix.strip():
        prefix = LAMBDA_DEFAULTS["prefix"]
    if not isinstance(prefix, str) or not PREFIX_RE.fullmatch(prefix.strip()):
        raise _bad("prefix: up to 200 letters, digits and !_.*'()/- characters")
    prefix = prefix.strip()
    prefix = prefix if prefix.endswith("/") else prefix + "/"
    if not prefix.startswith("fast-compute/"):
        raise _bad('prefix must start with "fast-compute/": the server\'s IAM policy and the bucket\'s '
                   "cleanup rule from deploy.sh only cover that")
    if any(p in ("", ".", "..") for p in prefix[:-1].split("/")):
        raise _bad("prefix: no empty, '.' or '..' parts")

    images = d.get("images")
    if isinstance(images, str):
        images = [x for x in re.split(r"[\s,]+", images) if x]
    if images == []:
        images = list(LAMBDA_DEFAULTS["images"])
    if not isinstance(images, list) or not all(isinstance(x, str) for x in images):
        raise _bad("images: a list of image names")
    images = list(dict.fromkeys(x.strip() for x in images if x.strip()))
    if not 1 <= len(images) <= 10:
        raise _bad("images: 1 to 10 image names")
    for x in images:
        if not IMAGE_RE.fullmatch(x):
            raise _bad(f"images: {_show(x)} is not an image name (e.g. python:3.12-slim)")

    network = d.get("network")
    if network not in ("none", "internet"):
        raise _bad('network must be "none" (function in an isolated VPC) or "internet"')
    return {"name": name, "function": function, "bucket": bucket, "region": region, "prefix": prefix,
            "images": images, "max_concurrency": _whole(d, "max_concurrency", 1, 1000), "network": network,
            "start_timeout": _whole(d, "start_timeout", 30, 900),
            "stale_after": _whole(d, "stale_after", 20, 600), "fresh_sandbox": _flag(d, "fresh_sandbox")}


def _default_region() -> str | None:
    """The region boto3 uses when a node names none (AWS_REGION, AWS_DEFAULT_REGION, ~/.aws/config)."""
    for k in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        v = os.environ.get(k, "").strip()
        if v:
            return v
    try:
        import boto3
        return boto3.session.Session().region_name
    except Exception:       # no boto3, or an unreadable AWS config file
        return None


# ====================================================================================
class SettingsStore:
    """<data_dir>/settings.json (see the module docstring). Thread-safe; secrets encrypted."""

    def __init__(self, path: Path, secret_key: str):
        self.path = Path(path)
        self._fernet = Fernet(secret_key.encode() if isinstance(secret_key, str) else secret_key)
        self._lock = threading.RLock()
        self._warned = False
        self._data = self._load()

    def _load(self) -> dict:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": 1}
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except ValueError as e:
            bad = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
            os.replace(self.path, bad)
            log.error("%s is not valid (%s): moved it to %s; admin settings start empty", self.path, e, bad)
            return {"version": 1}
        try:
            if self.path.stat().st_mode & 0o077:
                os.chmod(self.path, 0o600)
        except OSError:
            pass
        return data

    def _write(self, data: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                os.fchmod(f.fileno(), 0o600)
                json.dump(data, f, indent=1, sort_keys=True, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def update(self, change) -> dict:
        """change(data) on a copy, written to disk, then in use. ServiceError(500) if not saved."""
        with self._lock:
            data = copy.deepcopy(self._data)
            change(data)
            data["version"] = 1
            try:
                self._write(data)
            except OSError as e:
                raise ServiceError(500, f"could not save the settings: {e.strerror or e}")
            self._data = data
            return copy.deepcopy(data)

    def get(self, section: str):
        with self._lock:
            v = self._data.get(section)
            return copy.deepcopy(v) if v is not None else ([] if section == "lambda_nodes" else {})

    # ----- secrets -----
    def encrypt(self, text: str) -> str:
        return self._fernet.encrypt(text.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError, AttributeError, TypeError):
            raise SecretUnreadable("a secret saved in the admin settings cannot be decrypted "
                                   "(was DOCKERSERVER_SECRET_KEY changed?)")

    def has_aws_credentials(self) -> bool:
        a = self.get("aws")
        return bool(a.get("access_key_id") and a.get("secret_access_key_enc"))

    def aws_credentials(self) -> dict | None:
        """boto3 session kwargs of the saved access key, or None (then boto3's default chain)."""
        a = self.get("aws")
        if not (a.get("access_key_id") and a.get("secret_access_key_enc")):
            return None
        try:
            secret = self.decrypt(a["secret_access_key_enc"])
        except SecretUnreadable:
            raise SecretUnreadable("the AWS secret access key saved in Settings cannot be decrypted "
                                   "(was DOCKERSERVER_SECRET_KEY changed?); save the access key again")
        return {"aws_access_key_id": a["access_key_id"], "aws_secret_access_key": secret}

    def advisor(self) -> dict:
        """Saved advisor settings, API key decrypted (left out, with a warning, if unreadable)."""
        a = self.get("advisor")
        out = {k: a[k] for k in ("base_url", "model", "max_tokens") if a.get(k) not in (None, "")}
        if a.get("api_key_enc"):
            try:
                out["api_key"] = self.decrypt(a["api_key_enc"])
            except SecretUnreadable:
                if not self._warned:
                    log.warning("the advisor API key saved in Settings cannot be decrypted; using .env")
                    self._warned = True
        return out


class AwsCredentials:
    """The credentials provider a LambdaBackend calls when it builds its boto3 session."""

    def __init__(self, store: SettingsStore):
        self.store = store

    def __call__(self) -> dict | None:
        return self.store.aws_credentials()

    def available(self) -> bool:
        return self.store.has_aws_credentials()


# ====================================================================================
class AdminSettings:
    """Behind /admin/settings/* (api.py) and the admin MCP tools; callers are admins."""

    def __init__(self, settings, store: SettingsStore, runner, session_factory, sales=None,
                 client_factory=None):
        self.settings, self.store, self.runner, self.Session = settings, store, runner, session_factory
        self.sales = sales if sales is not None else SalesAgent(settings, store=store)
        self.client_factory = client_factory      # tests: callable(service, region) -> client
        self.credentials = AwsCredentials(store)
        self.tests = lambda_diag.AdminTestJobs()
        self._lock = threading.Lock()             # one node change at a time

    @property
    def pool(self):
        pool = getattr(self.runner, "pool", None)     # tests may stub the runner
        if pool is None:
            raise ServiceError(503, "this server has no compute node pool")
        return pool

    def _nodes(self) -> list:
        return list(getattr(getattr(self.runner, "pool", None), "nodes", None) or [])

    def attach(self):
        """At startup (create_app, before jobs are recovered): the Lambda nodes from .env get
        the saved credentials, and the nodes saved here join the pool."""
        self.import_legacy()
        if getattr(self.runner, "pool", None) is None:
            return
        for b in self.pool.nodes:
            if getattr(b, "kind", None) == "lambda" and getattr(b, "credentials", None) is None:
                b.credentials = self.credentials
        taken = {b.name.lower() for b in self.pool.nodes} | {"local"}
        added = []
        for d in self.store.get("lambda_nodes"):
            why = self._not_loaded(d, taken)
            if why:
                log.error("lambda node %s in %s: %s", d.get("name"), self.store.path, why)
                continue
            node = validate_node(d)
            self.pool.add_node(self._backend(node))
            taken.add(node["name"].lower())
            added.append(node["name"])
        if added:
            log.warning("lambda nodes from Settings: %s", ", ".join(added))

    @staticmethod
    def _not_loaded(d: dict, taken: set) -> str | None:
        try:
            node = validate_node(d)
        except ServiceError as e:
            return f"not loaded, invalid settings: {e.message}"
        if node["name"].lower() in taken:
            return "not loaded: a node with this name is configured in server/.env"
        return None

    def _backend(self, node: dict):
        cfg = NodeConfig(node["name"], f"lambda://{node['region']}/{node['function']}", "", kind="lambda",
                         options={k: copy.deepcopy(node[k]) for k in LAMBDA_ADMIN_KEYS}, source="admin")
        return make_backend(cfg, self.settings, credentials=self.credentials,
                            client_factory=self.client_factory)

    # ----- the interim "AWS Lambda" tab's lambda.json, carried over once -----
    def import_legacy(self) -> dict | None:
        """Carry over what the interim "AWS Lambda" tab of /admin-ui saved in <data_dir>/lambda.json:
        {"credentials": {"access_key_id": <enc>, "secret_access_key": <enc>, "updated_at", "updated_by"},
         "nodes": [{"name", "function", "bucket", "region"?, "network", "prefix", "images",
                    "max_concurrency", "start_timeout", "stale_after", "fresh_sandbox", ...}]}
        (both keys Fernet-encrypted with DOCKERSERVER_SECRET_KEY, like the ones saved here).
        An access key already saved here and node names already in use win; a node that does not
        pass validate_node() is skipped with the reason. What happened is stored under
        "imported" -> "lambda.json", so this runs once; lambda.json itself is left in place."""
        path = Path(self.settings.data_dir) / LEGACY_FILE
        done = self.store.get("imported")
        if (isinstance(done, dict) and done.get(LEGACY_FILE)) or not path.is_file():
            return None
        rec: dict = {"at": iso(), "aws_key": None, "nodes": [], "skipped": []}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as e:
            rec["error"] = f"{LEGACY_FILE} could not be read ({type(e).__name__}); nothing was carried over"
            raw = {}
        aws = self._legacy_key(raw.get("credentials"), rec)
        nodes = self._legacy_nodes(raw.get("nodes"), rec)

        def change(d):
            if aws:
                d.setdefault("aws", {}).update(aws)
            d.setdefault("lambda_nodes", []).extend(nodes)
            if not isinstance(d.get("imported"), dict):
                d["imported"] = {}
            d["imported"][LEGACY_FILE] = rec
        try:
            self.store.update(change)
        except ServiceError as e:
            log.error("could not carry over %s: %s (tried again at the next start)", path, e.message)
            return None
        skipped = [f"{x['name']} ({x['reason']})" for x in rec["skipped"]]
        if rec.get("aws_key_skipped"):
            skipped.insert(0, f"the AWS access key ({rec['aws_key_skipped']})")
        log.warning("carried over from %s: %s; not carried over: %s", path,
                    ", ".join(([f"AWS access key {rec['aws_key']}"] if rec["aws_key"] else [])
                              + [f"lambda node {n}" for n in rec["nodes"]]) or "nothing",
                    "; ".join(skipped + ([rec["error"]] if rec.get("error") else [])) or "nothing")
        return rec

    def _legacy_key(self, c, rec: dict) -> dict | None:
        """lambda.json's access key -> the "aws" fields to save, or None (rec says why)."""
        if not c:
            return None
        why = None
        try:
            kid = self.store.decrypt(c["access_key_id"]).strip()
            secret = self.store.decrypt(c["secret_access_key"]).strip()
        except (KeyError, TypeError, SecretUnreadable):
            why = "it cannot be decrypted (was DOCKERSERVER_SECRET_KEY changed?): save it again"
        else:
            if not KEY_ID_RE.fullmatch(kid) or not SECRET_RE.fullmatch(secret):
                why = "it is not a valid AWS access key: save it again"
            elif kid.startswith("ASIA"):
                why = "it is a temporary access key (ASIA…), which expires: save a long-term key"
            elif self.store.has_aws_credentials():
                why = "an access key was already saved here"
        if why:
            rec["aws_key_skipped"] = why
            return None
        rec["aws_key"] = mask_key_id(kid)
        by = c.get("updated_by")
        when = c.get("updated_at")
        return {"access_key_id": kid, "secret_access_key_enc": self.store.encrypt(secret),
                "updated_at": when if isinstance(when, str) and len(when) <= 40 else rec["at"],
                "updated_by": by if isinstance(by, str) and 0 < len(by) <= 64 else "import"}

    def _legacy_nodes(self, nodes, rec: dict) -> list[dict]:
        """lambda.json's nodes -> the ones to save (validated); the others go to rec["skipped"]."""
        if not isinstance(nodes, list):
            return []
        taken, out = self._taken(), []
        for i, n in enumerate(nodes):
            name = n.get("name") if isinstance(n, dict) else None
            label = name[:60] if isinstance(name, str) and name.strip() else f"#{i + 1}"
            try:
                if not isinstance(n, dict):
                    raise _bad("not a node object")
                if n.get("s3_endpoint_url") or n.get("lambda_endpoint_url"):
                    raise _bad("it uses a custom AWS endpoint URL, which only server/.env "
                               "(DOCKERSERVER_NODES) can set")
                d = {k: n[k] for k in NODE_FIELDS if n.get(k) is not None}
                if not d.get("region"):                 # it used boto3's default region
                    arn = FUNCTION_ARN_RE.fullmatch(d["function"]) if isinstance(d.get("function"), str) else None
                    d["region"] = arn["region"] if arn else _default_region()
                    if not d["region"]:
                        raise _bad("it has no region: add it again with its region")
                # the interim tab allowed a little more (start 10-900 s, stale 10-900 s, decimals)
                for k, lo, hi in (("start_timeout", 30, 900), ("stale_after", 20, 600)):
                    v = d.get(k)
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        d[k] = min(max(int(round(v)), lo), hi)
                node = validate_node(d, taken=taken)
            except ServiceError as e:
                rec["skipped"].append({"name": label, "reason": e.message})
                continue
            taken.add(node["name"])
            out.append({**node, "created_at": rec["at"], "updated_at": rec["at"], "updated_by": "import"})
            rec["nodes"].append(node["name"])
        return out

    def import_notice(self) -> dict | None:
        """What import_legacy() carried over, for IMPORT_NOTICE_DAYS after it ran."""
        done = self.store.get("imported")
        rec = done.get(LEGACY_FILE) if isinstance(done, dict) else None
        try:
            at = datetime.strptime(rec["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, KeyError, ValueError):
            return None
        if time.time() - at.timestamp() > IMPORT_NOTICE_DAYS * 86400:
            return None
        return {k: copy.deepcopy(rec.get(k)) for k in ("at", "aws_key", "aws_key_skipped", "nodes", "skipped", "error")}

    # ----- reading -----
    def _stored(self, name: str) -> dict | None:
        return next((d for d in self.store.get("lambda_nodes") if d.get("name") == name), None)

    def _taken(self, exclude: str | None = None) -> set[str]:
        names = ({b.name for b in self._nodes()} | {n.name for n in self.settings.nodes}
                 | {d.get("name") or "" for d in self.store.get("lambda_nodes")})
        return {n for n in names if n and n.lower() != (exclude or "").lower()}

    def _db_active(self) -> dict[str, int]:
        with self.Session() as db:
            rows = db.execute(select(Job.node, func.count()).where(
                Job.status.in_(ACTIVE_STATUSES), Job.node.is_not(None)).group_by(Job.node)).all()
        return {n: c for n, c in rows}

    @staticmethod
    def _active(name: str, b, db_counts: dict) -> int:
        """Jobs in flight on a node: this server's invocations (incl. test jobs) or active DB jobs."""
        inflight = b.active_count() if b is not None and hasattr(b, "active_count") else 0
        return max(db_counts.get(name, 0), inflight)

    def _cred_source(self, b=None) -> str:
        if b is not None and getattr(b, "_profile", None):
            return "profile"
        return "admin" if self.credentials.available() else env_credentials_source()

    def lambda_rows(self) -> list[dict]:
        counts = self._db_active()
        stored = {d.get("name"): d for d in self.store.get("lambda_nodes")}
        rows, seen = [], set()
        draining = getattr(getattr(self.runner, "pool", None), "draining", set())
        for b in self._nodes():
            if getattr(b, "kind", None) != "lambda":
                continue
            seen.add(b.name)
            src = getattr(b, "source", "env")
            if src == "admin" and b.name in stored:
                row = {k: copy.deepcopy(stored[b.name].get(k)) for k in NODE_FIELDS}
                row.update(updated_at=stored[b.name].get("updated_at"), updated_by=stored[b.name].get("updated_by"))
            else:
                c = b.config()
                row = {k: c.get(k) for k in NODE_FIELDS}
            h = b.health()
            row.update(source=src, editable=src == "admin", active_jobs=self._active(b.name, b, counts),
                       draining=b.name in draining, credentials=self._cred_source(b),
                       online=h.get("online"),
                       last_error=lambda_diag.clean(h["last_error"]) if h.get("last_error") else None)
            rows.append(row)
        in_pool = {b.name.lower() for b in self._nodes()}
        for name, d in stored.items():
            if name in seen:
                continue
            row = {k: copy.deepcopy(d.get(k)) for k in NODE_FIELDS}
            row.update(name=name, source="admin", editable=True, active_jobs=counts.get(name, 0),
                       draining=False, credentials=self._cred_source(), online=False, last_error=None,
                       updated_at=d.get("updated_at"), updated_by=d.get("updated_by"),
                       error=self._not_loaded(d, in_pool) or "not loaded")
            rows.append(row)
        return rows

    def node_row(self, name: str) -> dict:
        row = next((r for r in self.lambda_rows() if r["name"] == name), None)
        if row is None:
            raise ServiceError(404, f"no Lambda node named {name}")
        return row

    def overview(self) -> dict:
        """GET /admin/settings (no secrets)."""
        aws = self.aws_block()
        return {"aws": aws, "lambda_nodes": self.lambda_rows(),
                "lambda_defaults": {**copy.deepcopy(LAMBDA_DEFAULTS), "region": aws["region"]},
                "advisor": self.advisor_block(), "imported": self.import_notice()}

    # ----- AWS credentials -----
    def aws_block(self) -> dict:
        a = self.store.get("aws")
        saved = bool(a.get("access_key_id") and a.get("secret_access_key_enc"))
        out = {"source": "admin" if saved else env_credentials_source(),
               "access_key_id": mask_key_id(a.get("access_key_id")) if saved else None,
               "has_secret": bool(a.get("secret_access_key_enc")), "region": a.get("region"),
               "updated_at": a.get("updated_at"), "updated_by": a.get("updated_by"),
               "env_access_key_id": mask_key_id(os.environ.get("AWS_ACCESS_KEY_ID", "").strip()),
               "boto3": boto3_info()}
        if saved:
            try:
                self.store.decrypt(a["secret_access_key_enc"])
            except SecretUnreadable:
                out["error"] = ("the saved secret access key cannot be decrypted (was DOCKERSERVER_SECRET_KEY "
                                "changed?): save the access key again")
        return out

    def _reset_clients(self):
        for b in self._nodes():
            if hasattr(b, "reset_clients"):
                b.reset_clients()

    def set_aws(self, admin: str, data) -> dict:
        """{"access_key_id", "secret_access_key"} together, and/or {"region"} (null = none)."""
        if not isinstance(data, dict):
            raise _bad("the body must be a JSON object")
        unknown = set(data) - {"access_key_id", "secret_access_key", "region"}
        if unknown:
            raise _bad(f"unknown field(s): {', '.join(sorted(unknown))}")
        kid, secret = data.get("access_key_id"), data.get("secret_access_key")
        new_key = any(v not in (None, "") for v in (kid, secret))
        if new_key:
            if not (isinstance(kid, str) and kid.strip() and isinstance(secret, str) and secret.strip()):
                raise _bad("access_key_id and secret_access_key must be given together")
            kid, secret = kid.strip(), secret.strip()
            if not KEY_ID_RE.fullmatch(kid):
                raise _bad("access_key_id: 16-128 capital letters and digits (e.g. AKIA…)")
            if kid.startswith("ASIA"):
                raise _bad("this is a temporary access key (ASIA…): it needs a session token and expires; "
                           "create a long-term key for the IAM user (aws iam create-access-key)")
            if not SECRET_RE.fullmatch(secret):
                raise _bad("secret_access_key: 16-128 characters of A-Z a-z 0-9 / + = (40 for AWS keys)")
        has_region = "region" in data
        region = data.get("region")
        if has_region:
            if region is None or (isinstance(region, str) and not region.strip()):
                region = None
            elif not isinstance(region, str) or not REGION_RE.fullmatch(region.strip()):
                raise _bad(f"region {_show(region)} is not an AWS region name (e.g. eu-west-2)")
            else:
                region = region.strip()
        if not new_key and not has_region:
            raise _bad("nothing to change: give access_key_id and secret_access_key, and/or region")
        enc = self.store.encrypt(secret) if new_key else None

        def change(d):
            a = d.setdefault("aws", {})
            if new_key:
                a["access_key_id"], a["secret_access_key_enc"] = kid, enc
            if has_region:
                a["region"] = region
            a["updated_at"], a["updated_by"] = iso(), admin
        self.store.update(change)
        what = []
        if new_key:
            self._reset_clients()
            what.append(f"AWS access key (now {mask_key_id(kid)})")
        if has_region:
            what.append(f"default AWS region ({region or 'none'})")
        log.info("admin %s changed %s", admin, " and ".join(what))
        return self.aws_block()

    def delete_aws(self, admin: str) -> dict:
        def change(d):
            a = d.setdefault("aws", {})
            a.pop("access_key_id", None)
            a.pop("secret_access_key_enc", None)
            a["updated_at"], a["updated_by"] = iso(), admin
        self.store.update(change)
        self._reset_clients()
        log.info("admin %s changed AWS credentials (removed the saved access key)", admin)
        return self.aws_block()

    def _clients(self, cfg: dict) -> lambda_diag.AwsClients:
        profile = cfg.get("profile")
        return lambda_diag.AwsClients(
            cfg.get("region"), profile=profile, credentials=self.credentials,
            endpoints={"s3": cfg.get("s3_endpoint_url"), "lambda": cfg.get("lambda_endpoint_url")},
            factory=self.client_factory, source="profile" if profile else self._cred_source())

    def check_aws(self) -> dict:
        region = (self.store.get("aws").get("region") or os.environ.get("AWS_REGION")
                  or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
        return lambda_diag.check_credentials(self._clients({"region": region}))

    # ----- Lambda nodes -----
    def _lambda(self, name: str):
        b = next((x for x in self._nodes() if x.name == name), None)
        if b is None or getattr(b, "kind", None) != "lambda":
            raise ServiceError(404, f"no Lambda node named {name}")
        return b

    def _read_only(self, name: str):
        if any(x.name == name for x in self._nodes()) or any(n.name == name for n in self.settings.nodes):
            raise ServiceError(409, f"{name} is configured in server/.env (DOCKERSERVER_NODES) and is "
                                    "read-only here; change it there and restart")
        raise ServiceError(404, f"no Lambda node named {name} in Settings")

    def add_node(self, admin: str, data) -> dict:
        pool = self.pool
        with self._lock:
            node = validate_node(data, taken=self._taken())
            b = self._backend(node)
            now = iso()
            rec = {**node, "created_at": now, "updated_at": now, "updated_by": admin}
            self.store.update(lambda d: d.setdefault("lambda_nodes", []).append(rec))
            try:
                pool.add_node(b)
            except ValueError as e:     # cannot happen while names are checked above; undo
                self.store.update(lambda d: d.update(lambda_nodes=[
                    x for x in d.get("lambda_nodes", []) if x.get("name") != node["name"]]))
                raise ServiceError(409, str(e))
        log.info("admin %s changed lambda node %s (added: %s in %s, bucket %s)", admin, node["name"],
                 node["function"], node["region"], node["bucket"])
        return self.node_row(node["name"])

    def update_node(self, admin: str, name: str, data) -> dict:
        if not isinstance(data, dict):
            raise _bad("the node settings must be a JSON object")
        pool = self.pool
        with self._lock:
            cur = self._stored(name)
            if cur is None:
                self._read_only(name)
            if data.get("name") not in (None, "", name):
                raise _bad("a node's name cannot be changed: delete the node and add it again")
            merged = {**{k: cur.get(k) for k in NODE_FIELDS}, **{k: v for k, v in data.items() if k != "name"}}
            node = validate_node({**merged, "name": name}, taken=self._taken(exclude=name))
            changed = sorted(k for k in NODE_FIELDS if node[k] != cur.get(k))
            if not changed:
                return self.node_row(name)
            b = pool.by_name(name)
            live = b if b is not None and getattr(b, "source", None) == "admin" else None
            if b is not None and live is None:
                raise ServiceError(409, f"a node named {name} is configured in server/.env, so the one saved "
                                        "here is not loaded: delete it here and add it under another name")
            active = self._active(name, live, self._db_active())

            def save(d):
                for x in d.get("lambda_nodes", []):
                    if x.get("name") == name:
                        x.update(node, updated_at=iso(), updated_by=admin)
            if live is None:                         # not loaded (invalid before): store, load
                self.store.update(save)
                pool.add_node(self._backend(node))
                how = "saved and loaded"
            elif not active and live.retire_if_idle():
                try:
                    self.store.update(save)
                except ServiceError:
                    live.unretire()
                    raise
                pool.replace_node(self._backend(node))
                how = "replaced the backend"
            elif set(changed) & STRUCTURAL:
                n = max(active, 1)
                raise ServiceError(409, f"{n} job(s) running on {name}: drain it and wait, then change "
                                        f"{', '.join(sorted(set(changed) & STRUCTURAL))}")
            else:
                self.store.update(save)
                live.reconfigure(**{k: node[k] for k in changed})
                how = "applied in place (jobs are running)"
        log.info("admin %s changed lambda node %s (%s; %s)", admin, name, ", ".join(changed), how)
        return self.node_row(name)

    def delete_node(self, admin: str, name: str) -> None:
        pool = self.pool
        with self._lock:
            if self._stored(name) is None:
                self._read_only(name)
            b = pool.by_name(name)
            live = b if b is not None and getattr(b, "source", None) == "admin" else None
            # a saved node hidden by a server/.env node of the same name is not in use
            active = 0 if b is not None and live is None else self._active(name, live, self._db_active())
            if active or (live is not None and not live.retire_if_idle()):
                raise ServiceError(409, f"{max(active, 1)} job(s) running on {name}: drain it and wait, "
                                        "then delete it")
            try:
                self.store.update(lambda d: d.update(lambda_nodes=[
                    x for x in d.get("lambda_nodes", []) if x.get("name") != name]))
            except ServiceError:
                if live is not None:
                    live.unretire()
                raise
            if live is not None:
                pool.remove_node(name)
        log.info("admin %s changed lambda node %s (deleted)", admin, name)

    def validate_config(self, data) -> dict:
        """Diagnostics of an unsaved node (the name is optional and not checked for clashes)."""
        node = validate_node(data, require_name=False)
        return lambda_diag.diagnose(node, self._clients(node), node=node["name"])

    def diagnose(self, name: str) -> dict:
        cfg = self._lambda(name).config()
        return lambda_diag.diagnose(cfg, self._clients(cfg), node=name)

    def start_test_job(self, admin: str, name: str) -> dict:
        state = self.tests.start(self._lambda(name))
        log.info("admin %s started test job %s on lambda node %s", admin, state["id"], name)
        return state

    def test_job(self, test_id: str) -> dict:
        st = self.tests.get(test_id)
        if st is None:
            raise ServiceError(404, "no such test job (the last 20 are kept, until a restart)")
        return st

    def function_logs(self, name: str, minutes: int = 60) -> dict:
        cfg = self._lambda(name).config()
        return lambda_diag.function_logs(cfg, self._clients(cfg), minutes)

    # ----- homepage advisor -----
    def advisor_block(self) -> dict:
        saved, s = self.store.get("advisor"), self.settings
        cfg = self.sales.config()
        fields = [k for k in ("base_url", "model", "max_tokens", "api_key_enc") if saved.get(k) not in (None, "")]
        out = {"source": "admin" if fields else ("env" if s.sales_api_key or s.sales_model else "none"),
               "base_url": cfg["base_url"], "model": cfg["model"], "max_tokens": cfg["max_tokens"],
               "has_api_key": bool(cfg["api_key"]), "mode": "llm" if cfg["api_key"] and cfg["model"] else "faq",
               "updated_at": saved.get("updated_at"), "updated_by": saved.get("updated_by"),
               "saved": [k.replace("_enc", "") for k in fields],
               "env": {"base_url": s.sales_base_url, "model": s.sales_model, "max_tokens": s.sales_max_tokens,
                       "has_api_key": bool(s.sales_api_key)}}
        if saved.get("api_key_enc"):
            try:
                self.store.decrypt(saved["api_key_enc"])
            except SecretUnreadable:
                out["error"] = ("the saved API key cannot be decrypted (was DOCKERSERVER_SECRET_KEY changed?): "
                                "save it again")
        return out

    def set_advisor(self, admin: str, data) -> dict:
        """base_url / model / max_tokens: a value, or null/"" for the .env value; api_key:
        omitted or null keeps the saved key, "" removes it."""
        if not isinstance(data, dict):
            raise _bad("the body must be a JSON object")
        fields = {"base_url", "model", "max_tokens", "api_key"}
        unknown = set(data) - fields - {"source", "has_api_key", "mode", "updated_at", "updated_by",
                                        "saved", "env", "error"}
        if unknown:
            raise _bad(f"unknown field(s): {', '.join(sorted(unknown))}")
        if not set(data) & fields:
            raise _bad("nothing to change: give base_url, model, max_tokens and/or api_key")
        changes: dict = {}
        if "base_url" in data:
            v = _text(data, "base_url", required=False)
            if v is not None:
                try:
                    u = urlsplit(v)
                    u.port                           # noqa: B018 - raises ValueError if invalid
                except ValueError:
                    raise _bad("base_url: an http(s) URL of an OpenAI-compatible API, e.g. https://api.openai.com/v1")
                if len(v) > 300 or u.scheme not in ("http", "https") or not u.hostname or re.search(r"\s", v):
                    raise _bad("base_url: an http(s) URL of an OpenAI-compatible API, e.g. https://api.openai.com/v1")
                if "@" in u.netloc or u.query or u.fragment:
                    raise _bad("base_url: no user name, password, query or fragment (the key goes in api_key)")
                v = v.rstrip("/")
            changes["base_url"] = v
        if "model" in data:
            v = _text(data, "model", required=False)
            if v is not None and not MODEL_RE.fullmatch(v):
                raise _bad("model: up to 128 letters, digits and . _ : / @ + - (e.g. gpt-5-mini)")
            changes["model"] = v
        if "max_tokens" in data:
            v = data["max_tokens"]
            changes["max_tokens"] = None if v in (None, "") else _whole(data, "max_tokens", 16, 32000)
        key = data.get("api_key")
        new_key = None
        if key is not None:
            if not isinstance(key, str):
                raise _bad("api_key must be a string")
            key = key.strip()
            if key and not API_KEY_RE.fullmatch(key):
                raise _bad("api_key: up to 500 printable characters without spaces")
            new_key = self.store.encrypt(key) if key else ""

        def change(d):
            a = d.setdefault("advisor", {})
            for k, v in changes.items():
                if v is None:
                    a.pop(k, None)
                else:
                    a[k] = v
            if new_key == "":
                a.pop("api_key_enc", None)
            elif new_key:
                a["api_key_enc"] = new_key
            a["updated_at"], a["updated_by"] = iso(), admin
        before = self.store.get("advisor")
        self.store.update(change)
        # only what really changed; a field set to null/"" falls back to server/.env
        what = [k if v is not None else f"{k} from .env" for k, v in changes.items() if before.get(k) != v]
        if new_key == "" and before.get("api_key_enc"):
            what.append("API key removed")
        elif new_key:
            what.append("API key replaced")
        log.info("admin %s changed homepage advisor (%s)", admin, ", ".join(what) or "no change")
        return self.advisor_block()

    def delete_advisor(self, admin: str) -> dict:
        self.store.update(lambda d: d.pop("advisor", None))
        log.info("admin %s changed homepage advisor (reset to server/.env)", admin)
        return self.advisor_block()

    async def test_advisor(self, data) -> dict:
        q = data.get("question") if isinstance(data, dict) else None
        if not isinstance(q, str) or not q.strip():
            raise _bad("question is required")
        if len(q) > 500:
            raise _bad("question: at most 500 characters")
        return await self.sales.admin_test(q.strip())
