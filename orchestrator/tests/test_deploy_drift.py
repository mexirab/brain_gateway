"""Tests for the deploy drift check (orchestrator/jobs_deploy_drift.py).

The check reads the CI deploy job's ``.last-deployed-sha`` marker and asks the
GitHub REST API which newer pushes to main passed CI. Everything here runs
against a fake GitHub (respx routes every request to api.github.com to
``FakeGitHub.handler``; any other host is refused), a marker in ``tmp_path`` and
the real ``_client()`` (headers, no redirects).

Covers: marker parsing + the info/timestamp gauges, in_sync / behind /
not_on_main verdicts, dedup by commit, the per-attempt verdict cache, the job
lookup budget (``partial``), ``deploy_job_missing``, every failure path
(``github_error`` / ``rate_limited``), gauge retention across failures, log
hygiene (no token, warn-once, recovery), the scheduler wrapper's one-counter
contract, the DEPLOY_DRIFT_REPO validator, and the coupling with
``.github/workflows/ci.yml`` and the Prometheus / Alertmanager config.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx
import yaml

REPO = "example-owner/example-repo"
RUNS_PATH = f"/repos/{REPO}/actions/workflows/ci.yml/runs"
_JOBS_RE = re.compile(rf"/repos/{re.escape(REPO)}/actions/runs/(\d+)/jobs")
_COMPARE_RE = re.compile(rf"/repos/{re.escape(REPO)}/compare/([0-9a-f]{{40}})\.\.\.([0-9a-f]{{40}})")

REPO_ROOT = Path(__file__).resolve().parents[2]

T0 = 1_792_000_000.0  # a fixed instant in Oct 2026; every run/job time is relative to it
T_MARK = T0 - 3600.0  # marker mtime

# The real CI job names (deploy job excluded), as GitHub's jobs API reports them.
CI_JOB_NAMES = [
    "Python Lint & Format",
    "Grafana Dashboards In Sync",
    "Suite Theme In Sync",
    "Frontend Lint & Type Check",
    "Python Tests",
    "Docker Build (orchestrator)",
]
# Completion offsets of the CI jobs relative to the run's "passed at" time. The
# latest is NOT the last job in the list, so a max() over the jobs is tested,
# not "the last job's time".
_CI_OFFSETS = [-300, -240, -200, -100, 0, -50]

ALL_RESULTS = {
    "in_sync",
    "behind",
    "not_on_main",
    "partial",
    "marker_missing",
    "marker_invalid",
    "deploy_job_missing",
    "github_error",
    "rate_limited",
}
FAILURE_RESULTS = {"marker_missing", "marker_invalid", "deploy_job_missing", "github_error", "rate_limited"}
OK_RESULTS = ALL_RESULTS - FAILURE_RESULTS


# ---------------------------------------------------------------------------
# Data builders
# ---------------------------------------------------------------------------


def _sha(tag: object) -> str:
    return hashlib.sha1(f"deploy-drift-test-{tag}".encode()).hexdigest()


D = _sha("deployed")
G = _sha("green")
G2 = _sha("green-2")
B = _sha("new-marker")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rid(number: int) -> int:
    return 90_000 + number


def _run(
    number: int,
    sha: str,
    *,
    run_id: int | None = None,
    attempt: int = 1,
    status: str = "completed",
    conclusion: str | None = "failure",
    updated: float | None = None,
    branch: str = "main",
    event: str = "push",
) -> dict:
    return {
        "id": _rid(number) if run_id is None else run_id,
        "name": "CI",
        "run_number": number,
        "run_attempt": attempt,
        "head_sha": sha,
        "head_branch": branch,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "updated_at": _iso(T0 + number * 600 if updated is None else updated),
    }


def _jobs(
    *,
    passed_at: float = T0,
    deploy: tuple[str, str | None] | None = ("completed", "failure"),
    deploy_name: str | None = None,
    overrides: dict[str, tuple[str, str | None]] | None = None,
) -> list[dict]:
    """Jobs of one run attempt. CI jobs succeed (latest one completing at
    ``passed_at``) unless ``overrides`` says otherwise; ``deploy=None`` leaves
    the deploy job out of the list."""
    jobs = []
    for name, offset in zip(CI_JOB_NAMES, _CI_OFFSETS, strict=True):
        status, conclusion = (overrides or {}).get(name, ("completed", "success"))
        done = status == "completed"
        jobs.append(
            {
                "name": name,
                "status": status,
                "conclusion": conclusion,
                "completed_at": _iso(passed_at + offset) if done else None,
            }
        )
    if deploy is not None:
        from orchestrator.jobs_deploy_drift import DEPLOY_JOB_NAME

        status, conclusion = deploy
        jobs.append(
            {
                "name": deploy_name or DEPLOY_JOB_NAME,
                "status": status,
                "conclusion": conclusion,
                # Later than every CI job: must never be used as the pass time.
                "completed_at": _iso(passed_at + 999) if status == "completed" else None,
            }
        )
    return jobs


def _red_jobs() -> list[dict]:
    """A finished run whose tests failed: the deploy job was skipped."""
    return _jobs(deploy=("completed", "skipped"), overrides={"Python Tests": ("completed", "failure")})


def _running_jobs() -> list[dict]:
    """A run whose CI is still going: the deploy job isn't listed yet."""
    return _jobs(deploy=None, overrides={"Python Tests": ("in_progress", None)})


# ---------------------------------------------------------------------------
# Fake GitHub
# ---------------------------------------------------------------------------


@dataclass
class Resp:
    """A canned HTTP answer, built fresh on every request."""

    status: int = 200
    body: object = None
    headers: dict = field(default_factory=dict)
    content: bytes | None = None

    def build(self) -> httpx.Response:
        if self.content is not None:
            return httpx.Response(self.status, content=self.content, headers=self.headers)
        return httpx.Response(self.status, json=self.body, headers=self.headers)


def _raise(exc_type):
    def answer(request: httpx.Request):
        raise exc_type("simulated network failure", request=request)

    return answer


class FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.reset()

    def reset(self) -> None:
        self.runs: list[dict] = []
        self.runs_spec: object = None  # overrides the runs answer when set
        self.jobs: dict[int, object] = {}  # run id -> jobs list | spec
        self.compare: dict[tuple[str, str], object] = {}  # (base, head) -> body | spec

    @staticmethod
    def _answer(spec: object, request: httpx.Request) -> httpx.Response:
        if isinstance(spec, Resp):
            return spec.build()
        if callable(spec):
            return spec(request)
        return httpx.Response(200, json=spec)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if path == RUNS_PATH:
            spec = self.runs_spec
            if spec is None:
                spec = {"total_count": len(self.runs), "workflow_runs": self.runs}
            return self._answer(spec, request)
        m = _JOBS_RE.fullmatch(path)
        if m and int(m.group(1)) in self.jobs:
            spec = self.jobs[int(m.group(1))]
            if isinstance(spec, list):
                spec = {"total_count": len(spec), "jobs": spec}
            return self._answer(spec, request)
        m = _COMPARE_RE.fullmatch(path)
        if m and (m.group(1), m.group(2)) in self.compare:
            return self._answer(self.compare[(m.group(1), m.group(2))], request)
        return httpx.Response(404, json={"message": "Not Found"})

    # -- request inspection --------------------------------------------------
    def paths(self) -> list[str]:
        return [r.url.path for r in self.calls]

    def jobs_calls(self) -> list[int]:
        return [int(m.group(1)) for p in self.paths() if (m := _JOBS_RE.fullmatch(p))]

    def compare_calls(self) -> list[tuple[str, str]]:
        return [(m.group(1), m.group(2)) for p in self.paths() if (m := _COMPARE_RE.fullmatch(p))]


