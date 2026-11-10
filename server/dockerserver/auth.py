"""Access tokens: random, shown once, stored as SHA-256 hashes."""
import hashlib
import secrets

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
