"""End-to-end tests that really run containers (small: 0.5 CPU / 64-128 MB)."""
import uuid

from conftest import auth, make_user, needs_docker, wait_done


@needs_docker
def test_hello_world_and_isolation(app, client, admin_token, tmp_path):
    # A secret file on the host; the job must not be able to see it.
    marker = tmp_path / f"host-secret-{uuid.uuid4().hex}"
    marker.write_text("TOPSECRET")
    u = make_user(client, admin_token, "bob", max_cpus=0.5, max_mem_mb=128, max_run_seconds=60)
    script = f"""
import os, socket, subprocess
print("hello from container")
print("uid", os.getuid())
print("host_file_visible", os.path.exists({str(marker)!r}))
print("home_ubuntu_visible", os.path.exists("/home/ubuntu"))
print("docker_sock_visible", os.path.exists("/var/run/docker.sock"))
try:
    open("/usr/evil", "w"); print("rootfs_writable True")
except OSError: print("rootfs_writable False")
open("/workspace/out.txt", "w").write("ok"); print("workspace_writable True")
print("mem_max", open("/sys/fs/cgroup/memory.max").read().strip())
print("cpu_max", open("/sys/fs/cgroup/cpu.max").read().strip())
try:
    socket.create_connection(("1.1.1.1", 53), timeout=2); print("network True")
except OSError: print("network False")
"""
    h = auth(u["token"])
    r = client.post("/jobs", data={"command": "python main.py", "mem_mb": "64", "cpus": "0.5"},
                    files={"file": ("main.py", script.encode())}, headers=h)
    assert r.status_code == 201, r.text
    job = wait_done(client, u["token"], r.json()["id"])
    logs = client.get(f"/jobs/{job['id']}/logs", headers=h).text
    assert job["status"] == "succeeded", (job, logs)
    assert job["exit_code"] == 0 and job["started_at"] and job["finished_at"]
    assert "hello from container" in logs
    assert "uid 65534" in logs
    assert "host_file_visible False" in logs
    assert "home_ubuntu_visible False" in logs
    assert "docker_sock_visible False" in logs
    assert "rootfs_writable False" in logs
    assert "workspace_writable True" in logs
    assert f"mem_max {64 * 1024 * 1024}" in logs
    assert "cpu_max 50000 100000" in logs
    assert "network False" in logs


@needs_docker
def test_timeout_kills_container(client, admin_token):
    u = make_user(client, admin_token, "carol", max_cpus=0.5, max_mem_mb=64, max_run_seconds=3)
    r = client.post("/jobs", data={"command": "echo start; sleep 60; echo never"}, headers=auth(u["token"]))
    job = wait_done(client, u["token"], r.json()["id"], timeout=60)
    assert job["status"] == "timeout"
    assert job["duration_seconds"] < 20
    logs = client.get(f"/jobs/{job['id']}/logs", headers=auth(u["token"])).text
    assert "start" in logs and "never" not in logs


@needs_docker
def test_stop_job_and_failure_exit_code(client, admin_token):
    import time
    u = make_user(client, admin_token, "dave", max_cpus=0.5, max_mem_mb=64, max_run_seconds=60)
    h = auth(u["token"])
    r = client.post("/jobs", data={"command": "sleep 60"}, headers=h)
    jid = r.json()["id"]
    for _ in range(60):
        if client.get(f"/jobs/{jid}", headers=h).json()["status"] == "running":
            break
        time.sleep(0.5)
    assert client.post(f"/jobs/{jid}/stop", headers=h).json()["status"] == "stopped"

    r = client.post("/jobs", data={"command": "exit 3"}, headers=h)
    job = wait_done(client, u["token"], r.json()["id"])
    assert job["status"] == "failed" and job["exit_code"] == 3
