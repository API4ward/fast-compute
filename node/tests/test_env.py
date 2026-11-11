"""Node-side handling of job environment variables (no Docker needed)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from computenode.config import Settings  # noqa: E402
from computenode.runner import NodeJob, NodeRunner, Refused, check_env  # noqa: E402


def test_check_env_rules():
    assert check_env(None) == {} and check_env({"API_KEY": "x"}) == {"API_KEY": "x"}
    for bad in ({"PATH": "/x"}, {"LD_PRELOAD": "x"}, {"COMPUTENODE_TOKEN": "x"}, {"1A": "x"},
                {"A": 1}, {"A": "a\x00"}, {"A": "x" * 4097}, ["A"], {f"V{i}": "1" for i in range(51)}):
        with pytest.raises(Refused):
            check_env(bad)


def test_env_never_in_public_job_view():
    j = NodeJob("j1", "python:3.12-slim", "true", 1, 256, 60, False, env={"SECRET": "s3cr3t"})
    assert "env" not in j.public() and "s3cr3t" not in str(j.public()) and "s3cr3t" not in repr(j)


def test_info_advertises_env_feature():
    s = Settings(token="t" * 32)
    r = NodeRunner(s, client=object())
    r.total_cpus, r.total_mem_mb = 4, 8192
    assert "env" in r.info()["features"]
