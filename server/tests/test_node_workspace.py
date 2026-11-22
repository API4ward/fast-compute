"""Files sent to a compute node must land directly in /workspace, not /workspace/ws."""
import io
import tarfile

from dockerserver.backends import node_workspace
from dockerserver.workspace import build_workspace


def _tar(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode = 0o755 if data.startswith(b"#!") else 0o644
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def test_paths_are_rerooted_and_content_kept():
    payload = build_workspace("inline.tar", _tar({"main.py": b"print(1)", "pkg/a.sh": b"#!/bin/sh\n"}),
                              None, 65534, 65534, 10_000_000)
    with tarfile.open(fileobj=io.BytesIO(node_workspace(payload))) as t:
        names = {m.name: m for m in t.getmembers()}
        assert "main.py" in names and "pkg/a.sh" in names
        assert not [n for n in names if n == "ws" or n.startswith("ws/") or n == ""]
        assert t.extractfile("main.py").read() == b"print(1)"
        assert names["pkg/a.sh"].mode & 0o111
        assert names["main.py"].uid == 65534


def test_a_user_directory_named_ws_is_kept():
    payload = build_workspace("inline.tar", _tar({"ws/main.py": b"x"}), None, 65534, 65534, 10_000_000)
    with tarfile.open(fileobj=io.BytesIO(node_workspace(payload))) as t:
        assert "ws/main.py" in t.getnames()


def test_empty_workspace_sends_nothing():
    assert node_workspace(b"") == b""
    assert node_workspace(build_workspace(None, None, None, 65534, 65534, 10_000_000)) == b""