class Marker:
    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, sha: str, mtime: float = T_MARK, *, raw: str | None = None) -> None:
        self.path.write_text(raw if raw is not None else sha + "\n")
        os.utime(self.path, (mtime, mtime))


# ---------------------------------------------------------------------------
# Fixtures + metric readers
# ---------------------------------------------------------------------------


def _reset_metrics(mod) -> None:
    mod.DEPLOYED_COMMIT_INFO.clear()
    for gauge in (
        mod.DEPLOYED_COMMIT_TIMESTAMP,
        mod.DEPLOY_UNDEPLOYED_GREEN_COMMITS,
        mod.DEPLOY_UNDEPLOYED_GREEN_SINCE,
    ):
        gauge.set(float("nan"))


@pytest.fixture
def drift(monkeypatch, tmp_path):
    """The module under test, configured for REPO with a marker in tmp_path,
    fresh module state and NaN gauges (as at process start)."""
    from orchestrator import jobs_deploy_drift as mod
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "deploy_drift_repo", REPO, raising=False)
    monkeypatch.setattr(settings, "deploy_marker_path", str(tmp_path / ".last-deployed-sha"), raising=False)
    monkeypatch.setattr(settings, "deploy_drift_github_token", "", raising=False)
    monkeypatch.setattr(mod, "_state", mod._State())
    _reset_metrics(mod)
    yield mod
    _reset_metrics(mod)


@pytest.fixture
def gh():
    fake = FakeGitHub()
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        router.route(host="api.github.com").mock(side_effect=fake.handler)
        yield fake


@pytest.fixture
def marker(tmp_path):
    return Marker(tmp_path / ".last-deployed-sha")


@pytest.fixture
def logs(caplog, drift):
    """The module's log records as (level, message), DEBUG and up."""
    caplog.set_level(logging.DEBUG, logger=drift.logger.name)

    def records() -> list[tuple[int, str]]:
        return [(r.levelno, r.getMessage()) for r in caplog.records if r.name == drift.logger.name]

    return records


def _count(mod) -> float:
    return mod.DEPLOY_UNDEPLOYED_GREEN_COMMITS._value.get()


def _since(mod) -> float:
    return mod.DEPLOY_UNDEPLOYED_GREEN_SINCE._value.get()


def _stamp(mod) -> float:
    return mod.DEPLOYED_COMMIT_TIMESTAMP._value.get()


def _info(mod) -> list[str]:
    return sorted(s.labels["commit"] for m in mod.DEPLOYED_COMMIT_INFO.collect() for s in m.samples)


def _drift_is_nan(mod) -> bool:
    return math.isnan(_count(mod)) and math.isnan(_since(mod))


def _counters(mod) -> dict[str, float]:
    out = {}
    for metric in mod.DEPLOY_DRIFT_CHECK_TOTAL.collect():
        for s in metric.samples:
            if s.name.endswith("_total"):
                out[s.labels["result"]] = s.value
    return out


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


def _behind_setup(gh: FakeGitHub, *, deployed: str = D, passed_at: float = T0 + 7000) -> None:
    """Deployed run 10; run 11 passed CI and its deploy was cancelled (a newer
    push superseded it in the deploy concurrency group)."""
    gh.runs = [_run(11, G, conclusion="cancelled"), _run(10, deployed, conclusion="success")]
    gh.jobs[_rid(11)] = _jobs(passed_at=passed_at, deploy=("completed", "cancelled"))


# ===========================================================================
# 1. Marker
# ===========================================================================


class TestMarker:
    async def test_valid_marker_publishes_short_commit_and_mtime(self, drift, gh, marker):
        marker.write(D, mtime=T_MARK)
        gh.runs = [_run(10, D, conclusion="success")]

        assert await drift._check() == "in_sync"
        assert _info(drift) == [D[:12]]
        assert len(_info(drift)[0]) == 12
        assert _stamp(drift) == T_MARK

    async def test_missing_marker_clears_everything_without_asking_github(self, drift, gh, marker):
        marker.write(D)
        gh.runs = [_run(10, D, conclusion="success")]
        assert await drift._check() == "in_sync"
        assert _count(drift) == 0
        calls_before = len(gh.calls)

        marker.path.unlink()
        assert await drift._check() == "marker_missing"
        assert _info(drift) == []
        assert math.isnan(_stamp(drift))
        assert _drift_is_nan(drift)
        assert len(gh.calls) == calls_before

    async def test_invalid_marker_clears_the_info_series(self, drift, gh, marker):
        marker.write(D)
        gh.runs = [_run(10, D, conclusion="success")]
        await drift._check()

        marker.write("", raw="deploy failed\n")
        assert await drift._check() == "marker_invalid"
        assert _info(drift) == []
        assert math.isnan(_stamp(drift))
        assert _drift_is_nan(drift)

    def test_directory_is_marker_missing(self, drift, tmp_path):
        assert drift.read_marker(str(tmp_path)) == "marker_missing"

    def test_absent_path_is_marker_missing(self, drift, tmp_path):
        assert drift.read_marker(str(tmp_path / "nope")) == "marker_missing"

    def test_fifo_does_not_block(self, drift, tmp_path):
        """A FIFO in the marker's place must not hang the event loop."""
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)
        out: list[object] = []
        t = threading.Thread(target=lambda: out.append(drift.read_marker(str(fifo))), daemon=True)
        t.start()
        t.join(timeout=5)
        assert not t.is_alive(), "read_marker blocked on a FIFO"
        assert out == ["marker_missing"]

    @pytest.mark.parametrize(
        "raw",
        ["", "\n", "not-a-sha", D[:39], D + "a", "g" * 40, D[:20] + " " + D[20:], "sha=" + D],
        ids=["empty", "newline", "word", "39hex", "41hex", "non_hex", "inner_space", "prefixed"],
    )
    def test_invalid_content_is_marker_invalid(self, drift, marker, raw):
        marker.write("", raw=raw)
        assert drift.read_marker(str(marker.path)) == "marker_invalid"

    def test_uppercase_and_whitespace_are_normalised(self, drift, marker):
        marker.write("", raw=f"  \t{D.upper()}\r\n\n", mtime=T_MARK)
        assert drift.read_marker(str(marker.path)) == (D, T_MARK)

    async def test_new_marker_replaces_the_info_series(self, drift, gh, marker):
        gh.runs = [_run(11, B, conclusion="success"), _run(10, D, conclusion="success")]
        marker.write(D)
        await drift._check()
        assert _info(drift) == [D[:12]]

        marker.write(B, mtime=T_MARK + 600)
        await drift._check()
        assert _info(drift) == [B[:12]]
        assert _stamp(drift) == T_MARK + 600

    async def test_redeploy_of_same_commit_updates_timestamp_only(self, drift, gh, marker):
        gh.runs = [_run(10, D, conclusion="success")]
        marker.write(D, mtime=T_MARK)
        await drift._check()
        marker.write(D, mtime=T_MARK + 120)
        await drift._check()
        assert _info(drift) == [D[:12]]
        assert _stamp(drift) == T_MARK + 120


