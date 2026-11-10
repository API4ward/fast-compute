"""Build the tar that is copied into a job's /workspace volume.

Uploads (single file, .tar/.tar.gz/.tgz, .zip) and git checkouts are re-packed
into a fresh tar that contains only regular files and directories, owned by the
in-container user, under a top-level "ws/" directory. Links, devices, absolute
paths and ".." components are dropped.
"""
import io
import os
import posixpath
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

WS = "ws"


class WorkspaceError(ValueError):
    pass


def _safe_rel(name: str) -> str | None:
    name = name.replace("\\", "/").lstrip("/")
    norm = posixpath.normpath(name)
    if norm in ("", ".") or norm.startswith("../") or norm == ".." or "/../" in f"/{norm}/":
        return None
    return norm


class _Builder:
    def __init__(self, uid: int, gid: int, max_bytes: int):
        self.buf = io.BytesIO()
        self.tar = tarfile.open(fileobj=self.buf, mode="w")
        self.uid, self.gid, self.max_bytes = uid, gid, max_bytes
        self.total = 0
        self.dirs: set[str] = set()
        self._dir(WS)

    def _info(self, name, mode):
        ti = tarfile.TarInfo(name)
        ti.uid, ti.gid, ti.mode = self.uid, self.gid, mode
        return ti

    def _dir(self, path):
        if path in self.dirs:
            return
        parent = posixpath.dirname(path)
        if parent:
            self._dir(parent)
        ti = self._info(path, 0o755)
        ti.type = tarfile.DIRTYPE
        self.tar.addfile(ti)
        self.dirs.add(path)

    def add_dir(self, rel):
        rel = _safe_rel(rel)
        if rel:
            self._dir(f"{WS}/{rel}")

    def add_file(self, rel, data: bytes, executable=False):
        rel = _safe_rel(rel)
        if not rel:
            return
        self.total += len(data)
        if self.total > self.max_bytes:
            raise WorkspaceError("workspace content exceeds size limit")
        path = f"{WS}/{rel}"
        self._dir(posixpath.dirname(path))
        ti = self._info(path, 0o755 if executable else 0o644)
        ti.size = len(data)
        self.tar.addfile(ti, io.BytesIO(data))

    def finish(self) -> bytes:
        self.tar.close()
        return self.buf.getvalue()


def build_workspace(filename: str | None, content: bytes | None, git_dir: Path | None,
                    uid: int, gid: int, max_bytes: int) -> bytes:
    b = _Builder(uid, gid, max_bytes)
    if git_dir is not None:
        for root, dirs, files in os.walk(git_dir, followlinks=False):
            rel_root = os.path.relpath(root, git_dir)
            for d in dirs:
                b.add_dir(os.path.join(rel_root, d))
            for f in files:
                p = Path(root) / f
                if p.is_symlink() or not p.is_file():
                    continue
                b.add_file(os.path.join(rel_root, f), p.read_bytes(), os.access(p, os.X_OK))
    if content is not None:
        name = (filename or "upload").lower()
        if name.endswith(".zip"):
            _add_zip(b, content)
        elif name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")):
            _add_tar(b, content)
        else:
            base = posixpath.basename((filename or "main.py").replace("\\", "/")) or "main.py"
            b.add_file(base, content)
    return b.finish()


def _add_zip(b: _Builder, content: bytes):
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as e:
        raise WorkspaceError(f"bad zip file: {e}")
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                b.add_dir(info.filename)
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode and mode != 0o100000:  # not a regular file (e.g. symlink)
                continue
            if b.total + info.file_size > b.max_bytes:
                raise WorkspaceError("workspace content exceeds size limit")
            b.add_file(info.filename, zf.read(info), bool((info.external_attr >> 16) & 0o111))


def _add_tar(b: _Builder, content: bytes):
    try:
        tf = tarfile.open(fileobj=io.BytesIO(content), mode="r:*")
    except tarfile.TarError as e:
        raise WorkspaceError(f"bad tar file: {e}")
    with tf:
        for m in tf:
            if m.isdir():
                b.add_dir(m.name)
            elif m.isfile():
                if b.total + m.size > b.max_bytes:
                    raise WorkspaceError("workspace content exceeds size limit")
                b.add_file(m.name, tf.extractfile(m).read(), bool(m.mode & 0o111))


def git_clone(url: str, dest: Path, timeout: int):
    """Shallow-clone an https git URL on the host (no hooks, no submodules)."""
    if not url.startswith("https://"):
        raise WorkspaceError("only https:// git URLs are supported")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": tempfile.gettempdir(),
    }
    cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.symlinks=false",
           "clone", "--depth", "1", "--no-tags", "--single-branch", "--", url, str(dest)]
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, timeout=timeout, text=True)
    except subprocess.TimeoutExpired:
        raise WorkspaceError("git clone timed out")
    if r.returncode != 0:
        raise WorkspaceError(f"git clone failed: {r.stderr.strip()[-500:]}")
