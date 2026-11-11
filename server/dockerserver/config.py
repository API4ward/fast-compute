"""Settings, read from environment variables (prefix DOCKERSERVER_).

load_settings() first loads KEY=VALUE lines from the project's `.env` file
(<repo>/.env, or DOCKERSERVER_ENV_FILE). That file is the source of truth: its values
override inherited environment variables. It is git-ignored and should be mode 600;
secrets such as compute-node tokens live only there, never in git.
"""
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("dockerserver.config")


def _env(name, default):
    return os.environ.get(f"DOCKERSERVER_{name}", default)


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_DIR / ".env"


def load_env_file(path: Path, override: bool = False) -> None:
    try:
        text = path.read_text()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        if override:
            os.environ[k.strip()] = v
        else:
            os.environ.setdefault(k.strip(), v)


@dataclass
class NodeConfig:
    name: str
    url: str
    token: str = field(repr=False)


def parse_nodes(raw: str | None) -> list[NodeConfig]:
    """DOCKERSERVER_NODES = JSON list of {name, url, token_env}; the token is read from the
    environment variable named by token_env. Nodes without a token are skipped (warned)."""
    if not raw or not raw.strip():
        return []
    try:
        items = json.loads(raw)
    except ValueError as e:
        raise SystemExit(f"DOCKERSERVER_NODES is not valid JSON: {e}")
    nodes = []
    for it in items:
        name, url, token_env = it.get("name"), it.get("url"), it.get("token_env")
        if not (name and url and token_env):
            raise SystemExit(f"DOCKERSERVER_NODES entry needs name, url and token_env: {it!r}")
        token = os.environ.get(token_env, "").strip()
        if not token:
            log.warning("compute node %s: env var %s is not set; node disabled", name, token_env)
            continue
        nodes.append(NodeConfig(name, url, token))
    return nodes


def ensure_secret_key(env_file: Path) -> None:
    """Generate DOCKERSERVER_SECRET_KEY into the .env file on first start (mode stays 600).
    Losing this key makes stored environment variables unreadable - back up .env."""
    if os.environ.get("DOCKERSERVER_SECRET_KEY", "").strip():
        return
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    try:
        existed = env_file.exists()
        with open(env_file, "a") as f:
            f.write("\n# Encrypts users' environment variables at rest. Keep it; back it up with .env.\n"
                    f"DOCKERSERVER_SECRET_KEY={key}\n")
        if not existed:
            env_file.chmod(0o600)
        log.warning("generated DOCKERSERVER_SECRET_KEY into %s", env_file)
    except OSError as e:
        log.warning("could not write DOCKERSERVER_SECRET_KEY to %s (%s); using a temporary key - "
                    "stored environment variables will not survive a restart", env_file, e)
    os.environ["DOCKERSERVER_SECRET_KEY"] = key


