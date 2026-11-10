"""Admin CLI: python -m dockerserver.cli <command>."""
import argparse
import sys

from sqlalchemy import select

from .auth import issue_token
from .config import load_settings
from .models import User, make_session_factory


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m dockerserver.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("create-admin", help="create an admin user and print its token")
    a.add_argument("name", nargs="?", default="admin")
    t = sub.add_parser("issue-token", help="issue a new token for an existing user")
    t.add_argument("name")
    sub.add_parser("list-users")
    args = p.parse_args(argv)

    settings = load_settings()
    Session = make_session_factory(settings.db_url)
    with Session() as db:
        if args.cmd == "create-admin":
            if db.scalar(select(User).where(User.name == args.name)):
                sys.exit(f"user {args.name!r} already exists (use issue-token)")
            u = User(name=args.name, role="admin", max_cpus=2.0, max_mem_mb=512)
            db.add(u)
            db.commit()
            _, raw = issue_token(db, u, "cli")
            print(f"created admin {u.name!r} (id {u.id})\ntoken: {raw}")
        elif args.cmd == "issue-token":
            u = db.scalar(select(User).where(User.name == args.name))
            if not u:
                sys.exit(f"no such user {args.name!r}")
            _, raw = issue_token(db, u, "cli")
            print(f"token for {u.name!r}: {raw}")
        elif args.cmd == "list-users":
            for u in db.scalars(select(User).order_by(User.id)):
                print(f"{u.id}\t{u.name}\t{u.role}\tactive={u.active}\tcpus={u.max_cpus}\t"
                      f"mem={u.max_mem_mb}MB\tmax_run={u.max_run_seconds}s")


if __name__ == "__main__":
    main()
