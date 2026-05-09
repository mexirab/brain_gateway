"""
Daily self-audit (F-014).

Queries Loki for the last N hours of error/warn level logs across all
Helios services (Docker containers + systemd units), buckets near-identical
messages, asks Jess to diagnose each cluster, and pushes a one-line digest
via Pushover. The full markdown report is persisted under
``SELF_AUDIT_OUTPUT_DIR`` so the user can review and discuss with Claude
Code before running any suggested commands.

Read-only by design: Jess emits text only. The orchestrator never executes
anything from her output. Safety story has three layers:

1. **Allow-list filter on suggested commands.** Each ``Suggestion:`` line
   gets shlex-tokenized; the head argv (after stripping sudo/wrappers) must
   be in a small read-only allow-list. Anything off-list is redacted.
2. **Dangerous-pattern regex** catches shell-injection markers
   (pipe-to-shell, command substitution payloads, redirect-write outside
   /tmp, etc.) regardless of whether the head argv looks safe.
3. **Secret-pattern filter** on disk write and mempalace index, so a
   service that logged a credential can't bake it into the audit report
   or future-Jess context.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from orchestrator import shared
from orchestrator.config import settings
from orchestrator.jobs_training_corpus import SECRET_PATTERNS
from orchestrator.metrics import (
    SELF_AUDIT_CLUSTERS_TOTAL,
    SELF_AUDIT_FORMAT_DRIFT_TOTAL,
    SELF_AUDIT_LATENCY,
    SELF_AUDIT_RUNS_TOTAL,
)

logger = logging.getLogger(__name__)


# --- Tunables ---------------------------------------------------------------

_BUCKET_PREFIX_LEN = 80
_LOKI_LIMIT = 5000

# Concurrency guard — one audit at a time. Cron + manual-trigger share this.
# Without it, a curl-loop on /api/self_audit/run can pin the Jess slot and
# starve the unified loop.
_AUDIT_LOCK = asyncio.Lock()


# --- Suggested-command filter (allow-list approach) ------------------------
#
# Allow-list is much stronger than a deny-list of substring fragments:
# Cyrillic homoglyphs, ;rm, $(rm), /bin/rm, base64-decoded payloads, etc.
# all bypass a substring check. With an allow-list, anything we don't
# recognize is rejected. False positives are recoverable (user re-asks);
# destructive false negatives are not.

_ALLOWED_COMMANDS = frozenset({
    "journalctl", "systemctl", "docker",
    "ls", "cat", "tail", "head", "grep", "ss", "top", "htop", "ps",
    "df", "du", "free", "uptime", "uname", "whoami", "date", "wc",
    "stat", "find", "awk", "sed", "echo", "tr", "lsof", "nvidia-smi",
})

# Subcommand allow-list for multi-purpose binaries. Empty set means
# "binary is fine but reject if it has any subcommand requirement".
_ALLOWED_SUBCOMMANDS: dict[str, set[str]] = {
    "systemctl": {"status", "restart", "reload", "is-active", "is-enabled",
                  "show", "list-units", "list-jobs", "cat"},
    "docker": {"logs", "ps", "compose", "stats", "inspect", "image"},
}

_DANGEROUS_PATTERNS = re.compile(
    r"(?:"
    r"\|\s*(?:sh|bash|zsh|dash)\b"           # pipe-to-shell
    r"|\$\([^)]*(?:rm|dd|mkfs|chmod|chown|kill|reboot|halt)\b"
    r"|`[^`]*(?:rm|dd|mkfs|chmod|chown|kill|reboot|halt)\b"
    r"|>\s*(?!/tmp/|/dev/null)\S*/"          # redirect-write outside /tmp
    r"|\bbase64\s+-d\b"                        # base64-decode pipeline
    r"|\beval\b"                                 # eval
    r"|\b(?:nc|ncat|netcat)\s+-l"              # listener
    r"|:\s*\(\)\s*\{"                            # fork-bomb leading shape
    r")",
    re.IGNORECASE,
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _check_suggestion(suggestion_text: str) -> tuple[bool, str]:
    """Return ``(allowed, reason)``. Reason is empty when allowed.

    Expects a string like ``INVESTIGATE: journalctl -u foo --since '1h ago'``.
    Anything that fails parsing, leaves the allow-list, or trips the
    dangerous-pattern regex returns ``False``.
    """
    line = unicodedata.normalize("NFKC", suggestion_text).strip()

    m = re.match(r"^(INVESTIGATE|FIX):\s*(.+)$", line, re.IGNORECASE)
    if not m:
        return False, "missing INVESTIGATE/FIX prefix"
    cmd = m.group(2).strip()

    if _DANGEROUS_PATTERNS.search(cmd):
        return False, "shell-injection or destructive pattern"

    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return False, "unparseable shell command"
    if not tokens:
        return False, "empty command"

    # Strip benign wrappers
    while tokens and tokens[0] in ("sudo", "nice", "ionice", "time"):
        tokens = tokens[1:]
    if not tokens:
        return False, "wrapper without command"

    head = tokens[0].rsplit("/", 1)[-1]  # /usr/bin/foo -> foo
    if head not in _ALLOWED_COMMANDS:
        return False, f"command not allow-listed: {head}"

    subs = _ALLOWED_SUBCOMMANDS.get(head)
    if subs:
        idx = 1
        while idx < len(tokens) and tokens[idx].startswith("-"):
            idx += 1
        if idx >= len(tokens):
            return False, f"{head} requires a subcommand"
        sub = tokens[idx]
        if sub not in subs:
            return False, f"subcommand not allow-listed: {head} {sub}"

    return True, ""


# --- Loki query -------------------------------------------------------------


async def _fetch_loki_errors(
    *,
    loki_url: str,
    lookback_hours: int,
    limit: int = _LOKI_LIMIT,
    timeout_sec: int = 30,
) -> tuple[list[dict[str, Any]], bool]:
    """Pull error/warn level logs from Loki.

    Returns ``(entries, transport_ok)``. ``transport_ok=False`` means Loki
    was unreachable or returned a malformed response — caller MUST distinguish
    this from the genuine "no errors" case to avoid lying to the user.
    """
    end = _now_utc()
    start = end - timedelta(hours=lookback_hours)
    query = '{host="helios", level=~"error|warn|critical|fatal"}'
    url = loki_url.rstrip("/") + "/loki/api/v1/query_range"
    params = {
        "query": query,
        "start": str(int(start.timestamp() * 1e9)),
        "end": str(int(end.timestamp() * 1e9)),
        "limit": str(limit),
        "direction": "backward",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError:
        logger.exception("[SELF_AUDIT] Loki query transport failed")
        return [], False
    except (json.JSONDecodeError, ValueError) as e:
        logger.exception("[SELF_AUDIT] Loki response decode failed: %s", e)
        return [], False

    out: list[dict[str, Any]] = []
    try:
        streams = (data.get("data") or {}).get("result") or []
    except AttributeError:
        logger.exception("[SELF_AUDIT] Loki response shape unexpected")
        return [], False

    for stream in streams:
        labels = stream.get("stream") or {}
        service = (
            labels.get("container")
            or labels.get("service")
            or labels.get("unit")
            or "unknown"
        )
        service = service.lstrip("/")
        level = labels.get("level", "unknown")
        for entry in stream.get("values") or []:
            if not isinstance(entry, list) or len(entry) < 2:
                continue
            ts_ns, line = entry[0], entry[1]
            try:
                ts = datetime.fromtimestamp(int(ts_ns) / 1e9, tz=timezone.utc)
            except (ValueError, TypeError):
                ts = end
            out.append(
                {
                    "ts": ts,
                    "service": service,
                    "level": level,
                    "line": str(line)[:2000],
                }
            )
    out.sort(key=lambda e: e["ts"])
    return out, True


async def _loki_alive(loki_url: str, timeout_sec: int = 10) -> bool:
    """Probe Loki with a query that should match in any healthy week.

    Used to distinguish "Loki returned 0 streams matching error filter"
    (= genuine clean week) from "Loki itself is unreachable" (= false-clean
    digest would lie to the user). See prod-support review concern #6.
    """
    url = loki_url.rstrip("/") + "/loki/api/v1/query"
    params = {"query": '{host="helios"}', "limit": "1"}
    try:
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, json.JSONDecodeError, ValueError):
        return False
    return bool((data.get("data") or {}).get("result"))


# --- Bucketing --------------------------------------------------------------


def _normalize_message(line: str) -> str:
    """Reduce a log line to a stable cluster key."""
    s = line.strip()
    if s.startswith("{") and '"msg"' in s:
        try:
            j = json.loads(s)
            if isinstance(j, dict) and isinstance(j.get("msg"), str):
                s = j["msg"]
        except json.JSONDecodeError:
            pass
    s = re.sub(r"[0-9a-f]{8,}", "<id>", s)
    s = re.sub(r"\d{2,}", "<n>", s)
    return s[:_BUCKET_PREFIX_LEN].strip()


def _bucket_logs(
    entries: list[dict[str, Any]], max_clusters: int
) -> list[dict[str, Any]]:
    """Group entries by ``(service, normalized_prefix)``. Top-N by count."""
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for e in entries:
        key = (e["service"], _normalize_message(e["line"]))
        if key not in buckets:
            buckets[key] = {
                "service": e["service"],
                "level": e["level"],
                "sample": e["line"][:500],
                "count": 0,
                "first_seen": e["ts"],
                "last_seen": e["ts"],
            }
        b = buckets[key]
        b["count"] += 1
        if e["ts"] > b["last_seen"]:
            b["last_seen"] = e["ts"]
        if e["ts"] < b["first_seen"]:
            b["first_seen"] = e["ts"]
    ranked = sorted(buckets.values(), key=lambda b: -b["count"])
    return ranked[:max_clusters]


# --- Prompt construction ----------------------------------------------------


_AUDIT_PROMPT = """You are a systems administrator running a daily 7am audit of a personal AI assistant deployment running on a single host (Helios) with multiple GPUs.

