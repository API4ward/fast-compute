"""Access tokens: random, shown once, stored as SHA-256 hashes."""
import hashlib
import secrets
import threading
import time
from collections import deque

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Token, User, utcnow

TOKEN_PREFIX = "ds_"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_token(db: Session, user: User, name: str = "default") -> tuple[Token, str]:
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    tok = Token(user_id=user.id, name=name, token_hash=hash_token(raw), prefix=raw[:10])
    db.add(tok)
    db.commit()
    return tok, raw


def lookup_user(db: Session, raw: str) -> User | None:
    tok = db.scalar(select(Token).where(Token.token_hash == hash_token(raw)))
    if tok is None or tok.revoked_at is not None or not tok.user.active:
        return None
    tok.last_used_at = utcnow()
    db.commit()
    return tok.user


class AuthFailureLimiter:
    """In-memory sliding window of failed authentications per client IP."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._fails: dict[str, deque] = {}
        self._lock = threading.Lock()

    def _prune(self, key, now):
        q = self._fails.get(key)
        while q and q[0] <= now - self.window:
            q.popleft()
        if q is not None and not q:
            del self._fails[key]
        return q

    def blocked(self, key: str) -> bool:
        if self.limit <= 0:
            return False
        with self._lock:
            q = self._prune(key, time.monotonic())
            return bool(q) and len(q) >= self.limit

    def record_failure(self, key: str):
        now = time.monotonic()
        with self._lock:
            self._prune(key, now)
            self._fails.setdefault(key, deque()).append(now)
            if len(self._fails) > 10000:  # bound memory: drop the oldest-seen keys
                for k in list(self._fails)[:1000]:
                    del self._fails[k]