# ===========================================================================
# 2./3. in_sync and behind
# ===========================================================================


class TestVerdicts:
    async def test_in_sync_costs_one_request(self, drift, gh, marker):
        marker.write(D)
        gh.runs = [
            _run(12, D, conclusion="success"),
            _run(11, G, conclusion="failure"),
            _run(10, G2, conclusion="success"),
        ]

        assert await drift._check() == "in_sync"
        assert (_count(drift), _since(drift)) == (0, 0)
        assert gh.paths() == [RUNS_PATH]
        assert dict(gh.calls[0].url.params) == {
            "branch": "main",
            "event": "push",
            "per_page": "20",
            "exclude_pull_requests": "true",
        }

    @pytest.mark.parametrize(
        "deploy",
        [("completed", "failure"), ("completed", "cancelled"), ("pending", None), ("in_progress", None)],
        ids=["deploy_failed", "deploy_cancelled", "deploy_pending", "deploy_running"],
    )
    async def test_ci_green_but_not_deployed_is_behind(self, drift, gh, marker, deploy):
        """Run 551's shape: conclusion failure, every CI job green, only the
        deploy job failed. The pass time is the latest CI job's completion, not
        the deploy job's nor the run's updated_at."""
        marker.write(D)
        passed = T0 + 7000
        status = "completed" if deploy[0] == "completed" else "in_progress"
        conclusion = deploy[1] if status == "completed" else None
        gh.runs = [
            _run(11, G, status=status, conclusion=conclusion, updated=passed + 1200),
            _run(10, D, conclusion="success"),
        ]
        gh.jobs[_rid(11)] = _jobs(passed_at=passed, deploy=deploy)

        assert await drift._check() == "behind"
        assert _count(drift) == 1
        assert _since(drift) == passed

    async def test_failed_tests_are_not_green(self, drift, gh, marker):
        marker.write(D)
        gh.runs = [_run(11, G), _run(10, D, conclusion="success")]
        gh.jobs[_rid(11)] = _red_jobs()

        assert await drift._check() == "in_sync"
        assert (_count(drift), _since(drift)) == (0, 0)

    async def test_running_ci_is_not_green_and_not_cached(self, drift, gh, marker):
        marker.write(D)
        gh.runs = [_run(11, G, status="in_progress", conclusion=None), _run(10, D, conclusion="success")]
        gh.jobs[_rid(11)] = _running_jobs()

        assert await drift._check() == "in_sync"
        assert await drift._check() == "in_sync"
        assert gh.jobs_calls() == [_rid(11), _rid(11)]

        # CI finishes green while the deploy job waits for the runner.
        gh.jobs[_rid(11)] = _jobs(passed_at=T0 + 9000, deploy=("queued", None))
        assert await drift._check() == "behind"
        assert _since(drift) == T0 + 9000

    async def test_running_with_deploy_listed_is_not_green(self, drift, gh, marker):
        marker.write(D)
        gh.runs = [_run(11, G, status="in_progress", conclusion=None), _run(10, D, conclusion="success")]
        gh.jobs[_rid(11)] = _jobs(deploy=("pending", None), overrides={"Python Tests": ("in_progress", None)})

        assert await drift._check() == "in_sync"
        assert await drift._check() == "in_sync"
        assert gh.jobs_calls() == [_rid(11), _rid(11)]

    async def test_since_is_the_oldest_green_and_count_skips_red(self, drift, gh, marker):
        marker.write(D)
        gh.runs = [
            _run(14, _sha(14), conclusion="success", updated=T0 + 900),  # deployed fine, no lookup
            _run(13, _sha(13)),
            _run(12, _sha(12)),
            _run(11, _sha(11)),
            _run(10, D, conclusion="success"),
            _run(9, _sha(9)),  # older than the deployed commit: never looked at
        ]
        gh.jobs[_rid(13)] = _jobs(passed_at=T0 + 300)
        gh.jobs[_rid(12)] = _red_jobs()
        gh.jobs[_rid(11)] = _jobs(passed_at=T0 + 100)
        gh.jobs[_rid(9)] = _jobs(passed_at=T0 - 5000)

        assert await drift._check() == "behind"
        assert _count(drift) == 3
        assert _since(drift) == T0 + 100
        assert sorted(gh.jobs_calls()) == [_rid(11), _rid(12), _rid(13)]

    async def test_completed_success_run_needs_no_job_lookup(self, drift, gh, marker):
        marker.write(D)
        gh.runs = [_run(11, G, conclusion="success", updated=T0 + 42), _run(10, D, conclusion="success")]

        assert await drift._check() == "behind"
        assert (_count(drift), _since(drift)) == (1, T0 + 42)
        assert gh.jobs_calls() == []

    async def test_non_main_and_non_push_runs_are_ignored(self, drift, gh, marker):
        marker.write(D)
        gh.runs = [
            _run(13, _sha(13), branch="feature"),
            _run(12, _sha(12), event="workflow_dispatch"),
            _run(11, "not-a-sha"),
            _run(10, D, conclusion="success"),
        ]
        assert await drift._check() == "in_sync"
        assert gh.jobs_calls() == []


# ===========================================================================
# 4. Dedup / 5. verdict cache
# ===========================================================================


