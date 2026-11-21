"""Service jobs: validation, forwarder access rules, and (with Docker) real isolation."""
import json
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from computenode.config import Settings  # noqa: E402
from computenode.ingress import Forwarder  # noqa: E402
from computenode.runner import LABEL, NodeRunner, Refused, check_service_port  # noqa: E402

IMAGE = "python:3.12-slim"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def settings(**kw) -> Settings:
    kw.setdefault("allowed_clients", ["127.0.0.1"])
    return Settings(token="t" * 32, listen="127.0.0.1", **kw)


def runner(**kw) -> NodeRunner:
    r = NodeRunner(settings(**kw), client=object())
    r.total_cpus, r.total_mem_mb = 4, 8192
    return r


@pytest.mark.parametrize("bad", [80, 1023, 65536, "8080", 8080.0, True])
def test_service_port_rules(bad):
    with pytest.raises(Refused):
        check_service_port(bad)


def test_service_port_valid():
    assert check_service_port(None) is None and check_service_port(8080) == 8080


def spec(**kw):
    return {"id": "s1", "image": IMAGE, "command": "true", "cpus": 1, "mem_mb": 64,
            "timeout_seconds": 60, "service_port": 8080, **kw}


def test_service_refused_without_client_allowlist():
    r = runner(allowed_clients=[])
    assert "service" not in r.info()["features"]
    with pytest.raises(Refused) as e:
        r.submit(spec(), None)
    assert e.value.code == 403


def test_service_cannot_request_outbound_network():
    with pytest.raises(Refused):
        runner(allow_network=True).submit(spec(network=True), None)


def test_only_services_get_the_long_timeout():
    r = runner(max_timeout_seconds=100, max_service_seconds=1000)
    with pytest.raises(Refused):
        r.submit({**spec(timeout_seconds=500), "service_port": None}, None)
    with pytest.raises(Refused):
        r.submit(spec(timeout_seconds=1001), None)


def test_subnets_never_overlap_and_must_fit():
    r = runner(ingress_port_min=9100, ingress_port_max=9199)
    subs = [s for p in range(9100, 9200) for s in r._service_subnets(p)]
    assert len(set(subs)) == 200
    small = runner(service_subnet="10.213.0.0/27", ingress_port_min=9100)
    small._service_subnets(9100)      # a /27 holds exactly one pair of /28s
    with pytest.raises(RuntimeError):
        small._service_subnets(9101)


def test_relay_id_not_public():
    from computenode.runner import NodeJob
    j = NodeJob("j", IMAGE, "true", 1, 64, 60, False, service_port=8080, relay_id="secret-id")
    assert "relay_id" not in j.public() and j.public()["service_port"] == 8080


@pytest.fixture
def echo_server():
    srv = socket.create_server(("127.0.0.1", 0))

    def run():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            with c:
                while d := c.recv(4096):
                    c.sendall(d)

    threading.Thread(target=run, daemon=True).start()
    yield srv.getsockname()[1]
    srv.close()


def test_forwarder_passes_allowed_client(echo_server):
    f = Forwarder(["127.0.0.1"])
    port = free_port()
    f.open("127.0.0.1", port, echo_server)
    try:
        with socket.create_connection(("127.0.0.1", port), 3) as c:
            c.sendall(b"hello")
            assert c.recv(16) == b"hello"
    finally:
        f.shutdown()


def test_forwarder_sends_preamble_first(echo_server):
    f = Forwarder(["127.0.0.1"])
    port = free_port()
    f.open("127.0.0.1", port, echo_server, b"secret:")
    try:
        with socket.create_connection(("127.0.0.1", port), 3) as c:
            c.sendall(b"hello")
            got = b""
            while len(got) < 12:
                got += c.recv(16)
            assert got == b"secret:hello"
    finally:
        f.shutdown()


def test_forwarder_rejects_other_clients(echo_server):
    f = Forwarder(["100.82.2.33"])
    port = free_port()
    f.open("127.0.0.1", port, echo_server)
    try:
        with socket.create_connection(("127.0.0.1", port), 3) as c:
            c.settimeout(3)
            try:
                c.sendall(b"hello")
                assert c.recv(16) == b""
            except ConnectionError:
                pass
    finally:
        f.shutdown()


def test_forwarder_limits_connections(echo_server):
    f = Forwarder(["127.0.0.1"], max_conns=1)
    port = free_port()
    f.open("127.0.0.1", port, echo_server)
    try:
        with socket.create_connection(("127.0.0.1", port), 3) as first:
            first.sendall(b"a")
            assert first.recv(4) == b"a"
            with socket.create_connection(("127.0.0.1", port), 3) as second:
                second.settimeout(3)
                try:
                    second.sendall(b"b")
                    assert second.recv(4) == b""
                except ConnectionError:
                    pass
    finally:
        f.shutdown()


# ---------- real Docker ----------
APP = """
import http.server as h
class H(h.BaseHTTPRequestHandler):
    def do_GET(s):
        b = b"service-ok"; s.send_response(200); s.send_header("Content-Length", str(len(b)))
        s.end_headers(); s.wfile.write(b)
    def log_message(s, *a): pass
h.ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
"""

PROBE = """
import json, os, socket
out = {}
for name, host, port in json.loads(os.environ["T"]):
    s = socket.socket(); s.settimeout(2)
    try:
        s.connect((host, port)); out[name] = "open"
    except ConnectionRefusedError:
        out[name] = "refused"
    except OSError:
        out[name] = "blocked"
    finally:
        s.close()
print(json.dumps(out))
"""

