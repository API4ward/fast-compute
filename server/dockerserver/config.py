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

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'dockerserver.db'}"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def image_allowed(self, image: str) -> bool:
        return "*" in self.allowed_images or image in self.allowed_images


def load_settings() -> Settings:
    s = Settings()
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    return s
