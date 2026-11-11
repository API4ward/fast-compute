"""HTTP API the bridge (aws-london) calls over the tailnet. Bearer token on every route.

  GET    /v1/info                  capacity / used / free cpus and mem_mb, active_jobs
  POST   /v1/jobs                  multipart: "spec" (JSON) + optional "workspace" (tar) -> 201
  GET    /v1/jobs/{id}             status, exit_code, oom, cpu/mem avg/peak, timestamps
  GET    /v1/jobs/{id}/logs?tail=N combined stdout/stderr (text/plain)
  POST   /v1/jobs/{id}/kill
  DELETE /v1/jobs/{id}             remove container + volume
"""
import hmac
import json
import logging
import re
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .runner import NodeRunner, Refused

log = logging.getLogger("computenode.server")
JOB_RE = re.compile(r"^/v1/jobs/([^/]+)(/logs|/kill)?$")


def parse_multipart(content_type: str, body: bytes) -> dict[str, bytes]:
    msg = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    if not msg.is_multipart():
        raise Refused("expected multipart/form-data")
    parts = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            parts[name] = part.get_payload(decode=True) or b""
    return parts


def make_handler(s: Settings, runner: NodeRunner):
    token = s.token.encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "computenode/0.1"

        def log_message(self, fmt, *args):
            log.info("%s %s", self.client_address[0], fmt % args)

        def _send(self, code, obj=None, raw: bytes | None = None, ctype="application/json"):
            body = raw if raw is not None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if s.allowed_clients and self.client_address[0] not in s.allowed_clients:
                return False
            auth = self.headers.get("Authorization", "")
            return auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip().encode(), token)

        def _handle(self, method):
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            url = urlparse(self.path)
            try:
                if url.path == "/v1/info" and method == "GET":
                    return self._send(200, runner.info())
                if url.path == "/v1/jobs" and method == "POST":
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > s.max_upload_bytes + 1024 * 1024:
                        raise Refused("request too large", 413)
                    parts = parse_multipart(self.headers.get("Content-Type", ""), self.rfile.read(n))
                    try:
                        spec = json.loads(parts.get("spec", b""))
                    except ValueError:
                        raise Refused("spec must be JSON")
                    job = runner.submit(spec, parts.get("workspace") or None)
                    return self._send(201, job.public())
                m = JOB_RE.match(url.path)
                if m:
                    jid, sub = m.group(1), m.group(2)
                    if sub is None and method == "GET":
                        return self._send(200, runner.get(jid).public())
                    if sub is None and method == "DELETE":
                        runner.remove(jid)
                        return self._send(200, {"deleted": jid})
                    if sub == "/logs" and method == "GET":
                        tail = parse_qs(url.query).get("tail", [None])[0]
                        data = runner.logs(jid, int(tail) if tail and tail.isdigit() else None)
                        return self._send(200, raw=data, ctype="text/plain; charset=utf-8")
                    if sub == "/kill" and method == "POST":
                        return self._send(200, runner.kill(jid).public())
                self._send(404, {"error": "not found"})
            except Refused as e:
                self._send(e.code, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                log.exception("request failed")
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_DELETE(self):
            self._handle("DELETE")

    return Handler


def serve(s: Settings):
    runner = NodeRunner(s)
    runner.start()
    host = s.bind_host()
    httpd = ThreadingHTTPServer((host, s.port), make_handler(s, runner))
    httpd.daemon_threads = True
    log.info("computenode %s listening on http://%s:%d (capacity %s cpus / %s MB)",
             s.node_name, host, s.port, runner.cap_cpus, runner.cap_mem_mb)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runner.shutdown()
        httpd.server_close()
