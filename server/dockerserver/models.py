"""Database models. Jobs carry everything step 2 (credit billing) needs:
user, cpus, mem_mb, started_at, finished_at."""
from datetime import datetime, timezone

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text,
                        UniqueConstraint, create_engine, inspect, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(16), default="user")  # "admin" | "user"
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    max_cpus: Mapped[float] = mapped_column(Float, default=1.0)
    max_mem_mb: Mapped[int] = mapped_column(Integer, default=256)
    max_run_seconds: Mapped[int] = mapped_column(Integer, default=600)
    max_concurrent_jobs: Mapped[int] = mapped_column(Integer, default=2)
    allow_network: Mapped[bool] = mapped_column(Boolean, default=False)
    # Credit balance in micro-hours (1 hour = 1 CPU core + 256 MB for 1 hour).
    # Always equal to the sum of this user's ledger entries.
    balance_uh: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    tokens: Mapped[list["Token"]] = relationship(back_populates="user")

    @property
    def balance_hours(self) -> float:
        return self.balance_uh / 1_000_000

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class UserEnv(Base):
    """A user's environment variable, injected into all their jobs; value Fernet-encrypted."""
    __tablename__ = "user_env"
    __table_args__ = (UniqueConstraint("user_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    value_enc: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Token(Base):
    __tablename__ = "tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64), default="default")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(16))  # first chars, for identification only
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship(back_populates="tokens")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    image: Mapped[str] = mapped_column(String(256))
    command: Mapped[str] = mapped_column(Text)
    git_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    cpus: Mapped[float] = mapped_column(Float)
    mem_mb: Mapped[int] = mapped_column(Integer)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    network: Mapped[bool] = mapped_column(Boolean, default=False)
    # queued -> preparing -> running ->
    #   succeeded | failed | timeout | stopped | out_of_credits | killed_by_admin | lost | error
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    # names (never values) of the environment variables the job ran with, comma-separated
    env_names: Mapped[str | None] = mapped_column(Text, nullable=True)
    # where it ran: "local" or a compute node name; container_id holds the local container id
    # or the node's job id
    node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    container_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Billing: charged once on finish (billed flag makes it idempotent).
    billed: Mapped[bool] = mapped_column(Boolean, default=False)
    cost_uh: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Usage samples from `docker stats` (CPU % is per core: 100 = one full core).
    stat_samples: Mapped[int] = mapped_column(Integer, default=0)
    cpu_avg_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    cpu_peak_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_avg_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_peak_mb: Mapped[float | None] = mapped_column(Float, nullable=True)

    @property
    def cost_hours(self) -> float | None:
        return None if self.cost_uh is None else self.cost_uh / 1_000_000

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None


class LedgerEntry(Base):
    """Every balance change. reason: topup | adjust | job."""
    __tablename__ = "ledger"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    delta_uh: Mapped[int] = mapped_column(Integer)
    balance_after_uh: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(16))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, unique=True)
    admin_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ---------------- OAuth (MCP connectors in claude.ai / Claude Desktop) ----------------
class OAuthClient(Base):
    """Dynamically registered OAuth client (RFC 7591)."""
    __tablename__ = "oauth_clients"
    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    info_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OAuthPending(Base):
    """An /authorize request waiting for the user to approve it on the consent page."""
    __tablename__ = "oauth_pending"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)   # sha256 of the request id
    client_id: Mapped[str] = mapped_column(String(64))
    params_json: Mapped[str] = mapped_column(Text)
    csrf: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[float] = mapped_column(Float)


class OAuthCode(Base):
    """Authorization code (single use, short-lived); stored hashed."""
    __tablename__ = "oauth_codes"
    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    ds_token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"))
    params_json: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[float] = mapped_column(Float)
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class OAuthToken(Base):
    """OAuth access / refresh token, stored hashed, bound to a user, the ds_ token the user
    approved with, one client and one resource (/mcp or /mcp-admin)."""
    __tablename__ = "oauth_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(8))          # "access" | "refresh"
    family: Mapped[str] = mapped_column(String(32), index=True)   # one approval = one family
    client_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ds_token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id"), index=True)
    resource: Mapped[str] = mapped_column(String(512))
    scopes: Mapped[str] = mapped_column(String(256), default="")
    expires_at: Mapped[float] = mapped_column(Float)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


TERMINAL_STATUSES = {"succeeded", "failed", "timeout", "stopped", "error", "out_of_credits",
                     "killed_by_admin", "lost"}
ACTIVE_STATUSES = {"queued", "preparing", "running"}


def _auto_migrate(engine):
    """Tiny SQLite migration: add columns that exist in the models but not in the DB."""
    insp = inspect(engine)
    added = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = col.type.compile(engine.dialect)
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                if default is None:
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}'))
                else:
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl} '
                                      f"NOT NULL DEFAULT {int(default) if isinstance(default, bool) else default!r}"))
                added.append(f"{table.name}.{col.name}")
    return added


def make_session_factory(db_url: str):
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    _auto_migrate(engine)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
