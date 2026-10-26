"""Deploy drift: which commit is live on this box, and is it behind main?

The CI deploy job (``.github/workflows/ci.yml``, job "Deploy to Jupiter", on
the self-hosted runner) writes the commit it deployed to ``.last-deployed-sha``
in the live checkout at the end of its rebuild step. That marker is the source
of truth for what runs here: the orchestrator image is only rebuilt when
orchestrator files change, so nothing baked into the image tracks it. The
orchestrator reads it through the read-only repo mount (``DEPLOY_MARKER_PATH``).

A commit can pass CI on main and still never deploy: GitHub keeps one pending
run per concurrency group and cancels it when another arrives, a deploy can
fail or time out, and the runner can be stopped. This check runs on the
always-on orchestrator, not on the runner, so it sees all of those.

Every ``CHECK_INTERVAL_MINUTES`` it lists the latest push runs of ci.yml on
main and walks the ones newer than the deployed commit's run. A run "passed
CI" when every job of its latest attempt except the deploy job succeeded. Runs
are ordered by run_number, which is push order on main; a commit with several
push runs counts by its newest. If the deployed commit is not among those runs
(far behind, no run of its own, or not on main at all), one compare call
settles it.

Results (``bgw_deploy_drift_check_total{result}``): ``in_sync``, ``behind``,
``not_on_main`` (all three set the gauges); ``partial`` (the lookup budget ran
out before any green run was found: gauges kept, retried next check);
``marker_missing``, ``marker_invalid``, ``deploy_job_missing`` (a finished run
lists no job named ``DEPLOY_JOB_NAME``: the deploy job was renamed, and every
run would otherwise read as "not green"), ``github_error``, ``rate_limited``.
The last five feed DeployDriftCheckFailing. After any of them the drift gauges
keep their values while the marker is unchanged (main's green commits don't go
away, so "behind since T" stays true); a new marker makes them NaN until GitHub
answers again. (Rare exception: a force-push or a re-run that turns a green run
red clears the drift without a new marker; the gauges catch up on the next
successful check.)

Known limits: a marker older than the 20-run window with every run in it red
reads ``in_sync``; and a deploy job that ships main's tip at pull time (rather
than the commit whose CI passed) can mark a commit whose own CI is still
running or later fails, which also reads ``in_sync``.

Gauges (``orchestrator/metrics.py`` → Deploys): ``bgw_deployed_commit_info``,
``bgw_deployed_commit_timestamp_seconds`` (marker mtime),
``bgw_deploy_undeployed_green_commits`` and
``bgw_deploy_undeployed_green_since_timestamp_seconds`` (when the OLDEST
undeployed green commit passed CI, so a stream of new merges can't keep
resetting the clock). DeployDrift alerts on the latter.

GitHub budget: one runs list per check, plus one jobs list per newer run whose
verdict isn't cached (a finished attempt's verdict never changes), at most
``MAX_JOB_LOOKUPS``, plus a compare call in the rare not-in-window case: ~6
requests/h steady, up to ~42/h, against 60/h per public IP unauthenticated
(shared with the rest of the LAN). ``DEPLOY_DRIFT_GITHUB_TOKEN`` (fine-grained,
public-repo read-only, no permissions) lifts that to 5000/h; it is never the
runner's or the owner's gh login. Requests go to api.github.com only, redirects
are not followed, proxy/CA environment variables are ignored, and logs carry
short SHAs, counts, HTTP status codes and exception class names — never the
token or ``str(e)`` (an httpx error message carries the URL).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from orchestrator.config import settings
from orchestrator.metrics import (
    DEPLOY_DRIFT_CHECK_TOTAL,
    DEPLOY_UNDEPLOYED_GREEN_COMMITS,
    DEPLOY_UNDEPLOYED_GREEN_SINCE,
    DEPLOYED_COMMIT_INFO,
    DEPLOYED_COMMIT_TIMESTAMP,
)

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
WORKFLOW_FILE = "ci.yml"
BRANCH = "main"
# Keep in sync with the deploy job's `name:` in .github/workflows/ci.yml
# (tests/test_deploy_drift.py checks it, and that it `needs` every other job).
DEPLOY_JOB_NAME = "Deploy to Jupiter"
CHECK_INTERVAL_MINUTES = 10
RUNS_PER_CHECK = 20
MAX_JOB_LOOKUPS = 5
REQUEST_TIMEOUT_SECONDS = 15
# Whole check, all requests: a slow-dripping response must not hold the job
# (APScheduler runs one instance at a time) past the next interval.
CHECK_DEADLINE_SECONDS = 120
SHORT_SHA_LEN = 12
# Wait this long after a rate-limit answer that says nothing about when it resets.
RATE_LIMIT_DEFAULT_WAIT_SECONDS = 900
COMPARE_PAGE_SIZE = 100

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_NAN = float("nan")
_RATE_LIMIT_CAUSE = "GitHub API rate limit; set DEPLOY_DRIFT_GITHUB_TOKEN to raise it"
_RESPONSE_ERRORS = (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, IndexError, OverflowError)


class _RateLimited(Exception):
    def __init__(self, until: float) -> None:
        super().__init__("GitHub API rate limit")
        self.until = until


class _DeployJobMissing(Exception):
    pass


class _NoRuns(ValueError):
    """main always has push runs: none is a wrong repo/workflow or a changed
    API, and reading it as "in sync" would hide every drift."""


@dataclass(frozen=True)
class _Run:
    id: int
    number: int
    attempt: int
    sha: str
    status: str
    conclusion: str | None
    updated_at: float


@dataclass
class _State:
    last_warned: tuple[str, str] | None = None  # (result, cause) last logged at WARNING
    behind: bool | None = None  # last logged in-sync/behind state
    checked_sha: str | None = None  # marker SHA at the last check that set the gauges
    published_sha: str | None = None  # SHA behind the bgw_deployed_commit_info series
    rate_limited_until: float = 0.0
    # (run id, attempt) -> when its CI passed (epoch s), or None if it didn't.
    verdicts: dict[tuple[int, int], float | None] = field(default_factory=dict)


_state = _State()


def _short(sha: str) -> str:
    return sha[:SHORT_SHA_LEN]


def _ts(value: object) -> float:
    """GitHub's ISO-8601 timestamp as epoch seconds. ValueError when missing or
    unparseable: a made-up "now" would keep resetting DeployDrift's clock."""
    if not isinstance(value, str) or not value:
        raise ValueError("missing timestamp")
    try:
        return datetime.fromisoformat(value).timestamp()
    except (OverflowError, OSError) as e:
        raise ValueError("timestamp out of range") from e


