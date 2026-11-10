"""Credits.

1 credit-hour = 1 CPU core + 256 MB RAM for 1 hour of wall-clock run time.
A job of `cpus` cores and `mem_mb` MB costs  seconds/3600 * max(cpus, mem_mb/256)
hours, i.e. it is charged for its reserved size, whichever dimension is larger.

Balances are stored as integer micro-hours (uh) to avoid float drift.
"""
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .models import ACTIVE_STATUSES, Job, LedgerEntry, User, utcnow

UH_PER_HOUR = 1_000_000
MEM_UNIT_MB = 256


def size_factor(cpus: float, mem_mb: int) -> float:
    return max(cpus / 1.0, mem_mb / MEM_UNIT_MB)


def cost_uh(cpus: float, mem_mb: int, seconds: float) -> int:
    return round(max(seconds, 0) * size_factor(cpus, mem_mb) * UH_PER_HOUR / 3600)


def hours(uh: int | None) -> float | None:
    return None if uh is None else uh / UH_PER_HOUR


def to_uh(h: float) -> int:
    return round(h * UH_PER_HOUR)


def job_run_seconds(job: Job, now: datetime | None = None) -> float:
    if not job.started_at:
        return 0.0
    end = job.finished_at or now or utcnow()
    return max((end - job.started_at).total_seconds(), 0.0)


def accrued_uh(job: Job, now: datetime | None = None) -> int:
    """Cost so far (running job) or final cost (finished job)."""
    if job.billed and job.cost_uh is not None:
        return job.cost_uh
    return cost_uh(job.cpus, job.mem_mb, job_run_seconds(job, now))


def add_entry(db: Session, user_id: int, delta_uh: int, reason: str, *, job_id=None,
              admin_id=None, note=None) -> LedgerEntry:
    """Atomically change a balance and append a ledger row. Caller commits."""
    db.execute(update(User).where(User.id == user_id)
               .values(balance_uh=User.balance_uh + delta_uh))
    bal = db.scalar(select(User.balance_uh).where(User.id == user_id))
    e = LedgerEntry(user_id=user_id, delta_uh=delta_uh, balance_after_uh=bal, reason=reason,
                    job_id=job_id, admin_id=admin_id, note=note)
    db.add(e)
    return e


def available_uh(db: Session, user_id: int, now: datetime | None = None) -> int:
    """Balance minus what the user's still-running (unbilled) jobs have used so far."""
    bal = db.scalar(select(User.balance_uh).where(User.id == user_id)) or 0
    running = db.scalars(select(Job).where(Job.user_id == user_id, Job.billed.is_(False),
                                           Job.status.in_(ACTIVE_STATUSES))).all()
    return bal - sum(accrued_uh(j, now) for j in running)


def bill_job(db: Session, job_id: int) -> int | None:
    """Charge a finished job exactly once. Returns the charge, or None if already billed."""
    claimed = db.execute(update(Job).where(Job.id == job_id, Job.billed.is_(False))
                         .values(billed=True)).rowcount
    if not claimed:
        db.rollback()
        return None
    job = db.get(Job, job_id)
    db.refresh(job)
    amount = cost_uh(job.cpus, job.mem_mb, job_run_seconds(job)) if job.started_at else 0
    job.cost_uh = amount
    if amount:
        add_entry(db, job.user_id, -amount, "job", job_id=job.id)
    db.commit()
    return amount


def balance_total_check(db: Session, user_id: int) -> bool:
    """Invariant: balance == sum(ledger)."""
    s = db.scalar(select(func.coalesce(func.sum(LedgerEntry.delta_uh), 0))
                  .where(LedgerEntry.user_id == user_id))
    return s == db.scalar(select(User.balance_uh).where(User.id == user_id))


def grant_initial(db: Session, user: User, hours_: float, admin_id=None):
    """Initial credit grant for a new user. Caller commits."""
    if hours_ > 0:
        add_entry(db, user.id, to_uh(hours_), "topup", admin_id=admin_id, note="initial grant")


def grant_missing_initial(db: Session, hours_: float):
    """Users that have never had a ledger entry (e.g. created before billing existed)."""
    has = select(LedgerEntry.user_id).distinct()
    for u in db.scalars(select(User).where(User.id.not_in(has))).all():
        grant_initial(db, u, hours_)
    db.commit()
