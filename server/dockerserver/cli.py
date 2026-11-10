"""Admin CLI: python -m dockerserver.cli <command>."""
import argparse
import sys

from sqlalchemy import select

from .auth import issue_token
from .billing import add_entry, grant_initial, to_uh
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
    c = sub.add_parser("credits", help="add (or with a negative number, deduct) credit-hours")
    c.add_argument("name")
    c.add_argument("hours", type=float)
    c.add_argument("--note", default="cli")
    args = p.parse_args(argv)

    settings = load_settings()
    Session = make_session_factory(settings.db_url)
    with Session() as db:
        if args.cmd == "create-admin":
            if db.scalar(select(User).where(User.name == args.name)):
                sys.exit(f"user {args.name!r} already exists (use issue-token)")
            u = User(name=args.name, role="admin", max_cpus=2.0, max_mem_mb=512)
            db.add(u)
            db.flush()
            grant_initial(db, u, settings.default_credit_hours)
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
                      f"mem={u.max_mem_mb}MB\tmax_run={u.max_run_seconds}s\tbalance={u.balance_hours:.4f}h")
        elif args.cmd == "credits":
            u = db.scalar(select(User).where(User.name == args.name))
            if not u:
                sys.exit(f"no such user {args.name!r}")
            e = add_entry(db, u.id, to_uh(args.hours), "topup" if args.hours > 0 else "adjust", note=args.note)
            db.commit()
            print(f"{u.name}: balance now {e.balance_after_uh / 1e6:.4f} hours")


if __name__ == "__main__":
    main()