Below are the {n_clusters} most frequent error/warning clusters from Loki for the last {lookback_hours} hours, grouped by service.

For each cluster, output a markdown section in EXACTLY this format:

## <service> · <SEVERITY>
- **Count:** <count>x in last {lookback_hours}h
- **First/Last:** <first_seen> -> <last_seen>
- **Likely cause:** <one or two sentences>
- **Suggestion:** <prefix>: <one shell command>

Where:
- SEVERITY in {{CRITICAL, HIGH, MEDIUM, LOW}} -- your judgment based on user impact
- prefix in {{INVESTIGATE, FIX}} -- INVESTIGATE for read-only diagnostic, FIX only for confident read-only or service-level restart
- The shell command MUST be a single command using ONLY: journalctl, systemctl status/restart/reload/show/cat, docker logs/ps/compose/stats/inspect, ls, cat, tail, head, grep, ss, top, htop, ps, df, du, free, uptime, find, awk, sed, lsof, nvidia-smi, wc, stat
- NO `rm`, `dd`, `mkfs`, `chmod`, `chown`, `kill`, `iptables`, `nft`, `tee`, `chattr`, `mount`, `umount`, `passwd`, `useradd`, `crontab`, `at`, `nc`, `curl`, `wget`, `sh`, `bash`, `python`, `eval`, `base64 -d`, `>`, `|sh`, `$(...)`, backticks. If you would suggest one of these, downgrade to INVESTIGATE.
- Prefer `journalctl -u <unit> --since '24h ago' | tail -50` or `docker logs <container> --tail 50` for INVESTIGATE.
- Use `systemctl restart <unit>` or `docker compose restart <service>` for service-level FIX.

