"""Egress isolation for network=true jobs. The Docker test runs a real job and probes from inside."""
import json
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from computenode.config import Settings  # noqa: E402
from computenode.egress import EGRESS_CODE  # noqa: E402
from computenode.runner import LABEL, NodeRunner  # noqa: E402

ns = {"__name__": "egress"}
exec(EGRESS_CODE, ns)  # noqa: S102


@pytest.mark.parametrize("addr", [
    "127.0.0.1", "10.0.0.1", "172.17.0.1", "192.168.1.1", "169.254.169.254", "100.100.100.100",
    "100.126.20.127", "0.0.0.0", "198.18.0.1", "192.0.2.1", "224.0.0.1", "255.255.255.255",
    "::1", "fd7a:115c:a1e0::1", "::ffff:127.0.0.1", "2606:4700::1", "nonsense"])
def test_non_public_ipv4_refused(addr):
    assert not ns["public_ipv4"](addr)


def test_public_ipv4_allowed():
    assert ns["public_ipv4"]("1.1.1.1") and ns["public_ipv4"]("93.184.216.34")


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "2130706433", "0x7f.0.0.1", "0177.0.0.1",
                                  "169.254.169.254", "10.1.2.3", "bad host", ""])
def test_resolve_refuses_internal_forms(host):
    with pytest.raises(ns["Denied"]):
        ns["resolve"](host, 443, {80, 443})


@pytest.mark.parametrize("host", ["0177.0.0.1", "010.0.0.1", "1.1.1.01", "0x01.1.1.1", "1.1.1.0x1",
                                  "16843009", "1.1", "1.1.1.1.", "127.1"])
def test_ambiguous_numeric_hosts_never_reach_the_resolver(host, monkeypatch):
    def fail(*a, **k):
        raise AssertionError("resolver was called")
    monkeypatch.setattr(ns["socket"], "getaddrinfo", fail)
    with pytest.raises(ns["Denied"]):
        ns["resolve"](host, 443, {443})


def test_resolve_refuses_if_any_answer_is_private(monkeypatch):
    answers = [(2, 1, 6, "", ("1.1.1.1", 443)), (2, 1, 6, "", ("10.0.0.5", 443))]
    monkeypatch.setattr(ns["socket"], "getaddrinfo", lambda *a, **k: answers)
    with pytest.raises(ns["Denied"]):
        ns["resolve"]("rebind.example", 443, {443})


def test_resolve_refuses_other_ports():
    with pytest.raises(ns["Denied"]):
        ns["resolve"]("1.1.1.1", 22, {80, 443})


def test_parse_head_rewrites_forward_request():
    head = (b"GET http://example.com/a?b=1 HTTP/1.1\r\nHost: example.com\r\n"
            b"Proxy-Authorization: x\r\nConnection: keep-alive\r\n\r\n")
    method, host, port, first = ns["parse_head"](head)
    assert (method, host, port) == ("GET", "example.com", 80)
    assert first.startswith(b"GET /a?b=1 HTTP/1.1\r\n") and b"Proxy-Authorization" not in first
    assert first.endswith(b"Connection: close\r\n\r\n") and b"keep-alive" not in first


@pytest.mark.parametrize("head", [b"GET /relative HTTP/1.1\r\n\r\n", b"GET ftp://x/ HTTP/1.1\r\n\r\n",
                                  b"CONNECT nope HTTP/1.1\r\n\r\n", b"garbage\r\n\r\n",
                                  b"GET http://a/ HTTP/1.1\r\nX: a\nY: b\r\n\r\n"])
def test_parse_head_rejects(head):
    with pytest.raises(ns["Denied"]):
        ns["parse_head"](head)