def _bool(name, default: bool | None) -> bool | None:
    v = _env(name, None)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # relative paths are resolved against the project directory (server/)
    data_dir: Path = field(default_factory=lambda: (PROJECT_DIR / _env("DATA_DIR", "data")).resolve())
    default_image: str = field(default_factory=lambda: _env("DEFAULT_IMAGE", "python:3.12-slim"))
    # Comma-separated allowlist of images users may request ("*" = any).
    allowed_images: list[str] = field(
        default_factory=lambda: [s.strip() for s in _env("ALLOWED_IMAGES", "python:3.12-slim").split(",") if s.strip()]
    )
    max_upload_bytes: int = field(default_factory=lambda: int(_env("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024))))
    max_log_bytes: int = field(default_factory=lambda: int(_env("MAX_LOG_BYTES", str(1024 * 1024))))
    pids_limit: int = field(default_factory=lambda: int(_env("PIDS_LIMIT", "256")))
    tmp_size: str = field(default_factory=lambda: _env("TMP_SIZE", "64m"))
    git_timeout: int = field(default_factory=lambda: int(_env("GIT_TIMEOUT", "120")))
    container_user: str = "65534:65534"  # nobody:nogroup
    # Host header values accepted by /mcp (DNS-rebinding protection). "host:*" = any port.
    allowed_hosts: list[str] = field(default_factory=lambda: [
        h.strip() for h in _env("ALLOWED_HOSTS",
                                "127.0.0.1:8800,localhost:8800,compute.llmfreedomforge.com").split(",")
        if h.strip()])
    # Auth-failure rate limit: more than AUTH_FAIL_LIMIT bad tokens from one client IP
    # within AUTH_FAIL_WINDOW seconds -> HTTP 429 until the window passes.
    auth_fail_limit: int = field(default_factory=lambda: int(_env("AUTH_FAIL_LIMIT", "20")))
    auth_fail_window: int = field(default_factory=lambda: int(_env("AUTH_FAIL_WINDOW", "300")))
    # Credits given to every new user (hours of 1 CPU + 256 MB).
    default_credit_hours: float = field(default_factory=lambda: float(_env("DEFAULT_CREDIT_HOURS", "1000")))
    # How often a running job is sampled (docker stats) and checked against the balance.
    monitor_interval: float = field(default_factory=lambda: float(_env("MONITOR_INTERVAL", "10")))
    # A job may start only if the balance covers this many seconds at its size.
    min_start_seconds: int = field(default_factory=lambda: int(_env("MIN_START_SECONDS", "60")))

    # ---- compute nodes / scheduling ----
    nodes: list[NodeConfig] = field(default_factory=lambda: parse_nodes(_env("NODES", "")))
    # Run jobs on this host's Docker. Default: only when no compute node is configured.
    local_execution: bool | None = field(default_factory=lambda: _bool("LOCAL_EXECUTION", None))
    node_info_ttl: float = field(default_factory=lambda: float(_env("NODE_INFO_TTL", "5")))
    node_poll_interval: float = field(default_factory=lambda: float(_env("NODE_POLL_INTERVAL", "2")))
    # A running job whose node can't be reached for this long is marked "lost".
    node_lost_grace: float = field(default_factory=lambda: float(_env("NODE_LOST_GRACE", "120")))
    # A job waiting for capacity is retried every QUEUE_RETRY s, for at most QUEUE_TIMEOUT s.
    queue_retry: float = field(default_factory=lambda: float(_env("QUEUE_RETRY", "5")))
    queue_timeout: float = field(default_factory=lambda: float(_env("QUEUE_TIMEOUT", "3600")))
    # Limits given to newly created users (admins can change them per user).
    default_max_cpus: float = field(default_factory=lambda: float(_env("DEFAULT_MAX_CPUS", "1")))
    default_max_mem_mb: int = field(default_factory=lambda: int(_env("DEFAULT_MAX_MEM_MB", "256")))
    default_max_run_seconds: int = field(default_factory=lambda: int(_env("DEFAULT_MAX_RUN_SECONDS", "600")))
    default_max_concurrent_jobs: int = field(default_factory=lambda: int(_env("DEFAULT_MAX_CONCURRENT_JOBS", "2")))

    # Fernet key encrypting users' environment variables at rest (generated into .env if missing).
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", ""), repr=False)

    @property
    def use_local(self) -> bool:
        return self.local_execution if self.local_execution is not None else not self.nodes

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'dockerserver.db'}"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def allowed_origins(self) -> list[str]:
        return [f"{scheme}://{h}" for h in self.allowed_hosts for scheme in ("https", "http")]

    def image_allowed(self, image: str) -> bool:
        return "*" in self.allowed_images or image in self.allowed_images


def load_settings() -> Settings:
    env_file = Path(os.environ.get("DOCKERSERVER_ENV_FILE", DEFAULT_ENV_FILE))
    load_env_file(env_file, override=True)
    ensure_secret_key(env_file)
    log.warning("config file: %s (%s)", env_file, "loaded" if env_file.exists() else "not found")
    s = Settings()
    if not s.nodes and not _env("NODES", "").strip() and any(
            k.startswith("DOCKERSERVER_NODE_TOKEN") and v.strip() for k, v in os.environ.items()):
        log.warning("a DOCKERSERVER_NODE_TOKEN_* variable is set but DOCKERSERVER_NODES is empty: "
                    "no compute node is configured (see .env.example)")
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    return s
