"""Node settings: environment variables COMPUTENODE_*, optionally loaded from a
config file (default ~/.computenode/config.env, written by `computenode init`)."""
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOME = Path(os.environ.get("COMPUTENODE_HOME", Path.home() / ".computenode"))
DEFAULT_CONFIG = DEFAULT_HOME / "config.env"


def load_env_file(path: Path) -> None:
    """KEY=VALUE lines; values already set in the environment win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _env(name, default=None):
    return os.environ.get(f"COMPUTENODE_{name}", default)


def _list(name, default):
    return [s.strip() for s in _env(name, default).split(",") if s.strip()]


def tailscale_ip() -> str | None:
    exe = shutil.which("tailscale") or next(
        (p for p in ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                     r"C:\Program Files\Tailscale\tailscale.exe") if Path(p).exists()), None)
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=10)
        ip = out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else None
        return ip
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return None


@dataclass
class Settings:
    token: str = field(default_factory=lambda: _env("TOKEN", ""))
    node_name: str = field(default_factory=lambda: _env("NODE_NAME", os.uname().nodename.split(".")[0]
                                                        if hasattr(os, "uname") else "node"))
    # "tailscale" = bind only to this machine's tailnet IPv4 (the safe default).
    listen: str = field(default_factory=lambda: _env("LISTEN", "tailscale"))
    port: int = field(default_factory=lambda: int(_env("PORT", "8900")))
    # Source IPs allowed to call the API besides holding the token (empty = any).
    allowed_clients: list[str] = field(default_factory=lambda: _list("ALLOWED_CLIENTS", ""))
    # Capacity offered to the cluster; 0 = docker's total minus the reserve below.
    max_cpus: float = field(default_factory=lambda: float(_env("MAX_CPUS", "0")))
    max_mem_mb: int = field(default_factory=lambda: int(_env("MAX_MEM_MB", "0")))
    reserve_cpus: float = field(default_factory=lambda: float(_env("RESERVE_CPUS", "1")))
    reserve_mem_mb: int = field(default_factory=lambda: int(_env("RESERVE_MEM_MB", "2048")))
    allowed_images: list[str] = field(default_factory=lambda: _list("ALLOWED_IMAGES", "python:3.12-slim"))
    # Jobs asking for network=true get a bridge network; off by default so jobs
    # cannot reach the host, the tailnet or the LAN.
    allow_network: bool = field(default_factory=lambda: _env("ALLOW_NETWORK", "false").lower() in ("1", "true", "yes"))
    pids_limit: int = field(default_factory=lambda: int(_env("PIDS_LIMIT", "256")))
    tmp_size: str = field(default_factory=lambda: _env("TMP_SIZE", "64m"))
    max_upload_bytes: int = field(default_factory=lambda: int(_env("MAX_UPLOAD_BYTES", str(60 * 1024 * 1024))))
    max_timeout_seconds: int = field(default_factory=lambda: int(_env("MAX_TIMEOUT_SECONDS", str(24 * 3600))))
    stats_interval: float = field(default_factory=lambda: float(_env("STATS_INTERVAL", "10")))
    # Finished jobs nobody collected are cleaned up after this long.
    retain_seconds: int = field(default_factory=lambda: int(_env("RETAIN_SECONDS", "3600")))
    container_user: str = "65534:65534"  # nobody:nogroup

    def image_allowed(self, image: str) -> bool:
        return "*" in self.allowed_images or image in self.allowed_images

    def bind_host(self) -> str:
        if self.listen != "tailscale":
            return self.listen
        ip = tailscale_ip()
        if not ip:
            raise SystemExit("COMPUTENODE_LISTEN=tailscale but no tailnet IP found. "
                             "Run `tailscale up` first, or set COMPUTENODE_LISTEN explicitly.")
        return ip


def load_settings(config: Path | None = None) -> Settings:
    load_env_file(config or Path(_env("CONFIG", str(DEFAULT_CONFIG))))
    s = Settings()
    if not s.token or len(s.token) < 24:
        raise SystemExit("COMPUTENODE_TOKEN missing or too short; run `computenode init` first.")
    return s