End with EXACTLY one line:

**Overall:** <C> critical, <H> high, <M> medium, <L> low

Where C/H/M/L are the counts of each severity across the clusters above. No other text after that line.

---

CLUSTERS:

{clusters_block}
"""


def _format_cluster_for_prompt(cluster: dict[str, Any]) -> str:
    return (
        f"### {cluster['service']} (level={cluster['level']}, count={cluster['count']})\n"
        f"First: {cluster['first_seen'].isoformat()}\n"
        f"Last:  {cluster['last_seen'].isoformat()}\n"
        f"Sample log line:\n```\n{cluster['sample']}\n```\n"
    )


def _build_audit_prompt(
    clusters: list[dict[str, Any]], lookback_hours: int
) -> str:
    if not clusters:
        return ""
    blocks = "\n".join(_format_cluster_for_prompt(c) for c in clusters)
    return _AUDIT_PROMPT.format(
        n_clusters=len(clusters),
        lookback_hours=lookback_hours,
        clusters_block=blocks,
    )


# --- Diagnosis sanitization -------------------------------------------------


_SUGGESTION_LINE_RE = re.compile(
    r"^(?P<prefix>\s*-?\s*\*\*Suggestion\*\*:\s*)(?P<payload>.+)$"
)
# Loosened: don't require severity word at end-of-line (model often appends
# punctuation or annotations like "(N times)").
_SEVERITY_HEADER_RE = re.compile(
    r"^##\s+.*?\b(CRITICAL|HIGH|MEDIUM|LOW)\b",
    re.MULTILINE,
)


def _sanitize_diagnosis(diagnosis: str) -> str:
    """Re-tag any Suggestion line whose command fails the allow-list filter.

    The original command is REDACTED in the rejected case so the user can't
    accidentally copy-paste a poisoned suggestion from the report.
    """
    out_lines: list[str] = []
    for line in diagnosis.split("\n"):
        m = _SUGGESTION_LINE_RE.match(line)
        if not m:
            out_lines.append(line)
            continue
        ok, reason = _check_suggestion(m.group("payload"))
        if ok:
            out_lines.append(line)
        else:
            out_lines.append(
                f"{m.group('prefix')}[REJECTED-AUTO: {reason}] "
                f"(original suggestion redacted; ask Claude what to investigate)"
            )
    return "\n".join(out_lines)


def _redact_secrets(text: str) -> str:
    """Mask anything matching SECRET_PATTERNS. Used on disk write + mempalace."""
    if not text:
        return text
    out = text
    for pat in SECRET_PATTERNS:
        out = pat.sub("<REDACTED-SECRET>", out)
    return out


def _count_severities(diagnosis: str) -> dict[str, int]:
    counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for m in _SEVERITY_HEADER_RE.finditer(diagnosis):
        counts[m.group(1)] += 1
    return counts


# --- Persistence ------------------------------------------------------------


def _save_audit_report_sync(
    *,
    output_dir: str,
    diagnosis: str,
    clusters: list[dict[str, Any]],
    lookback_hours: int,
    started_at: datetime,
    duration_sec: float,
) -> str | None:
    """Sync write — caller wraps in ``asyncio.to_thread``."""
    try:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        date_str = started_at.strftime("%Y-%m-%d")
        path = out / f"{date_str}.md"
        header = (
            f"# Jess self-audit · {date_str}\n\n"
            f"- Started: {started_at.isoformat()}\n"
            f"- Lookback: {lookback_hours}h\n"
            f"- Clusters: {len(clusters)}\n"
            f"- Duration: {duration_sec:.1f}s\n\n"
            f"---\n\n"
        )
        if diagnosis:
            body = _redact_secrets(diagnosis)
        else:
            body = "_(No diagnosis -- Jess unavailable. Cluster data below.)_\n\n"
            for c in clusters:
                body += _redact_secrets(_format_cluster_for_prompt(c)) + "\n"
        # Atomic write — survives concurrent triggers without partial writes.
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(header + body, encoding="utf-8")
        tmp.replace(path)
        logger.info("[SELF_AUDIT] Report saved: %s", path)
        return str(path)
    except (OSError, UnicodeError):
        logger.exception("[SELF_AUDIT] Failed to write report")
        return None


# --- Pushover digest --------------------------------------------------------


async def _push_digest(
    *,
    severity_counts: dict[str, int],
    top_cluster: dict[str, Any] | None,
    report_filename: str | None,
    lookback_hours: int,
    title_suffix: str = "",
) -> None:
    """Send a one-line digest via Pushover (best-effort, never raises).

    ``report_filename`` is just the basename — full path is omitted from the
    digest body to avoid leaking container-internal paths through Pushover's
    cloud (security review #5).
    """
    from orchestrator.pushover_manager import deliver_pushover_confirm

    crit = severity_counts.get("CRITICAL", 0)
    high = severity_counts.get("HIGH", 0)
    med = severity_counts.get("MEDIUM", 0)
    low = severity_counts.get("LOW", 0)
    total = crit + high + med + low
    date_str = _now_utc().strftime("%Y-%m-%d")

    title = f"Jess audit · {date_str}"
    if title_suffix:
        title += f" {title_suffix}"

    if total == 0:
        body = f"All clean ({lookback_hours}h scan, no errors)"
    else:
        body = f"{crit} critical, {high} high, {med} medium, {low} low"
        if top_cluster:
            body += f"\nTop: {top_cluster['service']} · {top_cluster['count']}x"
        if report_filename:
            body += f"\nReport: {report_filename}"

    try:
        await deliver_pushover_confirm(title=title, message=body)
    except Exception:
        logger.warning("[SELF_AUDIT] Pushover digest failed (non-fatal)", exc_info=True)


# --- Mempalace indexing -----------------------------------------------------


async def _index_to_mempalace(
    diagnosis_summary: str, severity_counts: dict[str, int]
) -> None:
    """Best-effort: store a short summary so future Jess can recall.

    The summary is run through ``_redact_secrets`` first — a service that
    logged a credential is otherwise a permanent leak into mempalace.
    """
    try:
        palace = shared.get_palace()
    except Exception:
        return
    if palace is None:
        return
    try:
        crit = severity_counts.get("CRITICAL", 0)
        high = severity_counts.get("HIGH", 0)
        text = (
            f"Self-audit {_now_utc().strftime('%Y-%m-%d')}: "
            f"{crit} critical, {high} high. "
            f"Summary: {diagnosis_summary[:400]}"
        )
        text = _redact_secrets(text)
        await palace.store(
            text=text,
            wing="system",
            room="audit",
            source="self_audit",
            category="general",
        )
    except Exception:
        logger.warning("[SELF_AUDIT] mempalace index failed (non-fatal)", exc_info=True)


# --- Main entrypoint --------------------------------------------------------


async def _ask_jess(*, prompt: str, timeout_sec: int) -> str | None:
    """Single ``call_model`` invocation. Deferred import to avoid cycles.

    System prompt is intentionally omitted: the audit is a one-off task with
    its own persona override in the prompt body, and we don't want the unified-loop
    Jess wrapping to try to call tools mid-audit.
    """
    from orchestrator.orchestrator import call_model

    try:
        resp = await call_model(
            shared.MODEL_URL,
            shared.MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            timeout=timeout_sec,
        )
    except Exception as e:
        logger.warning("[SELF_AUDIT] LLM call failed: %s", e)
        return None
    if not isinstance(resp, dict):
        # call_model can return None on early-bailout paths (e.g. tier
        # selection rejected the prompt). Treat as "LLM unavailable".
        return None
    return (resp.get("choices") or [{}])[0].get("message", {}).get("content")


async def run_self_audit() -> dict[str, Any]:
    """Execute the daily self-audit. Never raises.

    Returns a dict with ``result`` in
    ``{ok, partial, failed, skipped, busy}``. ``busy`` means another audit
    is already in flight (manual + cron collided).
    """
    if not (settings.self_audit_enabled and settings.jess_advanced):
        SELF_AUDIT_RUNS_TOTAL.labels(result="skipped").inc()
        reason = (
            "SELF_AUDIT_ENABLED=false"
            if not settings.self_audit_enabled
            else "JESS_ADVANCED=false"
        )
        return {"result": "skipped", "reason": reason}

    if _AUDIT_LOCK.locked():
        logger.warning("[SELF_AUDIT] Refusing concurrent run — audit already in flight")
        return {"result": "busy", "reason": "another audit is running"}

    async with _AUDIT_LOCK:
        return await _run_self_audit_locked()


async def _run_self_audit_locked() -> dict[str, Any]:
    start = time.monotonic()
    started_at = _now_utc()

    logger.info("[SELF_AUDIT] Starting daily audit")

    entries, transport_ok = await _fetch_loki_errors(
        loki_url=settings.self_audit_loki_url,
        lookback_hours=settings.self_audit_lookback_hours,
    )

    # Distinguish unreachable Loki from genuinely-clean week (prod-support #6).
    if not transport_ok:
        logger.error("[SELF_AUDIT] Loki transport failed — refusing to claim 'clean'")
        await _push_digest(
            severity_counts={"CRITICAL": 1, "HIGH": 0, "MEDIUM": 0, "LOW": 0},
            top_cluster={"service": "loki", "count": 1},
            report_filename=None,
            lookback_hours=settings.self_audit_lookback_hours,
            title_suffix="· LOKI UNREACHABLE",
        )
        SELF_AUDIT_RUNS_TOTAL.labels(result="failed").inc()
        SELF_AUDIT_LATENCY.observe(time.monotonic() - start)
        return {"result": "failed", "reason": "loki_unreachable"}

    if not entries:
        # Empty result with transport_ok=True: probe to confirm Loki returns
        # any helios stream at all. If even the probe is empty, we can't trust
        # the silence — promote to failed.
        if not await _loki_alive(settings.self_audit_loki_url):
            logger.error("[SELF_AUDIT] Loki probe empty — treating as unreachable")
            await _push_digest(
                severity_counts={"CRITICAL": 1, "HIGH": 0, "MEDIUM": 0, "LOW": 0},
                top_cluster={"service": "loki", "count": 1},
                report_filename=None,
                lookback_hours=settings.self_audit_lookback_hours,
                title_suffix="· LOKI EMPTY",
            )
            SELF_AUDIT_RUNS_TOTAL.labels(result="failed").inc()
            SELF_AUDIT_LATENCY.observe(time.monotonic() - start)
            return {"result": "failed", "reason": "loki_probe_empty"}

        logger.info("[SELF_AUDIT] No error/warn entries in lookback window")
        await _push_digest(
            severity_counts={"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0},
            top_cluster=None,
            report_filename=None,
            lookback_hours=settings.self_audit_lookback_hours,
        )
        SELF_AUDIT_RUNS_TOTAL.labels(result="ok").inc()
        SELF_AUDIT_LATENCY.observe(time.monotonic() - start)
        return {"result": "ok", "clusters": 0}

    clusters = _bucket_logs(entries, settings.self_audit_max_clusters)
    logger.info(
        "[SELF_AUDIT] %d log entries -> %d clusters", len(entries), len(clusters)
    )

    prompt = _build_audit_prompt(clusters, settings.self_audit_lookback_hours)
    raw_diagnosis = await _ask_jess(
        prompt=prompt, timeout_sec=settings.self_audit_llm_timeout_sec
    )

    if raw_diagnosis:
        diagnosis = _sanitize_diagnosis(raw_diagnosis)
        result_tag = "ok"
    else:
        diagnosis = ""
        result_tag = "partial"

    severity_counts = _count_severities(diagnosis) if diagnosis else {}
    if diagnosis and not any(severity_counts.values()):
        # Diagnosis text exists but no severity headers parsed — model went
        # off-format. Surface this so we can tune the prompt / re-evaluate.
        SELF_AUDIT_FORMAT_DRIFT_TOTAL.inc()
        logger.warning("[SELF_AUDIT] Diagnosis parsed zero severity headers — format drift")

    for sev, n in severity_counts.items():
        SELF_AUDIT_CLUSTERS_TOTAL.labels(severity=sev).inc(n)

    duration = time.monotonic() - start
    report_path = await asyncio.to_thread(
        _save_audit_report_sync,
        output_dir=settings.self_audit_output_dir,
        diagnosis=diagnosis,
        clusters=clusters,
        lookback_hours=settings.self_audit_lookback_hours,
        started_at=started_at,
        duration_sec=duration,
    )

    top_cluster = clusters[0] if clusters else None
    await _push_digest(
        severity_counts=severity_counts
        or {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0},
        top_cluster=top_cluster,
        report_filename=Path(report_path).name if report_path else None,
        lookback_hours=settings.self_audit_lookback_hours,
    )

    if diagnosis:
        await _index_to_mempalace(diagnosis[:500], severity_counts)

    SELF_AUDIT_RUNS_TOTAL.labels(result=result_tag).inc()
    SELF_AUDIT_LATENCY.observe(time.monotonic() - start)

    logger.info(
        "[SELF_AUDIT] Done · result=%s · clusters=%d · sev=%s · path=%s",
        result_tag, len(clusters), severity_counts, report_path,
    )
    return {
        "result": result_tag,
        "clusters": len(clusters),
        "severity_counts": severity_counts,
        "report_path": report_path,
    }