def read_marker(path: str) -> tuple[str, float] | str:
    """``(sha, mtime)`` from the deploy marker, or the check result naming
    what's wrong with it: ``marker_missing`` (absent, unreadable, not a regular
    file) or ``marker_invalid`` (not a 40-hex SHA)."""
    try:
        # O_NONBLOCK: a FIFO in its place must not block the event loop.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return "marker_missing"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return "marker_missing"
        raw = os.read(fd, 128)
    except OSError:
        return "marker_missing"
    finally:
        os.close(fd)
    sha = raw.decode("ascii", errors="replace").strip().lower()
    if not _SHA_RE.fullmatch(sha):
        return "marker_invalid"
    return sha, st.st_mtime


def _publish_marker(marker: tuple[str, float] | None) -> None:
    sha = marker[0] if marker else None
    if sha != _state.published_sha:
        # One series at a time: drop the old commit's before adding the new one.
        DEPLOYED_COMMIT_INFO.clear()
        if sha:
            DEPLOYED_COMMIT_INFO.labels(commit=_short(sha)).set(1)
        _state.published_sha = sha
    DEPLOYED_COMMIT_TIMESTAMP.set(marker[1] if marker else _NAN)


def _set_drift(count: float, since: float) -> None:
    DEPLOY_UNDEPLOYED_GREEN_COMMITS.set(count)
    DEPLOY_UNDEPLOYED_GREEN_SINCE.set(since)


def _client() -> httpx.AsyncClient:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "brain-gateway-deploy-drift",
    }
    token = settings.deploy_drift_github_token.strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.AsyncClient(
        base_url=GITHUB_API,
        headers=headers,
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=False,
        trust_env=False,
    )


def _retry_at(headers: httpx.Headers) -> float:
    now = time.time()
    try:
        if "retry-after" in headers:
            return now + min(float(headers["retry-after"]), 3600)
        if "x-ratelimit-reset" in headers:
            return min(float(headers["x-ratelimit-reset"]), now + 3600)
    except ValueError:
        pass
    return now + RATE_LIMIT_DEFAULT_WAIT_SECONDS


async def _get_json(client: httpx.AsyncClient, path: str, params: dict[str, object] | None = None) -> dict:
    resp = await client.get(path, params=params)
    if resp.status_code == 429 or (
        resp.status_code == 403 and (resp.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in resp.headers)
    ):
        raise _RateLimited(_retry_at(resp.headers))
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("unexpected GitHub response shape")
    return data


