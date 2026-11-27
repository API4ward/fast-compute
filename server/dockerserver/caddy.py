"""Caddy configuration management for the admin UI (/admin-ui -> Caddy tab).

The Caddyfile lives in the project (default: deploy/Caddyfile, which /etc/caddy/Caddyfile only
imports). Changes go through Caddy's local admin API (default http://127.0.0.1:2019), so no sudo
is needed:

  validate  POST /adapt  - parse the Caddyfile and return the JSON it becomes (nothing applied)
  apply     POST /load   - Caddy provisions the new config and swaps it in atomically; on any
                           error the running config stays as it was. Only after a successful load
                           is the file written, so the file and the running server never diverge
                           (and a Caddy restart reads the same config).

Every apply first copies the previous file into <data>/caddy/backups/; a backup can be loaded
back into the editor and applied like any other edit. Admin-only; see api.py.
"""
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("dockerserver.caddy")

MAX_BYTES = 256 * 1024
KEEP_BACKUPS = 30
_BACKUP_RE = re.compile(r"^\d{8}T\d{6}(?:\d{6})?Z-[A-Za-z0-9_.@-]+\.Caddyfile$")


class CaddyError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _loopback_url(url: str) -> str:
    u = urlsplit(url)
    if u.scheme != "http" or (u.hostname or "") not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(f"DOCKERSERVER_CADDY_ADMIN must be http://127.0.0.1:<port> (got {url!r})")
    return url.rstrip("/")


def summarize(cfg: dict | None) -> list[dict]:
    """Site blocks of an adapted/running Caddy JSON config: hosts, listen addresses, upstreams."""
    out = []
    servers = ((cfg or {}).get("apps") or {}).get("http", {}).get("servers") or {}
    for sname, srv in servers.items():
        for route in srv.get("routes") or []:
            hosts = sorted({h for m in route.get("match") or [] for h in m.get("host") or []})
            ups, kinds = [], set()

            def walk(o):
                if isinstance(o, dict):
                    if o.get("handler"):
                        kinds.add(o["handler"])
                    for u in o.get("upstreams") or []:
                        if isinstance(u, dict) and u.get("dial") and u["dial"] not in ups:
                            ups.append(u["dial"])
                    for v in o.values():
                        walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)
            walk(route.get("handle"))
            out.append({"server": sname, "listen": srv.get("listen") or [], "hosts": hosts or ["*"],
                        "upstreams": ups, "handlers": sorted(kinds - {"subroute"})})
    return out


def _caddy_error(r: httpx.Response) -> str:
    try:
        msg = r.json().get("error") or r.text
    except ValueError:
        msg = r.text
    return (msg or f"HTTP {r.status_code}").strip()