class TestDedupAndCache:
    async def test_same_commit_counts_once_by_its_newest_run(self, drift, gh, marker):
        marker.write(D)
        # Run ids are deliberately not in run_number order, and the list is shuffled.
        gh.runs = [
            _run(11, G, run_id=900),
            _run(10, D, conclusion="success", run_id=800),
            _run(12, G, run_id=500),
        ]
        gh.jobs[900] = _red_jobs()
        gh.jobs[500] = _jobs(passed_at=T0 + 50)

        assert await drift._check() == "behind"
        assert (_count(drift), _since(drift)) == (1, T0 + 50)
        assert gh.jobs_calls() == [500]

    async def test_newest_run_red_wins_over_older_green(self, drift, gh, marker):
        marker.write(D)
        gh.runs = [_run(12, G, run_id=500), _run(11, G, run_id=900), _run(10, D, conclusion="success")]
        gh.jobs[500] = _red_jobs()
        gh.jobs[900] = _jobs()

        assert await drift._check() == "in_sync"
        assert gh.jobs_calls() == [500]

    @pytest.mark.parametrize("green", [True, False], ids=["green", "red"])
    async def test_finished_verdict_is_not_refetched(self, drift, gh, marker, green):
        marker.write(D)
        gh.runs = [_run(11, G), _run(10, D, conclusion="success")]
        gh.jobs[_rid(11)] = _jobs(passed_at=T0 + 5) if green else _red_jobs()

        first = await drift._check()
        second = await drift._check()
        assert first == second == ("behind" if green else "in_sync")
        assert gh.jobs_calls() == [_rid(11)]
        if green:
            assert _since(drift) == T0 + 5

    async def test_new_attempt_is_refetched(self, drift, gh, marker):
        marker.write(D)
        gh.runs = [_run(11, G), _run(10, D, conclusion="success")]
        gh.jobs[_rid(11)] = _red_jobs()
        assert await drift._check() == "in_sync"

        # "Re-run failed jobs": same run id, attempt 2, now green.
        gh.runs = [_run(11, G, attempt=2), _run(10, D, conclusion="success")]
        gh.jobs[_rid(11)] = _jobs(passed_at=T0 + 77)
        assert await drift._check() == "behind"
        assert _since(drift) == T0 + 77
        assert gh.jobs_calls() == [_rid(11), _rid(11)]


# ===========================================================================
# 6. Lookup budget
# ===========================================================================


class TestLookupBudget:
    def _burst(self, gh: FakeGitHub, *, deployed_in_list: bool = True) -> list[int]:
        """Seven pushes newer than the deployed one, all with CI still running."""
        newer = list(range(11, 18))
        gh.runs = [_run(n, _sha(n), status="in_progress", conclusion=None) for n in reversed(newer)]
        if deployed_in_list:
            gh.runs.append(_run(10, D, conclusion="success"))
        for n in newer:
            gh.jobs[_rid(n)] = _running_jobs()
        return newer

    async def test_partial_keeps_gauges_for_the_same_marker(self, drift, gh, marker, logs):
        marker.write(D)
        gh.runs = [_run(10, D, conclusion="success")]
        assert await drift._check() == "in_sync"
        assert (_count(drift), _since(drift)) == (0, 0)

        newer = self._burst(gh)
        before = _counters(drift)
        await drift.check_deploy_drift()
        after = _counters(drift)

        assert after["partial"] == before["partial"] + 1
        assert all(after[r] == before[r] for r in FAILURE_RESULTS)
        assert (_count(drift), _since(drift)) == (0, 0)
        # Oldest first, and the budget is respected.
        assert gh.jobs_calls() == [_rid(n) for n in newer[: drift.MAX_JOB_LOOKUPS]]
        assert not any(level >= logging.WARNING for level, _ in logs())

    async def test_partial_with_a_new_marker_is_nan(self, drift, gh, marker):
        marker.write(B)
        self._burst(gh, deployed_in_list=False)

        assert await drift._check() == "partial"
        assert _drift_is_nan(drift)
        assert _info(drift) == [B[:12]]
        assert gh.compare_calls() == []

    async def test_green_found_before_budget_runs_out_is_behind(self, drift, gh, marker):
        marker.write(D)
        newer = self._burst(gh)
        gh.jobs[_rid(newer[0])] = _jobs(passed_at=T0 + 33, deploy=("queued", None))

        assert await drift._check() == "behind"
        assert (_count(drift), _since(drift)) == (1, T0 + 33)
        assert len(gh.jobs_calls()) == drift.MAX_JOB_LOOKUPS


# ===========================================================================
# 7. deploy_job_missing
# ===========================================================================


class TestDeployJobMissing:
    async def test_finished_run_without_deploy_job(self, drift, gh, marker, logs):
        marker.write(D)
        gh.runs = [_run(11, G), _run(10, D, conclusion="success")]
        gh.jobs[_rid(11)] = _jobs(deploy=("completed", "failure"), deploy_name="Deploy to Production")

        assert await drift._check() == "deploy_job_missing"
        assert _drift_is_nan(drift)
        assert any(level == logging.WARNING and "deploy_job_missing" in msg for level, msg in logs())

    async def test_finished_run_with_no_jobs_is_not_green(self, drift, gh, marker):
        # startup_failure (broken workflow file): no job ever started.
        marker.write(D)
        gh.runs = [_run(11, G, conclusion="startup_failure"), _run(10, D, conclusion="success")]
        gh.jobs[_rid(11)] = []

        assert await drift._check() == "in_sync"
        assert await drift._check() == "in_sync"
        assert gh.jobs_calls() == [_rid(11)], "a final no-jobs verdict should be cached"

    async def test_running_run_without_deploy_job_is_fine(self, drift, gh, marker):
        marker.write(D)
        gh.runs = [_run(11, G, status="in_progress", conclusion=None), _run(10, D, conclusion="success")]
        gh.jobs[_rid(11)] = _jobs(deploy=None, overrides={"Docker Build (orchestrator)": ("queued", None)})

        assert await drift._check() == "in_sync"
        assert (_count(drift), _since(drift)) == (0, 0)


# ===========================================================================
# 8. Deployed commit not in the run window
# ===========================================================================


def _window_setup(gh: FakeGitHub) -> None:
    """The marker D has no run in the window: run 20 (G2) and run 18 (G) are
    green, run 19 is red."""
    gh.runs = [_run(20, G2), _run(19, _sha(19)), _run(18, G)]
    gh.jobs[_rid(20)] = _jobs(passed_at=T0 + 2000)
    gh.jobs[_rid(19)] = _red_jobs()
    gh.jobs[_rid(18)] = _jobs(passed_at=T0 + 1000)


X = _sha("unrelated")

_COMPARE_CASES = {
    "ahead_all": (
        {"status": "ahead", "total_commits": 3, "commits": [{"sha": G}, {"sha": X}, {"sha": G2}]},
        "behind",
        2,
    ),
    "ahead_one": ({"status": "ahead", "total_commits": 2, "commits": [{"sha": X}, {"sha": G2}]}, "behind", 1),
    "ahead_none": ({"status": "ahead", "total_commits": 1, "commits": [{"sha": X}]}, "in_sync", 0),
    "ahead_paged": ({"status": "ahead", "total_commits": 250, "commits": [{"sha": G2}]}, "behind", 2),
    "behind": ({"status": "behind", "total_commits": 0, "commits": []}, "in_sync", 0),
    "identical": ({"status": "identical", "total_commits": 0, "commits": []}, "in_sync", 0),
    "diverged": ({"status": "diverged", "total_commits": 4, "commits": [{"sha": G}, {"sha": G2}]}, "not_on_main", 2),
    "http_404": (Resp(404, {"message": "Not Found"}), "not_on_main", 2),
    "http_422": (Resp(422, {"message": "No common ancestor"}), "not_on_main", 2),
}


