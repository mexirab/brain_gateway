"""
Tests for the scheduler-wide job_defaults on orchestrator/shared.py's
AsyncIOScheduler.

APScheduler 3.x ships a 1-second default misfire_grace_time: any job whose
fire time passes while the event loop is stalled for more than 1s is silently
dropped, and one-shot date jobs (reminders, focus break delivery, DND
auto-unmute) have no next occurrence to recover on. shared.py therefore
constructs the scheduler with
``job_defaults={"misfire_grace_time": 300, "coalesce": True}``
(prod-support M1, 2026-07-06).

These tests pin that configuration and its inheritance behavior so a revert
(e.g. dropping job_defaults during a refactor) cannot land silently — jobs
added without an explicit misfire_grace_time would quietly fall back into the
1s drop class.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta


async def _noop():
    pass


@asynccontextmanager
async def _paused_scheduler():
    """Start the shared scheduler paused (defaults get applied on add_job
    only once the scheduler is out of STATE_STOPPED — and AsyncIOScheduler
    3.11 requires a *running* event loop to start, hence async), and restore
    it to its import-time state — stopped, no jobs — afterwards."""
    from apscheduler.schedulers.base import STATE_STOPPED

    from orchestrator import shared

    started_here = False
    if shared.scheduler.state == STATE_STOPPED:
        shared.scheduler.start(paused=True)
        started_here = True
    try:
        yield shared.scheduler
    finally:
        shared.scheduler.remove_all_jobs()
        if started_here:
            shared.scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Behavior: jobs inherit the defaults
# ---------------------------------------------------------------------------


async def test_job_without_explicit_grace_inherits_300():
    """The regression guard: a job added the way orchestrator.py adds the
    wind-down rungs — no per-job misfire_grace_time — must inherit
    300/coalesce from shared.py's job_defaults. If someone removes
    job_defaults, this reverts to APScheduler's 1s drop-happy default."""
    async with _paused_scheduler() as scheduler:
        job = scheduler.add_job(
            _noop,
            trigger="date",
            run_date=datetime.now() + timedelta(days=365),
            id="_test_defaults_inherit",
            name="test: defaults inheritance",
            replace_existing=True,
        )

        assert job.misfire_grace_time == 300
        assert job.coalesce is True


async def test_explicit_per_job_grace_still_wins():
    """job_defaults only fill in unspecified values — a job that opts into
    its own grace window keeps it."""
    async with _paused_scheduler() as scheduler:
        job = scheduler.add_job(
            _noop,
            trigger="date",
            run_date=datetime.now() + timedelta(days=365),
            id="_test_defaults_override",
            name="test: per-job override",
            replace_existing=True,
            misfire_grace_time=10,
        )

        assert job.misfire_grace_time == 10
        assert job.coalesce is True