class CaddyManager:
    def __init__(self, caddyfile: Path, admin_url: str, backup_dir: Path, enabled: bool = True,
                 transport: httpx.BaseTransport | None = None):
        self.path = Path(caddyfile)
        self.admin_url = _loopback_url(admin_url)
        self.backup_dir = Path(backup_dir)
        self.enabled = enabled
        self._lock = threading.Lock()
        # trust_env=False: never send the admin API through an HTTP proxy.
        self._http = httpx.Client(base_url=self.admin_url, timeout=httpx.Timeout(60.0, connect=3.0),
                                  trust_env=False, transport=transport)

    # ---------- helpers ----------
    def _require(self):
        if not self.enabled:
            raise CaddyError(404, "Caddy management is turned off (DOCKERSERVER_CADDY_MANAGE=false)")

    def read(self) -> str:
        try:
            return self.path.read_text()
        except FileNotFoundError:
            return ""
        except OSError as e:
            raise CaddyError(500, f"cannot read {self.path}: {e}")

    def _check_content(self, content: str):
        if len(content.encode()) > MAX_BYTES:
            raise CaddyError(413, f"Caddyfile larger than {MAX_BYTES // 1024} KB")
        if "\x00" in content:
            raise CaddyError(400, "Caddyfile contains a NUL byte")

    def _adapt(self, content: str) -> tuple[dict, list]:
        try:
            r = self._http.post("/adapt", content=content.encode(),
                                headers={"Content-Type": "text/caddyfile"})
        except httpx.HTTPError as e:
            raise CaddyError(502, f"Caddy admin API {self.admin_url} unreachable: {e}")
        if r.status_code != 200:
            raise CaddyError(422, _caddy_error(r))
        body = r.json()
        return body.get("result") or {}, body.get("warnings") or []

    def _running(self) -> dict | None:
        r = self._http.get("/config/")
        r.raise_for_status()
        return r.json()

    # ---------- operations ----------
    def status(self) -> dict:
        content = self.read() if self.enabled else ""
        out = {"enabled": self.enabled, "path": str(self.path), "admin_url": self.admin_url,
               "content": content, "sha256": sha256(content), "reachable": False,
               "in_sync": None, "running_sites": [], "error": None}
        if not self.enabled:
            out["error"] = "Caddy management is turned off (DOCKERSERVER_CADDY_MANAGE=false)"
            return out
        try:
            running = self._running()
            out["reachable"] = True
            out["running_sites"] = summarize(running)
        except (httpx.HTTPError, ValueError) as e:
            out["error"] = f"Caddy admin API {self.admin_url} unreachable: {e}"
            return out
        try:
            adapted, _ = self._adapt(content)
            out["in_sync"] = adapted == running
        except CaddyError as e:
            out["error"] = f"the file on disk does not parse: {e.message}"
        return out

    def validate(self, content: str) -> dict:
        self._require()
        self._check_content(content)
        try:
            cfg, warnings = self._adapt(content)
        except CaddyError as e:
            if e.status != 422:
                raise
            return {"ok": False, "error": e.message, "warnings": [], "sites": []}
        return {"ok": True, "error": None, "warnings": warnings, "sites": summarize(cfg)}

    def apply(self, content: str, base_sha256: str | None, admin_name: str) -> dict:
        """Load `content` into Caddy; on success write it to the Caddyfile (old one backed up)."""
        self._require()
        self._check_content(content)
        if not content.endswith("\n"):
            content += "\n"
        with self._lock:
            current = self.read()
            if base_sha256 and base_sha256 != sha256(current):
                raise CaddyError(409, "the Caddyfile changed since you opened it; reload and redo your edit")
            cfg, warnings = self._adapt(content)          # 422 with Caddy's message on a syntax error
            try:
                r = self._http.post("/load", content=json.dumps(cfg).encode(),
                                    headers={"Content-Type": "application/json"})
            except httpx.HTTPError as e:
                raise CaddyError(502, f"Caddy admin API unreachable: {e}")
            if r.status_code != 200:
                # Caddy rolled back by itself; nothing was written.
                raise CaddyError(422, "Caddy refused the config (still running the old one): " + _caddy_error(r))
            backup = self._backup(current, admin_name) if current and current != content else None
            try:
                self._write(content)
            except OSError as e:
                log.error("caddy config loaded but %s could not be written: %s", self.path, e)
                raise CaddyError(500, f"Caddy is running the new config, but writing {self.path} failed "
                                      f"({e}); a Caddy restart would bring back the old one")
        log.warning("caddy config applied by %s (backup %s)", admin_name, backup)
        return {"applied": True, "sha256": sha256(content), "backup": backup, "warnings": warnings,
                "sites": summarize(cfg)}

    def reload(self, admin_name: str) -> dict:
        """Load the file on disk as it is (e.g. after editing it by hand)."""
        self._require()
        content = self.read()
        if not content.strip():
            raise CaddyError(400, f"{self.path} is empty or missing")
        with self._lock:
            cfg, warnings = self._adapt(content)
            r = self._http.post("/load", content=json.dumps(cfg).encode(),
                                headers={"Content-Type": "application/json", "Cache-Control": "must-revalidate"})
            if r.status_code != 200:
                raise CaddyError(422, "Caddy refused the config (still running the old one): " + _caddy_error(r))
        log.warning("caddy config reloaded from %s by %s", self.path, admin_name)
        return {"applied": True, "sha256": sha256(content), "warnings": warnings, "sites": summarize(cfg)}

    def _write(self, content: str):
        mode = 0o644
        try:
            mode = self.path.stat().st_mode & 0o777
        except FileNotFoundError:
            pass
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".Caddyfile.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
            os.chmod(tmp, mode)                       # caddy reads it through the group bit
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---------- backups ----------
    def _backup(self, content: str, admin_name: str) -> str:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        who = re.sub(r"[^A-Za-z0-9_.@-]", "_", admin_name)[:40] or "admin"
        name = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-{who}.Caddyfile"
        (self.backup_dir / name).write_text(content)
        for old in self.list_backups()[KEEP_BACKUPS:]:
            try:
                (self.backup_dir / old["name"]).unlink()
            except OSError:
                pass
        return name

    def list_backups(self) -> list[dict]:
        if not self.backup_dir.is_dir():
            return []
        out = []
        for p in self.backup_dir.iterdir():
            if _BACKUP_RE.match(p.name):
                st = p.stat()
                stamp, who = p.name.removesuffix(".Caddyfile").split("-", 1)
                when = datetime.strptime(stamp[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
                out.append({"name": p.name, "saved_at": when.isoformat(), "replaced_by": who,
                            "bytes": st.st_size})
        return sorted(out, key=lambda b: b["name"], reverse=True)

    def get_backup(self, name: str) -> str:
        self._require()
        if not _BACKUP_RE.match(name):
            raise CaddyError(404, "backup not found")
        try:
            return (self.backup_dir / name).read_text()
        except FileNotFoundError:
            raise CaddyError(404, "backup not found")

    def close(self):
        self._http.close()