class TestNotInWindow:
    @pytest.mark.parametrize("case", list(_COMPARE_CASES), ids=list(_COMPARE_CASES))
    async def test_compare_settles_it(self, drift, gh, marker, case):
        body, result, count = _COMPARE_CASES[case]
        marker.write(D)
        _window_setup(gh)
        gh.compare[(D, G2)] = body

        assert await drift._check() == result
        assert _count(drift) == count
        expected_since = {0: 0, 1: T0 + 2000, 2: T0 + 1000}[count]
        assert _since(drift) == expected_since
        # One compare, base = deployed, head = the newest green.
        assert gh.compare_calls() == [(D, G2)]

    async def test_no_green_in_window_is_in_sync_without_compare(self, drift, gh, marker):
        marker.write(D)
        gh.runs = [_run(20, G2), _run(19, G)]
        gh.jobs[_rid(20)] = _red_jobs()
        gh.jobs[_rid(19)] = _red_jobs()

        assert await drift._check() == "in_sync"
        assert gh.compare_calls() == []

    async def test_compare_server_error_is_github_error(self, drift, gh, marker):
        marker.write(D)
        _window_setup(gh)
        gh.compare[(D, G2)] = Resp(500, {"message": "boom"})
        assert await drift._check() == "github_error"
        assert _drift_is_nan(drift)

    async def test_compare_rate_limited(self, drift, gh, marker):
        marker.write(D)
        _window_setup(gh)
        gh.compare[(D, G2)] = Resp(429, {"message": "slow down"}, {"retry-after": "60"})
        assert await drift._check() == "rate_limited"


# ===========================================================================
# 9. Failures
# ===========================================================================


def _runs_with(**overrides) -> dict:
    run = _run(11, G)
    run.update(overrides)
    for key, value in list(run.items()):
        if value is _DROP:
            del run[key]
    return {"workflow_runs": [run, _run(10, D, conclusion="success")]}


_DROP = object()

_RUNS_FAILURES = {
    "http_500": Resp(500, {"message": "Server Error"}),
    "http_502": Resp(502, content=b"<html>bad gateway</html>"),
    "http_401": Resp(401, {"message": "Bad credentials"}),
    "http_403": Resp(403, {"message": "Resource not accessible"}),
    "http_404": Resp(404, {"message": "Not Found"}),
    "connect": _raise(httpx.ConnectError),
    "read_timeout": _raise(httpx.ReadTimeout),
    "not_json": Resp(200, content=b"<html>not json</html>"),
    "json_list": [1, 2, 3],
    "no_runs_key": {"total_count": 3},
    "runs_null": {"workflow_runs": None},
    "runs_dict": {"workflow_runs": {"a": 1}},
    "run_not_dict": {"workflow_runs": ["x", 3]},
    "no_updated_at": _runs_with(updated_at=_DROP),
    "bad_updated_at": _runs_with(updated_at="yesterday"),
    "null_updated_at": _runs_with(updated_at=None),
    "id_not_int": _runs_with(id="abc"),
    "no_run_number": _runs_with(run_number=_DROP),
    "empty_runs": {"total_count": 0, "workflow_runs": []},
    "only_pr_runs": {"workflow_runs": [_run(11, G, event="pull_request")]},
}

_JOBS_FAILURES = {
    "jobs_500": Resp(500, {"message": "Server Error"}),
    "jobs_connect": _raise(httpx.ConnectError),
    "jobs_null": {"jobs": None},
    "no_jobs_key": {"total_count": 1},
    "job_not_dict": {"jobs": ["x"]},
    # Built per request: _jobs() imports the module under test.
    "no_completed_at": lambda req: httpx.Response(200, json={"jobs": [{**j, "completed_at": None} for j in _jobs()]}),
}


class TestFailures:
    @pytest.mark.parametrize("case", list(_RUNS_FAILURES), ids=list(_RUNS_FAILURES))
    async def test_runs_list_failure_is_github_error(self, drift, gh, marker, case):
        marker.write(D)
        gh.runs_spec = _RUNS_FAILURES[case]
        gh.jobs[_rid(11)] = _jobs()

        assert await drift._check() == "github_error"
        assert _drift_is_nan(drift)
        assert _info(drift) == [D[:12]]

    @pytest.mark.parametrize("case", list(_JOBS_FAILURES), ids=list(_JOBS_FAILURES))
    async def test_jobs_failure_is_github_error(self, drift, gh, marker, case):
        marker.write(D)
        gh.runs = [_run(11, G), _run(10, D, conclusion="success")]
        gh.jobs[_rid(11)] = _JOBS_FAILURES[case]

        assert await drift._check() == "github_error"

    async def test_redirect_is_not_followed(self, drift, gh, marker):
        marker.write(D)
        moved = "https://api.github.com/repositories/1/actions/workflows/ci.yml/runs"
        gh.runs_spec = Resp(301, {"message": "Moved Permanently"}, {"location": moved})

        assert await drift._check() == "github_error"
        assert gh.paths() == [RUNS_PATH]

    async def test_empty_runs_names_the_likely_cause(self, drift, gh, marker, logs):
        marker.write(D)
        gh.runs_spec = {"workflow_runs": []}
        assert await drift._check() == "github_error"
        assert any("DEPLOY_DRIFT_REPO" in msg for _, msg in logs())

    async def test_whole_check_deadline(self, drift, marker, monkeypatch, logs):
        """A server that never answers can't hold the job past its deadline."""
        never = asyncio.Event()

        async def hang(request):
            await never.wait()

        marker.write(D)
        monkeypatch.setattr(drift, "CHECK_DEADLINE_SECONDS", 0.05)
        with respx.mock(assert_all_called=False) as router:
            router.route(host="api.github.com").mock(side_effect=hang)
            # Outer guard: without the module's own deadline this fails fast
            # instead of hanging the suite.
            assert await asyncio.wait_for(drift._check(), 10) == "github_error"
        assert any("no answer within" in msg for _, msg in logs())


