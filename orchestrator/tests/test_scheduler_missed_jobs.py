"""
Tests for the EVENT_JOB_MISSED observability path.

APScheduler silently drops any job whose misfire_grace_time (300s scheduler-wide,
see test_scheduler_defaults.py) lapses during an event-loop stall. One-shot
date-trigger jobs (reminder delivery, focus break delivery, dnd_auto_unmute)
have no next occurrence and no runtime recovery, so a drop is a permanently lost
action. The EVENT_JOB_MISSED listener wired in orchestrator.py startup turns each
drop into an ERROR log + a bgw_scheduler_jobs_missed_total{job_family} bump.

These tests pin two things a silent refactor could break:
  1. scheduler_job_family() collapses UUID/timestamp job ids to a bounded family
     label (the Prometheus-cardinality guard), and maps fixed-string ids to
     cron:<id>.
  2. _on_job_missed() increments the counter under the right family label and
     survives a missing/None scheduled_run_time.

Runs inside the brain-orchestrator container (full deps available). Skips
gracefully when orchestrator dependencies are unavailable locally.
"""

from datetime import datetime

import pytest


def _can_import():
    try:
        from orchestrator import metrics  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


_skip_no_deps = pytest.mark.skipif(
    not _can_import(),
    reason="orchestrator.metrics requires prometheus_client (container-only)",
)


class _FakeEvent:
    """Stand-in for apscheduler.events.JobExecutionEvent — _on_job_missed only
    reads .job_id and .scheduled_run_time via getattr."""

    def __init__(self, job_id, scheduled_run_time):
        self.job_id = job_id
        self.scheduled_run_time = scheduled_run_time


def _missed_count(family):
    from orchestrator.metrics import SCHEDULER_JOBS_MISSED

    return SCHEDULER_JOBS_MISSED.labels(job_family=family)._value.get()


# ---------------------------------------------------------------------------
# scheduler_job_family(): the cardinality guard
# ---------------------------------------------------------------------------


@_skip_no_deps
@pytest.mark.parametrize(
    "job_id,expected",
    [
        # Dynamic ids (UUID/timestamp bearing) collapse to their family.
        ("reminder_3f9a-uuid-1234", "reminder"),
        ("reminder_42_retry", "reminder"),
        ("focus_143502", "focus"),
        ("focus_checkin_143502", "focus"),
        ("focus_restored_143502", "focus"),
        ("routine_evening", "routine"),
        ("routine_nudge_2200", "routine"),
        ("auto_learn_sess-abc", "auto_learn"),
        ("interrupt_checkin_120000", "interrupt"),
        ("ambient_summary_xyz", "ambient"),
        # Fixed-string cron ids fall through to the bounded cron:<id> series.
        ("calendar_poll", "cron:calendar_poll"),
        ("morning_briefing", "cron:morning_briefing"),
        ("evening_briefing", "cron:evening_briefing"),
        ("dnd_auto_unmute", "cron:dnd_auto_unmute"),
        ("wind_down_dim", "cron:wind_down_dim"),
    ],
)
def test_job_family_classification(job_id, expected):
    from orchestrator.metrics import scheduler_job_family

    assert scheduler_job_family(job_id) == expected


@_skip_no_deps
def test_user_influenced_id_cannot_reach_unbounded_label():
    """The whole point of the family collapse: a reminder id built from
    user-influenced content still maps to the fixed 'reminder' family, never to
    a per-value cron:<id> series that would explode cardinality."""
    from orchestrator.metrics import scheduler_job_family

    assert scheduler_job_family("reminder_" + "x" * 200) == "reminder"


# ---------------------------------------------------------------------------
# _on_job_missed(): the listener body
# ---------------------------------------------------------------------------


@_skip_no_deps
def test_on_job_missed_increments_family_counter():
    from orchestrator.orchestrator import _on_job_missed

    before = _missed_count("reminder")
    _on_job_missed(_FakeEvent("reminder_abc-uuid", datetime(2026, 7, 7, 9, 30)))
    assert _missed_count("reminder") == before + 1


@_skip_no_deps
def test_on_job_missed_labels_by_family_not_raw_id():
    """Two distinct dynamic reminder ids must land on the SAME series."""
    from orchestrator.orchestrator import _on_job_missed

    before = _missed_count("reminder")
    _on_job_missed(_FakeEvent("reminder_one", datetime(2026, 7, 7, 9, 30)))
    _on_job_missed(_FakeEvent("reminder_two_retry", datetime(2026, 7, 7, 9, 31)))
    assert _missed_count("reminder") == before + 2


@_skip_no_deps
def test_on_job_missed_survives_missing_scheduled_time():
    """A malformed event with no scheduled_run_time must not raise (isoformat
    guard) — the counter still increments."""
    from orchestrator.orchestrator import _on_job_missed

    before = _missed_count("cron:calendar_poll")
    _on_job_missed(_FakeEvent("calendar_poll", None))
    assert _missed_count("cron:calendar_poll") == before + 1
