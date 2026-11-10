"""Pydantic schemas and serializers shared by the REST API and the MCP server."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .billing import UH_PER_HOUR, accrued_uh, hours, job_run_seconds
from .models import Job, LedgerEntry


class Limits(BaseModel):
    max_cpus: float | None = Field(None, gt=0, le=64)
    max_mem_mb: int | None = Field(None, ge=32, le=262144)
    max_run_seconds: int | None = Field(None, ge=1, le=7 * 24 * 3600)
    max_concurrent_jobs: int | None = Field(None, ge=1, le=100)
    allow_network: bool | None = None


class UserCreate(Limits):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    role: Literal["admin", "user"] = "user"


class UserUpdate(Limits):
    role: Literal["admin", "user"] | None = None
    active: bool | None = None


class UserOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    name: str
    role: str
    active: bool
    max_cpus: float
    max_mem_mb: int
    max_run_seconds: int
    max_concurrent_jobs: int
    allow_network: bool
    balance_hours: float
    created_at: datetime


class TokenCreate(BaseModel):
    name: str = Field("default", max_length=64)


class TokenOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    user_id: int
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class TokenIssued(TokenOut):
    token: str


class UserCreated(UserOut):
    token: str


class JobOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    user_id: int
    image: str
    command: str
    git_url: str | None
    cpus: float
    mem_mb: int
    timeout_seconds: int
    network: bool
    status: str
    exit_code: int | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: float | None
    billed: bool
    cost_hours: float | None
    stat_samples: int
    cpu_avg_pct: float | None
    cpu_peak_pct: float | None
    mem_avg_mb: float | None
    mem_peak_mb: float | None


class CreditChange(BaseModel):
    hours: float = Field(description="credit-hours to add (negative to deduct)")
    reason: Literal["topup", "adjust"] = "topup"
    note: str | None = Field(None, max_length=500)


class LedgerOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    user_id: int
    delta_hours: float
    balance_after_hours: float
    reason: str
    job_id: int | None
    admin_id: int | None
    note: str | None
    created_at: datetime


def ledger_out(e: LedgerEntry) -> dict:
    return {"id": e.id, "user_id": e.user_id, "delta_hours": hours(e.delta_uh),
            "balance_after_hours": hours(e.balance_after_uh), "reason": e.reason,
            "job_id": e.job_id, "admin_id": e.admin_id, "note": e.note, "created_at": e.created_at}


def usage_totals(jobs: list[Job]) -> dict:
    """cpu_hours = cores x hours, mem_gb_hours = GB x hours, cost_hours = credits charged/accrued."""
    t = {"jobs": 0, "run_seconds": 0.0, "cpu_hours": 0.0, "mem_gb_hours": 0.0, "cost_hours": 0.0}
    for j in jobs:
        sec = job_run_seconds(j)
        t["jobs"] += 1
        t["run_seconds"] += sec
        t["cpu_hours"] += j.cpus * sec / 3600
        t["mem_gb_hours"] += j.mem_mb / 1024 * sec / 3600
        t["cost_hours"] += accrued_uh(j) / UH_PER_HOUR
    return {k: round(v, 6) if isinstance(v, float) else v for k, v in t.items()}
