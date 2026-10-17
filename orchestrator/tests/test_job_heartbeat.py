"""Tests for orchestrator.job_heartbeat — persisted dead-man's-switch gauges."""

import logging
import time
from unittest.mock import patch

import pytest
from prometheus_client import CollectorRegistry, Gauge

KEY = "heartbeat_test_job_ts"


@pytest.fixture
def gauge():
    """A fresh gauge on a private registry, so tests never touch real metrics."""
    return Gauge("bgw_test_heartbeat_ts", "test heartbeat", registry=CollectorRegistry())


def _val(g):
    return g._value.get()


def test_stamp_sets_gauge_and_persists(tmp_db, gauge):
    from orchestrator import job_heartbeat, state_store

    before = time.time()
    job_heartbeat.stamp(gauge, KEY)
    after = time.time()

    assert before <= _val(gauge) <= after
    assert float(state_store.get_app_state(KEY)) == pytest.approx(_val(gauge))


def test_stamp_swallows_db_failure(tmp_db, gauge, caplog):
    from orchestrator import job_heartbeat

    with (
        patch("orchestrator.state_store.set_app_state", side_effect=RuntimeError("db locked")),
        caplog.at_level(logging.WARNING, logger="orchestrator.job_heartbeat"),
    ):
        job_heartbeat.stamp(gauge, KEY)  # must not raise

    assert _val(gauge) == pytest.approx(time.time(), abs=5)
    assert any("Could not persist" in r.getMessage() for r in caplog.records)
    # Only the exception type is logged, never its message
    assert not any("db locked" in r.getMessage() for r in caplog.records)


def test_seed_uses_persisted_value_and_does_not_overwrite(tmp_db, gauge):
    from orchestrator import job_heartbeat, state_store

    state_store.set_app_state(KEY, "1700000000.5")
    job_heartbeat.seed(gauge, KEY)

    assert _val(gauge) == 1700000000.5
    assert state_store.get_app_state(KEY) == "1700000000.5"


def test_seed_without_value_stamps_now_once(tmp_db, gauge):
    from orchestrator import job_heartbeat, state_store

    assert state_store.get_app_state(KEY) is None
    job_heartbeat.seed(gauge, KEY)

    stored = state_store.get_app_state(KEY)
    assert stored is not None
    assert float(stored) == pytest.approx(time.time(), abs=5)
    assert _val(gauge) == pytest.approx(float(stored))

    # A second seed (next restart) must keep the baseline, not re-stamp now().
    job_heartbeat.seed(gauge, KEY)
    assert state_store.get_app_state(KEY) == stored


def test_seed_db_error_logs_and_leaves_gauge(tmp_db, gauge, caplog):
    from orchestrator import job_heartbeat

    gauge.set(42.0)
    with (
        patch("orchestrator.state_store.get_app_state", side_effect=RuntimeError("boom")),
        patch("orchestrator.state_store.set_app_state") as mock_set,
        caplog.at_level(logging.WARNING, logger="orchestrator.job_heartbeat"),
    ):
        job_heartbeat.seed(gauge, KEY)

    assert _val(gauge) == 42.0
    mock_set.assert_not_called()  # a read failure must not stamp now()
    assert any("Could not seed" in r.getMessage() for r in caplog.records)


def test_seed_corrupt_value_does_not_raise(tmp_db, gauge):
    """A non-numeric stored value hits the float() inside the try."""
    from orchestrator import job_heartbeat, state_store

    gauge.set(7.0)
    state_store.set_app_state(KEY, "not-a-number")
    job_heartbeat.seed(gauge, KEY)  # must not raise into startup

    assert _val(gauge) == 7.0


def test_forget_deletes_key(tmp_db, gauge):
    from orchestrator import job_heartbeat, state_store

    state_store.set_app_state(KEY, "123.0")
    job_heartbeat.forget(KEY)
    assert state_store.get_app_state(KEY) is None
    job_heartbeat.forget(KEY)  # no-op when already absent


def test_forget_swallows_errors(tmp_db, caplog):
    from orchestrator import job_heartbeat

    with (
        patch("orchestrator.state_store.delete_app_state", side_effect=RuntimeError("boom")),
        caplog.at_level(logging.WARNING, logger="orchestrator.job_heartbeat"),
    ):
        job_heartbeat.forget(KEY)

    assert any("Could not clear" in r.getMessage() for r in caplog.records)