class TestRateLimit:
    NOW = T0 + 100_000.0

    _CASES = {
        "403_reset": (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(T0 + 100_600))}, 600),
        "429_retry_after": (429, {"retry-after": "120"}, 120),
        "403_retry_after": (403, {"retry-after": "60"}, 60),
        "429_no_hint": (429, {}, 900),
        "retry_after_cap": (429, {"retry-after": "99999"}, 3600),
        "reset_cap": (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(T0 + 999_999))}, 3600),
        "reset_garbage": (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "soon"}, 900),
    }

    @pytest.mark.parametrize("case", list(_CASES), ids=list(_CASES))
    async def test_waits_until_reset_without_requests(self, drift, gh, marker, monkeypatch, case):
        status, headers, wait = self._CASES[case]
        clock = Clock(self.NOW)
        monkeypatch.setattr(drift, "time", clock)
        marker.write(D)
        gh.runs_spec = Resp(status, {"message": "API rate limit exceeded"}, headers)

        assert await drift._check() == "rate_limited"
        assert len(gh.calls) == 1

        gh.runs_spec = None
        gh.runs = [_run(10, D, conclusion="success")]
        clock.now = self.NOW + wait - 1
        assert await drift._check() == "rate_limited"
        assert len(gh.calls) == 1, "asked GitHub again before the rate limit reset"

        clock.now = self.NOW + wait + 1
        assert await drift._check() == "in_sync"
        assert len(gh.calls) == 2

    async def test_one_warning_per_rate_limit(self, drift, gh, marker, monkeypatch, logs):
        clock = Clock(self.NOW)
        monkeypatch.setattr(drift, "time", clock)
        marker.write(D)
        gh.runs_spec = Resp(429, {"message": "API rate limit exceeded"}, {"retry-after": "1200"})
        assert await drift._check() == "rate_limited"
        clock.now = self.NOW + 600
        assert await drift._check() == "rate_limited"
        warnings = [msg for level, msg in logs() if level == logging.WARNING]
        assert len(warnings) == 1

    async def test_jobs_endpoint_rate_limit(self, drift, gh, marker):
        marker.write(D)
        gh.runs = [_run(11, G), _run(10, D, conclusion="success")]
        gh.jobs[_rid(11)] = Resp(403, {"message": "rate"}, {"x-ratelimit-remaining": "0"})
        assert await drift._check() == "rate_limited"

    async def test_plain_403_with_remaining_budget_is_not_rate_limited(self, drift, gh, marker):
        marker.write(D)
        gh.runs_spec = Resp(403, {"message": "Forbidden"}, {"x-ratelimit-remaining": "41"})
        assert await drift._check() == "github_error"


# ===========================================================================
# 10. Gauge retention
# ===========================================================================


def _break_http_500(gh: FakeGitHub) -> None:
    gh.runs_spec = Resp(500, {"message": "boom"})


def _break_network(gh: FakeGitHub) -> None:
    gh.runs_spec = _raise(httpx.ConnectError)


def _break_rate_limit(gh: FakeGitHub) -> None:
    gh.runs_spec = Resp(429, {"message": "slow down"}, {"retry-after": "600"})


def _break_deploy_job(gh: FakeGitHub) -> None:
    gh.runs.insert(0, _run(12, G2))
    gh.jobs[_rid(12)] = _jobs(deploy=("completed", "failure"), deploy_name="Ship it")


_BREAKERS = {
    "http_500": (_break_http_500, "github_error"),
    "network": (_break_network, "github_error"),
    "rate_limited": (_break_rate_limit, "rate_limited"),
    "deploy_job_missing": (_break_deploy_job, "deploy_job_missing"),
}


class TestGaugeRetention:
    @pytest.mark.parametrize("case", list(_BREAKERS), ids=list(_BREAKERS))
    async def test_same_marker_keeps_new_marker_nans(self, drift, gh, marker, case):
        breaker, result = _BREAKERS[case]
        marker.write(D)
        _behind_setup(gh, passed_at=T0 + 4242)
        assert await drift._check() == "behind"
        assert (_count(drift), _since(drift)) == (1, T0 + 4242)

        breaker(gh)
        assert await drift._check() == result
        assert (_count(drift), _since(drift)) == (1, T0 + 4242)

        marker.write(B, mtime=T_MARK + 900)
        assert await drift._check() == result
        assert _drift_is_nan(drift)
        assert _info(drift) == [B[:12]]
        assert _stamp(drift) == T_MARK + 900

    async def test_success_after_failure_sets_fresh_values(self, drift, gh, marker):
        marker.write(B)
        gh.runs_spec = Resp(500, {"message": "boom"})
        assert await drift._check() == "github_error"
        assert _drift_is_nan(drift)

        gh.runs_spec = None
        _behind_setup(gh, deployed=B, passed_at=T0 + 1)
        assert await drift._check() == "behind"
        assert (_count(drift), _since(drift)) == (1, T0 + 1)


# ===========================================================================
# 11. Logging
# ===========================================================================


SENTINEL_TOKEN = "github_pat_SENTINEL_deploy_drift_0123456789"


class TestLogging:
    async def test_token_is_sent_but_never_logged(self, drift, gh, marker, monkeypatch, caplog, logs):
        from orchestrator.config import settings

        monkeypatch.setattr(settings, "deploy_drift_github_token", f"  {SENTINEL_TOKEN}\n", raising=False)
        marker.write(D)

        _behind_setup(gh)
        assert await drift._check() == "behind"
        _break_http_500(gh)
        assert await drift._check() == "github_error"
        _break_network(gh)
        assert await drift._check() == "github_error"
        gh.runs_spec = Resp(200, content=b"{not json")
        assert await drift._check() == "github_error"

        async def boom(*a, **k):
            raise RuntimeError("unexpected")

        real_drift = drift._drift
        gh.runs_spec = None
        monkeypatch.setattr(drift, "_drift", boom)
        drift._state.last_warned = None  # so the crash path logs its traceback
        assert await drift._check() == "github_error"
        monkeypatch.setattr(drift, "_drift", real_drift)
        gh.runs_spec = Resp(403, {"message": "rate"}, {"x-ratelimit-remaining": "0"})
        assert await drift._check() == "rate_limited"

        assert gh.calls
        assert all(r.headers.get("authorization") == f"Bearer {SENTINEL_TOKEN}" for r in gh.calls)
        assert logs(), "expected some log output to inspect"
        assert SENTINEL_TOKEN not in caplog.text
        assert "SENTINEL" not in caplog.text
        # str(e) of an httpx error carries the URL; only class names are logged.
        assert "api.github.com" not in caplog.text

    @pytest.mark.parametrize("token", ["", "   "], ids=["blank", "spaces"])
    async def test_no_authorization_header_without_token(self, drift, gh, marker, monkeypatch, token):
        from orchestrator.config import settings

        monkeypatch.setattr(settings, "deploy_drift_github_token", token, raising=False)
        marker.write(D)
        _behind_setup(gh)
        assert await drift._check() == "behind"
        assert len(gh.calls) == 2
        for r in gh.calls:
            assert "authorization" not in r.headers
            assert r.headers["accept"] == "application/vnd.github+json"
            assert r.headers["user-agent"] == "brain-gateway-deploy-drift"

    async def test_proxy_and_ca_env_are_ignored(self, drift, gh, marker, monkeypatch):
        """The token must not be routed through an env-configured proxy or CA:
        with trust_env on, httpx would load this (missing) CA file and fail."""
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
        monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/deploy-drift-ca.pem")
        marker.write(D)
        gh.runs = [_run(10, D, conclusion="success")]
        assert await drift._check() == "in_sync"
        assert str(gh.calls[0].url).startswith("https://api.github.com/")

    async def test_warning_once_then_debug_then_recovery(self, drift, gh, marker, logs):
        def failed() -> list[tuple[int, str]]:
            return [(lvl, msg) for lvl, msg in logs() if "Check failed" in msg]

        marker.write(D)
        gh.runs_spec = Resp(500, {"message": "boom"})
        await drift._check()
        await drift._check()
        assert [lvl for lvl, _ in failed()] == [logging.WARNING, logging.DEBUG]
        assert "github_error (HTTP 500)" in failed()[0][1]

        gh.runs_spec = Resp(502, {"message": "bad gateway"})
        await drift._check()
        assert failed()[-1][0] == logging.WARNING
        assert "HTTP 502" in failed()[-1][1]

        gh.runs_spec = None
        gh.runs = [_run(10, D, conclusion="success")]
        await drift._check()
        assert (logging.INFO, "[DEPLOY_DRIFT] Check recovered") in logs()

        gh.runs_spec = Resp(502, {"message": "bad gateway"})
        await drift._check()
        assert failed()[-1][0] == logging.WARNING, "the same failure after a recovery is new again"

    async def test_marker_missing_warns_once(self, drift, gh, marker, logs):
        for _ in range(3):
            assert await drift._check() == "marker_missing"
        levels = [lvl for lvl, msg in logs() if "Check failed" in msg]
        assert levels == [logging.WARNING, logging.DEBUG, logging.DEBUG]

    async def test_state_changes_log_once_with_short_sha(self, drift, gh, marker, logs):
        marker.write(D)
        _behind_setup(gh)
        await drift._check()
        await drift._check()
        gh.runs = [_run(10, D, conclusion="success")]
        await drift._check()
        await drift._check()

        infos = [msg for lvl, msg in logs() if lvl == logging.INFO]
        assert len(infos) == 2
        assert "behind main" in infos[0] and "1 newer commit(s)" in infos[0]
        assert "in sync" in infos[1]
        assert all(D[:12] in msg and D not in msg for msg in infos)

    async def test_crash_traceback_logged_only_once(self, drift, gh, marker, monkeypatch, caplog):
        caplog.set_level(logging.DEBUG, logger=drift.logger.name)

        async def boom(*a, **k):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(drift, "_drift", boom)
        marker.write(D)
        gh.runs = [_run(10, D, conclusion="success")]
        assert await drift._check() == "github_error"
        assert await drift._check() == "github_error"

        crashes = [r for r in caplog.records if "Check crashed" in r.getMessage()]
        assert [r.levelno for r in crashes] == [logging.ERROR]
        assert crashes[0].exc_info
        assert _drift_is_nan(drift)

    async def test_new_crash_after_other_failure_gets_traceback(self, drift, gh, marker, monkeypatch, caplog):
        caplog.set_level(logging.DEBUG, logger=drift.logger.name)
        marker.write(D)
        gh.runs_spec = Resp(500, {"message": "boom"})
        assert await drift._check() == "github_error"

        async def boom(*a, **k):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(drift, "_drift", boom)
        gh.runs_spec = None
        gh.runs = [_run(10, D, conclusion="success")]
        assert await drift._check() == "github_error"
        crashes = [r for r in caplog.records if "Check crashed" in r.getMessage()]
        assert len(crashes) == 1 and crashes[0].exc_info