# Connect-only is not evidence on Docker Desktop, whose network stack accepts TCP to any address.
# This sends a real request and reports what actually came back.
DATA_PROBE = """
import json, os, socket
out = {}
for name, host, port in json.loads(os.environ["T"]):
    s = socket.socket(); s.settimeout(3); d = b""
    try:
        s.connect((host, port)); s.sendall(b"GET / HTTP/1.0\\r\\nHost: x\\r\\n\\r\\n" + b" " * 64)
        while c := s.recv(4096):
            d += c
    except OSError:
        pass
    finally:
        s.close()
    out[name] = d.decode(errors="replace")
print(json.dumps(out))
"""


@pytest.fixture
def docker_runner():
    docker = pytest.importorskip("docker")
    try:
        client = docker.from_env()
        client.ping()
        client.images.get(IMAGE)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"docker with {IMAGE} not available: {e}")
    # A range unlikely to clash with a real node on the same machine
    r = NodeRunner(settings(ingress_port_min=19100, ingress_port_max=19109,
                            service_subnet="10.214.0.0/24", stats_interval=2), client=client)
    r.total_cpus, r.total_mem_mb = 2, 1024
    r.docker_version = "test"
    yield r
    for jid in list(r.jobs):
        r.remove(jid)
    if r._forwarder:
        r._forwarder.shutdown()


def wait_ready(r, jid, timeout=40):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        j = r.get(jid)
        if j.service_ready:
            return j
        assert j.status in ("preparing", "running"), j.error
        time.sleep(0.3)
    raise AssertionError("service never became ready")


def test_service_end_to_end_and_isolated(docker_runner):
    r = docker_runner
    jid = f"svc-test-{int(time.time())}"
    job = r.submit({"id": jid, "image": IMAGE, "command": f"python -c '{APP}'", "cpus": 0.5,
                    "mem_mb": 64, "timeout_seconds": 120, "service_port": 8080}, None)
    wait_ready(r, jid)
    url = f"http://127.0.0.1:{job.ingress_port}/"
    assert urllib.request.urlopen(url, timeout=5).read() == b"service-ok"

    client = r.client
    app = client.containers.get(job.container_id)
    relay = client.containers.get(job.relay_id)
    app_name, edge_name, _ = r._service_names(jid)
    relay.reload()
    nets = relay.attrs["NetworkSettings"]["Networks"]
    relay_app_ip, relay_edge_ip = nets[app_name]["IPAddress"], nets[edge_name]["IPAddress"]
    edge_gw = client.networks.get(edge_name).attrs["IPAM"]["Config"][0]["Gateway"]

    # a bystander on the default bridge, as a network=true job would be
    bystander = client.containers.run(IMAGE, ["python", "-m", "http.server", "8081"], detach=True,
                                      labels={LABEL: "svc-test-bystander"}, network_mode="bridge",
                                      user="65534:65534", working_dir="/tmp")
    try:
        bystander.reload()
        by_ip = bystander.attrs["NetworkSettings"]["Networks"]["bridge"]["IPAddress"]
        targets = [["relay listener via app net", relay_app_ip, 9000],
                   ["relay edge address", relay_edge_ip, 9000],
                   ["edge gateway ssh", edge_gw, 22],
                   ["default bridge container", by_ip, 8081],
                   ["default bridge gateway", "172.17.0.1", 22],
                   ["internet", "1.1.1.1", 443]]
        res = app.exec_run(["python", "-c", PROBE], environment={"T": json.dumps(targets)})
        result = json.loads(res.output)
        # the relay is the app's only neighbour; it has no listener on the app network
        assert result.pop("relay listener via app net") == "refused"
        assert all(v == "blocked" for v in result.values()), result

        # another container cannot reach the relay and so cannot skip the bridge's auth
        app.reload()
        app_ip = app.attrs["NetworkSettings"]["Networks"][app_name]["IPAddress"]
        res = bystander.exec_run(["python", "-c", DATA_PROBE], environment={"T": json.dumps(
            [["relay edge", relay_edge_ip, 9000], ["app", app_ip, 8080]])})
        assert "service-ok" not in json.loads(res.output)["relay edge"], res.output
        assert "service-ok" not in json.loads(res.output)["app"], res.output

        # even from the host side, the relay's published port serves nothing without the secret
        published = relay.attrs["NetworkSettings"]["Ports"]["9000/tcp"][0]["HostPort"]
        with socket.create_connection(("127.0.0.1", int(published)), 3) as c:
            c.settimeout(8)
            c.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n" + b" " * 64)
            got = b""
            try:
                while d := c.recv(4096):
                    got += d
            except OSError:
                pass
        assert b"service-ok" not in got
    finally:
        bystander.remove(force=True)

    r.kill(jid)
    end = time.monotonic() + 20
    while r.get(jid).status in ("preparing", "running") and time.monotonic() < end:
        time.sleep(0.2)
    time.sleep(1)
    assert r.get(jid).status == "killed"
    assert job.ingress_port not in r._ingress_ports
    assert not client.networks.list(names=[app_name]) and not client.networks.list(names=[edge_name])
    assert not [c for c in client.containers.list(all=True) if c.name == r._service_names(jid)[2]]
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", job.ingress_port), 1)
