"""Settings, read from environment variables (prefix DOCKERSERVER_)."""
import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name, default):
    return os.environ.get(f"DOCKERSERVER_{name}", default)


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")).resolve())
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
    s = Settings()
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    return s
