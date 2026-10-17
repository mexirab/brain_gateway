"""Dead-man's-switch heartbeats that survive a restart.

A scheduled job stamps a ``*_last_run_timestamp_seconds`` gauge when it fires,
and a ``*Stale`` alert fires when the stamp gets too old. A gauge alone lives
in RAM: after every deploy it reads 0, and the alerts ignore 0 (so they can't
false-page before the first run) — which left them blind until the job next
ran, and a job that died right after a deploy never paged at all.

``stamp()`` also writes the time to the ``app_state`` table; ``seed()`` loads it
back at startup, so the gauge shows the real last run (re-seeding now() on
every boot hid dead jobs during busy deploy weeks). The first time a job is
enabled there is no stamp yet, so ``seed()`` records the registration time
once — a job that never fires still ages and pages. ``forget()`` drops the
stamp of a job that isn't scheduled, so turning it back on after a long break
starts fresh instead of paging on a days-old stamp.
"""

import logging
import time

from prometheus_client import Gauge

from orchestrator import state_store

logger = logging.getLogger(__name__)


def stamp(gauge: Gauge, key: str) -> None:
    """Record that a job fired now. A failed DB write never breaks the job."""
    now = time.time()
    gauge.set(now)
    try:
        state_store.set_app_state(key, str(now))
    except Exception as e:
        logger.warning("[HEARTBEAT] Could not persist %s: %s", key, type(e).__name__)


def seed(gauge: Gauge, key: str) -> None:
    """Startup, for a registered job: show the persisted last run; with none
    yet, record now() once as the baseline."""
    try:
        raw = state_store.get_app_state(key)
        if raw:
            gauge.set(float(raw))
            return
    except Exception as e:
        logger.warning("[HEARTBEAT] Could not seed %s: %s", key, type(e).__name__)
        return
    stamp(gauge, key)


def forget(key: str) -> None:
    """Startup, for a job that isn't scheduled: drop its stamp."""
    try:
        state_store.delete_app_state(key)
    except Exception as e:
        logger.warning("[HEARTBEAT] Could not clear %s: %s", key, type(e).__name__)