PROBE = r"""
import json, socket, urllib.request as U, urllib.error as E
PROXY = __import__("os").environ["HTTPS_PROXY"]
out = {}
def direct(name, host, port):
    s = socket.socket(); s.settimeout(3)
    try:
        s.connect((host, port)); s.sendall(b"GET / HTTP/1.0\r\n\r\n")
        out[name] = "DATA" if s.recv(16) else "closed"
    except OSError:
        out[name] = "blocked"
    finally:
        s.close()
def via(name, url):
    try:
        out[name] = U.urlopen(url, timeout=12).status
    except E.HTTPError as e:
        out[name] = e.code
    except Exception as e:
        out[name] = "error:" + type(e).__name__
via("public_https", "https://example.com/")
via("public_http", "http://example.com/")
for n, h in (("metadata", "169.254.169.254"), ("gateway", "172.17.0.1"), ("loopback", "127.0.0.1"),
             ("desktop_host", "host.docker.internal"), ("lan", "192.168.1.1"), ("tailnet", "100.100.100.100")):
    via("proxy_" + n, f"http://{h}/")
via("proxy_port22", "http://example.com:22/")
def connect(name, target):
    host, port = PROXY.split("//")[1].split(":")
    c = socket.create_connection((host, int(port)), timeout=12)
    c.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    out[name] = int(c.recv(64).split()[1]); c.close()
connect("connect_public", "example.com:443")
connect("connect_metadata", "169.254.169.254:443")
connect("connect_loopback", "127.0.0.1:443")
connect("connect_decimal_loopback", "2130706433:443")
connect("connect_port22", "example.com:22")
direct("direct_public", "1.1.1.1", 80)
direct("direct_metadata", "169.254.169.254", 80)
direct("direct_gateway", "172.17.0.1", 80)
direct("direct_tailnet", "100.100.100.100", 80)
try:
    socket.gethostbyname("example.com"); out["job_dns"] = "resolved"
except OSError:
    out["job_dns"] = "blocked"
print("RESULT" + json.dumps(out))
"""


def test_job_gets_public_http_only():
    docker = pytest.importorskip("docker")
    try:
        client = docker.from_env()
        client.ping()
    except docker.errors.DockerException:
        pytest.skip("no Docker")
    s = Settings(token="t" * 32, allow_network=True)
    r = NodeRunner(s, client=client)
    r.total_cpus, r.total_mem_mb = 2, 1024
    r.docker_version = "test"
    jid = "egress-" + uuid.uuid4().hex[:8]
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        data = PROBE.encode()
        info = tarfile.TarInfo("probe.py")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    job = r.submit({"id": jid, "image": "python:3.12-slim", "command": "python probe.py", "cpus": 0.5,
                    "mem_mb": 128, "timeout_seconds": 120, "network": True}, buf.getvalue())
    try:
        deadline = time.monotonic() + 150
        while job.status in ("preparing", "running") and time.monotonic() < deadline:
            time.sleep(0.5)
        logs = r.logs(jid).decode(errors="replace")
        assert job.status == "succeeded", f"{job.status} {job.error} {logs[-800:]}"
        res = json.loads(logs.split("RESULT", 1)[1])
        print(res)
        assert res["public_https"] == 200 and res["public_http"] == 200
        for k in ("metadata", "gateway", "loopback", "desktop_host", "lan", "tailnet", "port22"):
            assert res["proxy_" + k] == 403, (k, res)
        assert res["connect_public"] == 200
        for k in ("metadata", "loopback", "decimal_loopback", "port22"):
            assert res["connect_" + k] == 403, (k, res)
        for k in ("direct_public", "direct_metadata", "direct_gateway", "direct_tailnet"):
            assert res[k] != "DATA", (k, res)
        assert res["job_dns"] == "blocked"
        # the proxy and the network are gone as soon as the job ends
        name = f"computenode-egress-{jid}"
        for _ in range(100):    # teardown runs just after the job's status turns final
            if not client.api.containers(all=True, filters={"name": name}) \
                    and not client.api.networks(names=[name]):
                break
            time.sleep(0.1)
        else:
            pytest.fail("egress proxy or network left behind")
    finally:
        r.remove(jid)