async def _fetch_runs(client: httpx.AsyncClient, repo: str) -> list[_Run]:
    """Latest push runs of the CI workflow on main, newest first, one per commit."""
    data = await _get_json(
        client,
        f"/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/runs",
        params={"branch": BRANCH, "event": "push", "per_page": RUNS_PER_CHECK, "exclude_pull_requests": "true"},
    )
    newest: dict[str, _Run] = {}
    for item in data["workflow_runs"]:
        sha = item.get("head_sha")
        if item.get("head_branch") != BRANCH or item.get("event") != "push":
            continue
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            continue
        run = _Run(
            id=int(item["id"]),
            number=int(item["run_number"]),
            attempt=int(item.get("run_attempt") or 1),
            sha=sha,
            status=str(item.get("status") or ""),
            conclusion=item.get("conclusion"),
            updated_at=_ts(item.get("updated_at")),
        )
        if sha not in newest or run.number > newest[sha].number:
            newest[sha] = run
    if not newest:
        raise _NoRuns
    return sorted(newest.values(), key=lambda r: r.number, reverse=True)


async def _passed_at(client: httpx.AsyncClient, repo: str, run: _Run, lookups: list[int]) -> tuple[bool, float | None]:
    """``(known, passed_at)``: when ``run`` passed CI (epoch s), None if it
    didn't (yet). ``known`` is False when the per-check lookup budget ran out."""
    if run.status == "completed" and run.conclusion == "success":
        # Deploy job included. Shouldn't happen for a run newer than the
        # deployed one; updated_at (the latest possible time) keeps it from
        # aging an alert early.
        return True, run.updated_at
    key = (run.id, run.attempt)
    if key in _state.verdicts:
        return True, _state.verdicts[key]
    if lookups[0] >= MAX_JOB_LOOKUPS:
        return False, None
    lookups[0] += 1
    data = await _get_json(client, f"/repos/{repo}/actions/runs/{run.id}/jobs", params={"per_page": 100})
    jobs = data["jobs"]
    if not isinstance(jobs, list):
        raise ValueError("unexpected GitHub jobs shape")
    if run.status == "completed" and not jobs:
        # Finished without starting a job (startup_failure: a broken workflow
        # file). Never green, and final for this attempt.
        _state.verdicts[key] = None
        return True, None
    has_deploy = any(j.get("name") == DEPLOY_JOB_NAME for j in jobs)
    if run.status == "completed" and not has_deploy:
        # A finished run lists every job, skipped ones included.
        raise _DeployJobMissing
    others = [j for j in jobs if j.get("name") != DEPLOY_JOB_NAME]
    done = [j for j in others if j.get("status") == "completed"]
    failed = any(j.get("conclusion") != "success" for j in done)
    # The deploy job `needs` every other job, so once it is listed they all are.
    passed = has_deploy and bool(others) and len(done) == len(others) and not failed
    at = None
    if passed:
        at = max(_ts(j.get("completed_at")) for j in others)
    if passed or failed:
        _state.verdicts[key] = at  # final for this attempt; a re-run is a new attempt
    return True, at


