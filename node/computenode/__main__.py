"""computenode init | serve | token"""
import argparse
import logging
import secrets
import sys

from .config import DEFAULT_CONFIG, load_settings

TEMPLATE = """# DockerServer compute node. Values here are overridden by real environment variables.
COMPUTENODE_TOKEN={token}
COMPUTENODE_LISTEN=tailscale
COMPUTENODE_PORT=8900
# Only the bridge (aws-london) may call this node.
COMPUTENODE_ALLOWED_CLIENTS=100.82.2.33
COMPUTENODE_ALLOWED_IMAGES=python:3.12-slim
COMPUTENODE_ALLOW_NETWORK=false
COMPUTENODE_RESERVE_CPUS=2
COMPUTENODE_RESERVE_MEM_MB=8192
"""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="computenode")
    sub = ap.add_subparsers(dest="cmd", required=True)
    init = sub.add_parser("init", help="write config with a fresh token")
    init.add_argument("--force", action="store_true")
    sub.add_parser("serve", help="run the node API")
    sub.add_parser("token", help="print the token")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if a.cmd == "init":
        if DEFAULT_CONFIG.exists() and not a.force:
            sys.exit(f"{DEFAULT_CONFIG} already exists (use --force to overwrite)")
        DEFAULT_CONFIG.write_text(TEMPLATE.format(token=secrets.token_urlsafe(32)))
        DEFAULT_CONFIG.chmod(0o600)
        print(f"wrote {DEFAULT_CONFIG}")
    elif a.cmd == "token":
        print(load_settings().token)
    else:
        from .server import serve
        serve(load_settings())


if __name__ == "__main__":
    main()
