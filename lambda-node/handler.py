"""fast-compute job runner inside AWS Lambda (one invocation = one job).

The server (dockerserver, lambda_backend.py) invokes the function asynchronously with an
event that carries the job and short-lived pre-signed S3 URLs:

    {"job": "ds12-1700000000", "command": "python main.py", "timeout_seconds": 600,
     "env": {...}, "max_log_bytes": 1048576, "fresh_sandbox": true,
     "workspace_url": GET url or null, "status_url": PUT url, "logs_url": PUT url,
     "kill_url": GET url}

This handler needs NO AWS permissions: it only talks to those URLs (standard library only).
The function's execution role should have no policies (or only VPC access in isolated mode),
so the role credentials Lambda puts in this process's environment are worthless to job code.

Flow: status "running" -> unpack workspace to /tmp/fc/ws -> run `sh -c command` there
(own process group, clean environment) -> every few seconds upload the log tail, refresh
the status heartbeat and look for a kill request -> final status + log -> exit the process.

fresh_sandbox: Lambda reuses execution environments between invocations, so a job could leave
a background process or files behind for the next user. After reporting, the handler ends the
whole runtime process (os._exit), which makes Lambda discard this environment; the next job
gets a cold, clean one. The async invoke config must have MaximumRetryAttempts=0 (deploy.sh
sets it) so this deliberate exit is never retried.
"""
import io
import json
import os
import resource
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request

ROOT = "/tmp/fc"
USED_MARK = "/tmp/.fc-used"
TICK = 0.5
LOG_EVERY = 3.0
HEARTBEAT_EVERY = 10.0
KILL_EVERY = 2.0
SAFETY_MS = 15000          # stop the job this long before Lambda's own hard timeout
PASS_ENV = ("LANG", "LC_ALL", "TZ")


def _http(method, url, data=None, content_type=None, timeout=20, tries=3):
    """Returns (status, body); status 0 when the request failed outright."""
    last = (0, b"")
    for i in range(tries):
        req = urllib.request.Request(url, data=data, method=method)
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return e.code, b""
            last = (e.code, b"")
        except (urllib.error.URLError, OSError):
            last = (0, b"")
        time.sleep(0.5 * (2 ** i))
    return last


class Reporter:
    def __init__(self, ev):
        self.ev = ev
        self.state = {"job": ev.get("job"), "status": "preparing", "beat": 0}
        self.lock = threading.Lock()

    def status(self, **fields):
        with self.lock:
            self.state.update(fields)
            self.state["beat"] += 1
            self.state["updated_at"] = time.time()
            body = json.dumps(self.state).encode()
        _http("PUT", self.ev["status_url"], body, "application/json")

    def logs(self, path):
        limit = int(self.ev.get("max_log_bytes") or 1024 * 1024)
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                if size > limit:
                    f.seek(size - limit)
                    data = b"[... log truncated ...]\n" + f.read()
                else:
                    data = f.read()
        except OSError:
            data = b""
        _http("PUT", self.ev["logs_url"], data, "text/plain; charset=utf-8")

    def kill_requested(self):
        url = self.ev.get("kill_url")
        return bool(url) and _http("GET", url, timeout=5, tries=1)[0] == 200


def _clean_tmp():
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(os.path.join(ROOT, "ws"), exist_ok=True)


def _kill_strays():
    """Environment was used before (fresh_sandbox off or an exit was skipped): kill every
    process except this one and its ancestors, so nothing from an earlier job survives."""
    keep, pid = {os.getpid()}, os.getpid()
    while pid > 1:
        try:
            with open(f"/proc/{pid}/stat") as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
            keep.add(pid)
        except (OSError, ValueError, IndexError):
            break
    for _ in range(3):
        for name in os.listdir("/proc"):
            if name.isdigit() and int(name) not in keep and int(name) != 1:
                try:
                    os.kill(int(name), signal.SIGKILL)
                except OSError:
                    pass
        time.sleep(0.05)


def _unpack(ev, dest):
    url = ev.get("workspace_url")
    if not url:
        return
    code, body = _http("GET", url, timeout=60)
    if code != 200:
        raise RuntimeError(f"workspace download failed (HTTP {code})")
    with tarfile.open(fileobj=io.BytesIO(body)) as t:
        t.extractall(dest, filter="data")


def _job_env(ev, home):
    env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
    env.update({k: str(v) for k, v in (ev.get("env") or {}).items()})
    env.update({"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home, "PYTHONUNBUFFERED": "1",
                "TMPDIR": "/tmp"})
    return env   # nothing from the Lambda runtime (AWS_* credentials, _HANDLER, ...) is passed on


def _killpg(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass


def run(ev, context=None, now=time.time):
    rep = Reporter(ev)
    log_path = os.path.join(ROOT, "output.log")
    ws = os.path.join(ROOT, "ws")
    started = now()
    try:
        _clean_tmp()
        rep.status(status="preparing", started_at=started)
        _unpack(ev, ws)
    except Exception as e:  # noqa: BLE001
        rep.status(status="error", error=f"prepare: {e}", finished_at=now())
        return {"status": "error"}

    timeout = float(ev.get("timeout_seconds") or 600)
    if context is not None:
        timeout = min(timeout, max(context.get_remaining_time_in_millis() - SAFETY_MS, 1000) / 1000)
    with open(log_path, "wb") as out:
        p = subprocess.Popen(["/bin/sh", "-c", ev["command"]], cwd=ws, env=_job_env(ev, ws),
                             stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                             start_new_session=True)
    t0 = time.monotonic()
    rep.status(status="running", started_at=now())
    reason = None
    last_log = last_beat = last_kill = t0
    log_size = -1
    while p.poll() is None:
        time.sleep(TICK)
        m = time.monotonic()
        if m - t0 >= timeout:
            reason = "timeout"
            break
        if m - last_kill >= KILL_EVERY:
            last_kill = m
            if rep.kill_requested():
                reason = "killed"
                break
        if m - last_log >= LOG_EVERY:
            last_log = m
            try:
                sz = os.path.getsize(log_path)
            except OSError:
                sz = 0
            if sz != log_size:
                log_size = sz
                rep.logs(log_path)
        if m - last_beat >= HEARTBEAT_EVERY:
            last_beat = m
            rep.status()
    if reason:
        _killpg(p)
    code = p.wait()
    _killpg(p)                     # leftovers in the job's process group
    wall = max(time.monotonic() - t0, 1e-6)
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    stats = {"stat_samples": 1, "cpu_avg_pct": round((ru.ru_utime + ru.ru_stime) / wall * 100, 1),
             "mem_peak_mb": round(ru.ru_maxrss / 1024, 1)}
    stats["cpu_peak_pct"], stats["mem_avg_mb"] = stats["cpu_avg_pct"], None
    if reason == "timeout":
        status, exit_code = "timeout", 137
    elif reason == "killed":
        status, exit_code = "killed", 137
    else:
        exit_code = code if code >= 0 else 128 - code
        status = "succeeded" if exit_code == 0 else "failed"
    rep.logs(log_path)
    rep.status(status=status, exit_code=exit_code, finished_at=now(), duration=wall, **stats)
    return {"status": status, "exit_code": exit_code}


def handler(event, context):
    if os.path.exists(USED_MARK):
        _kill_strays()
    try:
        open(USED_MARK, "w").close()
    except OSError:
        pass
    try:
        result = run(event, context)
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)
    if event.get("fresh_sandbox", True):
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)              # discard this execution environment (see module docstring)
    return result