# ===========================================================================
# 12. Scheduler wrapper
# ===========================================================================


class TestCheckDeployDrift:
    async def test_every_result_counts_exactly_one_preseeded_label(self, drift, gh, marker):
        assert set(_counters(drift)) == ALL_RESULTS, "every result must be pre-seeded"

        async def step(expected: str) -> None:
            before = _counters(drift)
            await drift.check_deploy_drift()
            after = _counters(drift)
            diff = {k: after.get(k, 0) - before.get(k, 0) for k in set(before) | set(after)}
            assert {k: v for k, v in diff.items() if v} == {expected: 1}

        await step("marker_missing")
        marker.write("", raw="garbage")
        await step("marker_invalid")

        marker.write(D)
        gh.runs = [_run(10, D, conclusion="success")]
        await step("in_sync")

        gh.reset()
        _behind_setup(gh)
        await step("behind")

        gh.reset()
        marker.write(B)
        _window_setup(gh)
        gh.compare[(B, G2)] = {"status": "diverged", "total_commits": 2, "commits": []}
        await step("not_on_main")

        gh.reset()
        _break_http_500(gh)
        await step("github_error")

        gh.reset()
        marker.write(D)
        gh.runs = [_run(n, _sha(n), status="in_progress", conclusion=None) for n in range(17, 10, -1)]
        gh.runs.append(_run(10, D, conclusion="success"))
        for n in range(11, 18):
            gh.jobs[_rid(n)] = _running_jobs()
        await step("partial")

        gh.reset()
        _behind_setup(gh)
        _break_deploy_job(gh)
        await step("deploy_job_missing")

        gh.reset()
        _break_rate_limit(gh)
        await step("rate_limited")

        assert set(_counters(drift)) == ALL_RESULTS, "a result outside the pre-seeded set was counted"

    @pytest.mark.parametrize("where", ["_check", "read_marker"], ids=["check", "read_marker"])
    async def test_never_raises(self, drift, gh, marker, monkeypatch, caplog, where):
        caplog.set_level(logging.ERROR, logger=drift.logger.name)

        async def async_boom(*a, **k):
            raise RuntimeError("unexpected")

        def sync_boom(*a, **k):
            raise PermissionError("unexpected")

        monkeypatch.setattr(drift, where, async_boom if where == "_check" else sync_boom)
        marker.write(D)
        before = _counters(drift)

        await drift.check_deploy_drift()  # must not raise

        after = _counters(drift)
        assert after["github_error"] == before["github_error"] + 1
        assert any("Check crashed" in r.getMessage() for r in caplog.records)


# ===========================================================================
# 13. Config validator
# ===========================================================================


def _settings_with_repo(value: str):
    from orchestrator.config import Settings

    env = {"MODEL_URL": "http://localhost:8080/v1", "MODEL_NAME": "m", "DEPLOY_DRIFT_REPO": value}
    with patch.dict(os.environ, env, clear=True):
        return Settings(_env_file=None)


class TestConfig:
    def test_defaults(self):
        from orchestrator.config import Settings

        with patch.dict(os.environ, {"MODEL_URL": "http://localhost:8080/v1", "MODEL_NAME": "m"}, clear=True):
            s = Settings(_env_file=None)
        assert s.deploy_drift_repo == ""
        assert s.deploy_drift_github_token == ""
        assert s.deploy_marker_path == "/opt/gateway_mvp/.last-deployed-sha"

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("mexirab/brain_gateway", "mexirab/brain_gateway"),
            ("  mexirab/brain_gateway \n", "mexirab/brain_gateway"),
            ("Owner-1/repo.name_x", "Owner-1/repo.name_x"),
            ("a" * 39 + "/x", "a" * 39 + "/x"),
        ],
        ids=["real", "padded", "punct", "owner39"],
    )
    def test_valid_repo_is_kept(self, value, expected):
        assert _settings_with_repo(value).deploy_drift_repo == expected

    @pytest.mark.parametrize(
        "value",
        [
            "bad",
            "a/b/c",
            "owner/..",
            "owner/.",
            "../x",
            "owner/name?x=1",
            "owner/name#frag",
            "owner/na me",
            "-owner/x",
            "a" * 40 + "/x",
            "owner/",
            "/repo",
            "owner/%2e%2e",
            "https://github.com/owner/repo",
        ],
        ids=[
            "no_slash",
            "three_parts",
            "dotdot",
            "dot",
            "traversal",
            "query",
            "fragment",
            "space",
            "lead_hyphen",
            "owner40",
            "no_name",
            "no_owner",
            "pct_dotdot",
            "url",
        ],
    )
    def test_malformed_repo_disables_the_check(self, value, caplog):
        caplog.set_level(logging.ERROR)
        assert _settings_with_repo(value).deploy_drift_repo == ""
        assert any("DEPLOY_DRIFT_REPO" in r.getMessage() for r in caplog.records)