async def _compare(client: httpx.AsyncClient, repo: str, deployed: str, green: str) -> tuple[str, set[str] | None]:
    """GitHub's status of ``green`` relative to ``deployed`` (ahead, behind,
    identical, diverged; ``unknown`` when GitHub doesn't know ``deployed``),
    plus the commits in ``green`` but not in ``deployed`` when the first page
    holds all of them, else None."""
    try:
        data = await _get_json(
            client, f"/repos/{repo}/compare/{deployed}...{green}", params={"per_page": COMPARE_PAGE_SIZE}
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (404, 422):
            return "unknown", None
        raise
    commits = data.get("commits") or []
    shas = {c.get("sha") for c in commits if isinstance(c, dict)}
    complete = isinstance(data.get("total_commits"), int) and data["total_commits"] <= len(commits)
    return str(data.get("status") or "unknown"), shas if complete else None


async def _drift(client: httpx.AsyncClient, repo: str, runs: list[_Run], deployed: str) -> tuple[str, int, float]:
    """``(result, undeployed green commits, oldest one's CI-pass time or 0)``."""
    idx = next((i for i, r in enumerate(runs) if r.sha == deployed), None)
    newer = runs if idx is None else runs[:idx]
    lookups = [0]
    unknown = False
    green: list[tuple[_Run, float]] = []
    # Oldest first: the oldest undeployed green commit sets the clock, so the
    # lookup budget goes to it before newer ones. Runs skipped for budget are
    # therefore newer than every green found.
    for run in reversed(newer):
        known, at = await _passed_at(client, repo, run, lookups)
        unknown = unknown or not known
        if known and at is not None:
            green.append((run, at))
    live = {(r.id, r.attempt) for r in runs}
    _state.verdicts = {k: v for k, v in _state.verdicts.items() if k in live}

    if not green and unknown:
        return "partial", 0, 0.0
    result = "behind" if green else "in_sync"
    if idx is None and green:
        # The deployed commit has no run among the latest RUNS_PER_CHECK pushes:
        # far behind, a commit without a push run of its own, or not on main.
        newest_green = max(green, key=lambda g: g[0].number)[0]
        status, undeployed = await _compare(client, repo, deployed, newest_green.sha)
        if status in ("behind", "identical"):
            result, green = "in_sync", []
        elif status != "ahead":
            result = "not_on_main"
        elif undeployed is not None:
            # Drop green runs the deployed commit already contains.
            green = [g for g in green if g[0].sha in undeployed]
            result = "behind" if green else "in_sync"
    since = min((at for _, at in green), default=0.0)
    return result, len(green), since


def _not_checked(sha: str | None, result: str, cause: str) -> str:
    """The check couldn't set the drift gauges: keep them for the same
    marker, NaN for a new one. Returns ``result``."""
    if sha is None or sha != _state.checked_sha:
        _set_drift(_NAN, _NAN)
    if result == "partial":
        logger.debug("[DEPLOY_DRIFT] Check partial: %s", cause)
        return result
    # WARNING when the failure is new or its cause changed, DEBUG while it
    # repeats (every 10 min); DeployDriftCheckFailing alerts on the counter.
    level = logging.DEBUG if _state.last_warned == (result, cause) else logging.WARNING
    logger.log(level, "[DEPLOY_DRIFT] Check failed: %s (%s)", result, cause)
    _state.last_warned = (result, cause)
    return result


async def _query_github(sha: str) -> tuple[str, int, float]:
    async with _client() as client:
        runs = await _fetch_runs(client, settings.deploy_drift_repo)
        return await _drift(client, settings.deploy_drift_repo, runs, sha)


async def _check() -> str:
    marker = read_marker(settings.deploy_marker_path)
    if isinstance(marker, str):
        _publish_marker(None)
        _state.checked_sha = None
        return _not_checked(None, marker, "no readable deploy marker at DEPLOY_MARKER_PATH")
    sha = marker[0]
    _publish_marker(marker)

    if time.time() < _state.rate_limited_until:
        # Same cause as the 403/429 that set the wait, so it warns once.
        return _not_checked(sha, "rate_limited", _RATE_LIMIT_CAUSE)
    try:
        result, count, since = await asyncio.wait_for(_query_github(sha), CHECK_DEADLINE_SECONDS)
    except _RateLimited as e:
        _state.rate_limited_until = e.until
        return _not_checked(sha, "rate_limited", _RATE_LIMIT_CAUSE)
    except _DeployJobMissing:
        return _not_checked(sha, "deploy_job_missing", f"no job named {DEPLOY_JOB_NAME!r} in a finished CI run")
    except httpx.HTTPStatusError as e:
        return _not_checked(sha, "github_error", f"HTTP {e.response.status_code}")
    except TimeoutError:
        return _not_checked(sha, "github_error", f"no answer within {CHECK_DEADLINE_SECONDS}s")
    except _NoRuns:
        return _not_checked(
            sha, "github_error", f"no push runs of {WORKFLOW_FILE} on {BRANCH}: wrong DEPLOY_DRIFT_REPO?"
        )
    except _RESPONSE_ERRORS as e:
        return _not_checked(sha, "github_error", type(e).__name__)
    except Exception as e:  # a bug here must not freeze stale gauges
        # ERROR + traceback once per new crash, not every 10 min while it lasts.
        if _state.last_warned != ("github_error", type(e).__name__):
            logger.error("[DEPLOY_DRIFT] Check crashed: %s", type(e).__name__, exc_info=True)
        return _not_checked(sha, "github_error", type(e).__name__)
    if result == "partial":
        return _not_checked(sha, result, "job lookup budget used up before a green run was found")

    _set_drift(count, since)
    _state.checked_sha = sha
    if _state.last_warned is not None:
        logger.info("[DEPLOY_DRIFT] Check recovered")
        _state.last_warned = None
    behind = result != "in_sync"
    if behind != _state.behind:
        short = _short(sha)
        if behind:
            logger.info(
                "[DEPLOY_DRIFT] Deployed %s is behind main (%s): %d newer commit(s) passed CI and are not deployed",
                short,
                result,
                count,
            )
        else:
            logger.info("[DEPLOY_DRIFT] Deployed %s is in sync with main's newest green commit", short)
        _state.behind = behind
    return result


async def check_deploy_drift() -> None:
    """Scheduler job: one drift check. Never raises."""
    try:
        result = await _check()
    except Exception as e:  # safety net for anything before the marker is read
        logger.error("[DEPLOY_DRIFT] Check crashed: %s", type(e).__name__, exc_info=True)
        result = "github_error"
    DEPLOY_DRIFT_CHECK_TOTAL.labels(result=result).inc()
