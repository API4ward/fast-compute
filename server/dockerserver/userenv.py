"""Per-user environment variables, injected into every job the user runs.

* Stored in the user_env table, values encrypted at rest (Fernet, key = DOCKERSERVER_SECRET_KEY).
* A per-job `env` (submit_job) overrides the user's variables for that job only; per-job
  values are never stored - jobs record only the variable names.
* Only the owner can see their values (masked by default); admins can't read them anywhere.
* Values are never logged.

Rules: names match [A-Za-z_][A-Za-z0-9_]* (max 64 chars), values up to 4096 characters without NUL,
at most 50 variables per user and 32 KiB in total per job. Reserved (refused): PATH, HOME,
HOSTNAME, PWD, PYTHONHOME, PYTHONUNBUFFERED and names starting with LD_ or DOCKERSERVER_.
"""
import re

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import UserEnv, utcnow

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
RESERVED = {"PATH", "HOME", "HOSTNAME", "PWD", "PYTHONHOME", "PYTHONUNBUFFERED"}
RESERVED_PREFIXES = ("LD_", "DOCKERSERVER_")
MAX_VALUE = 4096
MAX_VARS = 50
MAX_TOTAL = 32 * 1024


class EnvError(ValueError):
    pass


def check_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise EnvError(f"invalid variable name {name!r}: use letters, digits and _ (not starting "
                       "with a digit), at most 64 characters")
    if name.upper() in RESERVED or name.upper().startswith(RESERVED_PREFIXES):
        raise EnvError(f"{name} is reserved and cannot be set")
    return name


def check_value(name: str, value) -> str:
    if not isinstance(value, str):
        raise EnvError(f"value of {name} must be a string")
    if len(value) > MAX_VALUE:
        raise EnvError(f"value of {name} is longer than {MAX_VALUE} characters")
    if "\x00" in value:
        raise EnvError(f"value of {name} contains a NUL character")
    return value


def check_env(env: dict | None) -> dict[str, str]:
    """Validate a {name: value} mapping (per-job env or the merged result)."""
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise EnvError("env must be an object of NAME: value")
    out = {check_name(k): check_value(k, v) for k, v in env.items()}
    if len(out) > MAX_VARS:
        raise EnvError(f"at most {MAX_VARS} environment variables")
    if sum(len(k) + len(v) for k, v in out.items()) > MAX_TOTAL:
        raise EnvError(f"environment variables exceed {MAX_TOTAL} bytes in total")
    return out


def mask(value: str) -> str:
    return "••••" + value[-4:] if len(value) > 8 else "••••"


class EnvStore:
    def __init__(self, secret_key: str):
        if not secret_key:
            raise RuntimeError("DOCKERSERVER_SECRET_KEY is not set")
        self.f = Fernet(secret_key.encode() if isinstance(secret_key, str) else secret_key)

    def _dec(self, blob: bytes) -> str:
        try:
            return self.f.decrypt(blob).decode()
        except InvalidToken:
            raise RuntimeError("cannot decrypt stored environment variable: wrong DOCKERSERVER_SECRET_KEY?")

    def get_all(self, db: Session, user_id: int) -> dict[str, str]:
        rows = db.scalars(select(UserEnv).where(UserEnv.user_id == user_id).order_by(UserEnv.name))
        return {r.name: self._dec(r.value_enc) for r in rows}

    def listing(self, db: Session, user_id: int, reveal: bool = False) -> list[dict]:
        rows = db.scalars(select(UserEnv).where(UserEnv.user_id == user_id).order_by(UserEnv.name))
        out = []
        for r in rows:
            v = self._dec(r.value_enc)
            out.append({"name": r.name, "value": v if reveal else mask(v), "length": len(v),
                        "updated_at": r.updated_at})
        return out

    def set(self, db: Session, user_id: int, name: str, value: str):
        check_name(name)
        check_value(name, value)
        row = db.scalar(select(UserEnv).where(UserEnv.user_id == user_id, UserEnv.name == name))
        if row is None:
            count = len(db.scalars(select(UserEnv.name).where(UserEnv.user_id == user_id)).all())
            if count >= MAX_VARS:
                raise EnvError(f"at most {MAX_VARS} environment variables")
            row = UserEnv(user_id=user_id, name=name)
            db.add(row)
        row.value_enc = self.f.encrypt(value.encode())
        row.updated_at = utcnow()
        db.flush()
        if sum(len(k) + len(v) for k, v in self.get_all(db, user_id).items()) > MAX_TOTAL:
            db.rollback()
            raise EnvError(f"environment variables exceed {MAX_TOTAL} bytes in total")
        db.commit()

    def delete(self, db: Session, user_id: int, name: str) -> bool:
        n = db.execute(delete(UserEnv).where(UserEnv.user_id == user_id, UserEnv.name == name)).rowcount
        db.commit()
        return bool(n)

    def merged(self, db: Session, user_id: int, job_env: dict | None) -> dict[str, str]:
        """User-global variables, overridden by the per-job ones."""
        env = self.get_all(db, user_id)
        env.update(check_env(job_env))
        return check_env(env)
