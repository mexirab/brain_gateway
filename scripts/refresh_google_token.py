#!/usr/bin/env python3
"""Keep the Google OAuth token warm — independently of the orchestrator.

Why this exists
---------------
The orchestrator only refreshes the Google token when it's alive AND makes a
Google API call. If it's down for a long stretch (e.g. the GPU box is powered
off), the token goes stale and Calendar/Gmail silently fail. That's exactly what
caused a ~2-month silent outage. Refreshing on a weekly cron from the always-on
box (Jupiter) keeps the token — and Google's refresh token itself, which expires
after ~6 months of non-use for unverified apps — alive no matter what the
orchestrator is doing.

This script is deliberately standalone: it talks to Google directly using the
stored OAuth files, so it works even when the orchestrator container is down.
It only needs `google-auth` installed in whatever Python runs it.

Paths default to <repo>/credentials/ and can be overridden with
GOOGLE_TOKEN_PATH / GOOGLE_CREDENTIALS_PATH. On success it can write a Prometheus
textfile-collector metric (GOOGLE_TOKEN_METRICS_PATH) that the
GoogleTokenRefreshStale alert watches — independent of the orchestrator, since
node_exporter reads the file directly. Point it at node_exporter's textfile dir,
e.g. GOOGLE_TOKEN_METRICS_PATH=/var/lib/node_exporter/textfile_collector/jess_google_token.prom

Exit codes: 0 = refreshed/valid · 1 = not configured (no files) · 2 = failed.

Cron (weekly, Mondays 04:00) on the always-on host:
    0 4 * * 1  cd /opt/gateway_mvp && python3 scripts/refresh_google_token.py >> /var/log/jess-token-refresh.log 2>&1

Or reuse the container's Python env (only when the container is up):
    docker exec brain-orchestrator python scripts/refresh_google_token.py
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TOKEN = os.path.join(_HERE, "..", "credentials", "google_token.json")
DEFAULT_CREDS = os.path.join(_HERE, "..", "credentials", "google_credentials.json")

# MUST match orchestrator/google_auth.py SCOPES.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def _write_metric(path: str, ts: int) -> None:
    """Atomically write a Prometheus textfile-collector metric (success timestamp)."""
    body = (
        "# HELP jess_google_token_refresh_success_timestamp_seconds "
        "Unix time of the last successful Google OAuth token refresh.\n"
        "# TYPE jess_google_token_refresh_success_timestamp_seconds gauge\n"
        f"jess_google_token_refresh_success_timestamp_seconds {ts}\n"
    )
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(body)
    os.replace(tmp, path)


def main() -> int:
    token_path = os.environ.get("GOOGLE_TOKEN_PATH", DEFAULT_TOKEN)
    creds_path = os.environ.get("GOOGLE_CREDENTIALS_PATH", DEFAULT_CREDS)
    metrics_path = os.environ.get("GOOGLE_TOKEN_METRICS_PATH", "")

    if not os.path.exists(creds_path) or not os.path.exists(token_path):
        print(
            f"[token-refresh] not configured — need {creds_path} and {token_path}. "
            "Run google_setup.py first. Nothing to do."
        )
        return 1

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as e:
        print(f"[token-refresh] google-auth is not installed in this Python: {e}", file=sys.stderr)
        return 2

    try:
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    except Exception as e:
        print(f"[token-refresh] could not load token file: {e}", file=sys.stderr)
        return 2

    if not creds or not creds.refresh_token:
        print(
            "[token-refresh] token file has no refresh_token — re-run google_setup.py "
            "to re-authorize.",
            file=sys.stderr,
        )
        return 2

    # Refresh unconditionally: this mints a new access token AND exercises the
    # refresh token so Google doesn't expire it for inactivity.
    try:
        creds.refresh(Request())
    except Exception as e:
        print(f"[token-refresh] refresh FAILED: {e}", file=sys.stderr)
        return 2

    try:
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    except Exception as e:
        print(f"[token-refresh] refreshed but could NOT save {token_path}: {e}", file=sys.stderr)
        return 2

    print(f"[token-refresh] OK — token refreshed (expires {getattr(creds, 'expiry', '?')})")

    if metrics_path:
        try:
            _write_metric(metrics_path, int(time.time()))
        except Exception as e:  # metric is best-effort, never fail the run for it
            print(f"[token-refresh] (warning) could not write metric {metrics_path}: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