# ===========================================================================
# 14./15. Coupling with CI and the monitoring config
# ===========================================================================


def _load_yaml_or_skip(rel: str):
    path = REPO_ROOT / rel
    if not path.is_file():
        pytest.skip(f"{rel} not available in this checkout/mount")
    return yaml.safe_load(path.read_text())


class TestCiCoupling:
    def _ci(self):
        from orchestrator.jobs_deploy_drift import WORKFLOW_FILE

        return _load_yaml_or_skip(f".github/workflows/{WORKFLOW_FILE}")

    def test_deploy_job_name_matches(self):
        from orchestrator.jobs_deploy_drift import DEPLOY_JOB_NAME

        jobs = self._ci()["jobs"]
        named = [jid for jid, job in jobs.items() if job.get("name") == DEPLOY_JOB_NAME]
        assert len(named) == 1, f"exactly one CI job must be named {DEPLOY_JOB_NAME!r}"
        assert "strategy" not in jobs[named[0]], "a matrix would suffix the deploy job's name"

    def test_deploy_job_needs_every_other_job(self):
        from orchestrator.jobs_deploy_drift import DEPLOY_JOB_NAME

        jobs = self._ci()["jobs"]
        deploy_id = next(jid for jid, job in jobs.items() if job.get("name") == DEPLOY_JOB_NAME)
        needs = jobs[deploy_id].get("needs") or []
        needs = [needs] if isinstance(needs, str) else list(needs)
        assert set(needs) == set(jobs) - {deploy_id}

    def test_workflow_runs_on_push_to_main(self):
        from orchestrator import jobs_deploy_drift as mod

        ci = self._ci()
        on = ci.get("on", ci.get(True))  # PyYAML reads a bare `on:` key as True
        branches = on["push"]["branches"]
        assert mod.BRANCH in ([branches] if isinstance(branches, str) else branches)

    def test_deploy_job_writes_the_marker_the_orchestrator_reads(self):
        from orchestrator.config import Settings
        from orchestrator.jobs_deploy_drift import DEPLOY_JOB_NAME

        jobs = self._ci()["jobs"]
        deploy = next(job for job in jobs.values() if job.get("name") == DEPLOY_JOB_NAME)
        script = "\n".join(step.get("run", "") for step in deploy["steps"])
        marker_name = os.path.basename(Settings.model_fields["deploy_marker_path"].default)
        assert marker_name in script
        assert re.search(r'>\s*"?\$\{?MARKER\}?"?', script), "the deploy job must write the marker"


def _route_receivers(route: dict, labels: dict[str, str]) -> list[str]:
    """Receivers Alertmanager would notify for ``labels`` (child routes in
    order; a match without ``continue`` stops the walk)."""

    def matches(child: dict) -> bool:
        for matcher in child.get("matchers") or []:
            m = re.fullmatch(r'\s*(\w+)\s*(=~|!~|!=|=)\s*"(.*)"\s*', matcher)
            assert m, f"unparseable matcher {matcher!r}"
            name, op, value = m.groups()
            actual = labels.get(name, "")
            ok = {
                "=": actual == value,
                "!=": actual != value,
                "=~": re.fullmatch(value, actual) is not None,
                "!~": re.fullmatch(value, actual) is None,
            }[op]
            if not ok:
                return False
        return True

    out = []
    for child in route.get("routes") or []:
        if matches(child):
            out.extend(_route_receivers(child, labels) or [child["receiver"]])
            if not child.get("continue"):
                return out
    return out or [route.get("receiver", "")]


class TestAlertWiring:
    def _rules(self) -> dict[str, dict]:
        data = _load_yaml_or_skip("monitoring/prometheus/alert-rules.yml")
        return {r["alert"]: r for g in data["groups"] for r in g.get("rules", []) if "alert" in r}

    def test_both_alerts_exist(self):
        rules = self._rules()
        assert "DeployDrift" in rules
        assert "DeployDriftCheckFailing" in rules

    @pytest.mark.parametrize("alert", ["DeployDrift", "DeployDriftCheckFailing"], ids=["drift", "check_failing"])
    def test_routed_to_pushover_quiet(self, alert):
        am = _load_yaml_or_skip("monitoring/alertmanager/alertmanager.yml.template")
        quiet = [
            r
            for r in am["route"]["routes"]
            if r.get("receiver") == "pushover-quiet"
            and any(str(m).strip().startswith("alertname=~") for m in r.get("matchers") or [])
        ]
        assert quiet, "no pushover-quiet route with an alertname=~ matcher"
        regexes = [
            re.fullmatch(r'\s*alertname=~"(.*)"\s*', m).group(1)
            for r in quiet
            for m in r["matchers"]
            if str(m).strip().startswith("alertname=~")
        ]
        assert any(re.fullmatch(rx, alert) for rx in regexes)
        # And the route walk ends there (not paging, not the null receiver).
        severity = self._rules()[alert]["labels"]["severity"]
        receivers = _route_receivers(am["route"], {"alertname": alert, "severity": severity})
        assert receivers[-1] == "pushover-quiet"
        assert "pushover" not in receivers and "null" not in receivers

    def test_check_failing_counts_exactly_the_failure_results(self):
        from orchestrator import jobs_deploy_drift as mod

        expr = self._rules()["DeployDriftCheckFailing"]["expr"]
        counter_name = next(
            s.name for m in mod.DEPLOY_DRIFT_CHECK_TOTAL.collect() for s in m.samples if s.name.endswith("_total")
        )
        assert counter_name in expr
        rx = re.search(r'result=~"([^"]+)"', expr).group(1)
        for result in FAILURE_RESULTS:
            assert re.fullmatch(rx, result), f"{result} should feed DeployDriftCheckFailing"
        for result in OK_RESULTS:
            assert not re.fullmatch(rx, result), f"{result} is not a failure"

    def test_drift_alert_reads_the_since_gauge(self):
        from orchestrator import jobs_deploy_drift as mod

        expr = self._rules()["DeployDrift"]["expr"]
        name = mod.DEPLOY_UNDEPLOYED_GREEN_SINCE._name
        assert name in expr
        # 0 means "in sync": the alert must not read it as "behind since 1970".
        assert re.search(re.escape(name) + r"\{[^}]*\}\s*>\s*0", expr)
