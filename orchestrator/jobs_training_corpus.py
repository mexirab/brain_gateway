"""
Background job: nightly training-corpus drain.

Reads every source of user/assistant conversation turns we care about and
appends new records to an append-only monthly JSONL at
``<training_corpus_dir>/YYYY-MM.jsonl``.

Sources:
  - Open WebUI sqlite db (``training_corpus_owui_db``)
  - state_store ``chat_messages`` (``training_corpus_state_db``)
  - Claude Code session jsonls (``training_corpus_cc_dir``)

Design:
  - Append-only, never deletes. Retention is "forever".
  - Content-addressed dedup: sha1(source|session|role|whitespace-normalized
    text). Safe to re-run any time, safe across backfills and schema churn.
  - One file per month so old data stays readable but the hot file is small.
  - Graceful: any missing source is skipped with a warning, not an error.
  - Secret-aware: records matching any secret/ciphertext pattern are dropped
    before they hit disk. Partial redaction is NOT done — adjacent context
    can re-identify a leaked secret, so we drop the whole record.
  - Runs inside a thread via ``asyncio.to_thread`` — all I/O is blocking.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, TypeGuard

from orchestrator.config import settings
from orchestrator.metrics import TRAINING_CORPUS_RECORDS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

MIN_LEN = 2
MAX_RECORD_CHARS = 50_000  # drop any single turn >50 KB
MAX_JSON_BLOB_BYTES = 5_000_000  # per-row blob ceiling for sqlite reads
MAX_CC_LINE_BYTES = 2_000_000  # per-line ceiling for jsonl reads
OVERSIZE_RUN_WARN = 10_000  # nightly-drain new-records anomaly threshold
SQLITE_READ_TIMEOUT_SEC = 30

# System-reminder / tool-dump prefixes that identify wholesale harness noise.
# Previous implementation dropped anything starting with ``<`` or ``[``, which
# also killed legitimate messages (code questions, bracketed notes).
_NOISE_PREFIXES = (
    "<system-reminder>",
    "<command-",
    "<local-command-",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<function_calls>",
    "<ide_selection>",
)

# Secret / ciphertext patterns. Shared shape with
# scripts/build_embedding_corpus.py — keep the two in sync when adding rules.
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),  # OpenAI / Anthropic
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),  # Google
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{20,}"),  # Slack
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}"),
    re.compile(
        r"(?i)(api[_-]?key|token|secret|password)"
        r"['\"]?\s*[:=]\s*['\"][^'\"]{12,}"
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # Fernet ciphertext blobs from auto_learn. Useless for an embedding model
    # and a leak vector if auto_learn.key ever escapes.
    re.compile(r"gAAAAA[A-Za-z0-9_\-=]{40,}"),
    # HA long-lived access tokens (JWT-shaped).
    re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}"),
)


# ---------------------------------------------------------------------------
# Record shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    id: str
    ts: str  # ISO8601, best-effort — sources without timestamps use drain time
    source: str  # "owui" | "state_store" | "cc_session"
    session_id: str
    role: str  # "user" | "assistant"
    text: str


def _fingerprint(source: str, session_id: str, role: str, text: str) -> str:
    norm = re.sub(r"\s+", " ", text.lower()).strip()
    h = hashlib.sha1()
    h.update(source.encode("utf-8"))
    h.update(b"|")
    h.update(session_id.encode("utf-8"))
    h.update(b"|")
    h.update(role.encode("utf-8"))
    h.update(b"|")
    h.update(norm.encode("utf-8"))
    return h.hexdigest()


def _looks_secret(text: str) -> bool:
    return any(p.search(text) for p in SECRET_PATTERNS)


def _accept(text: object) -> TypeGuard[str]:
    """TypeGuard narrowing: after this returns True, mypy knows text is str."""
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if len(stripped) < MIN_LEN:
        return False
    if len(stripped) > MAX_RECORD_CHARS:
        return False
    return not _looks_secret(stripped)


def _looks_like_noise(text: str) -> bool:
    return any(text.startswith(p) for p in _NOISE_PREFIXES)


def _parse_ts(value: Any) -> str:
    """Best-effort ISO8601 string. Accepts unix epoch (int/float) or a string."""
    if isinstance(value, (int, float)) and value > 0:
        try:
            return datetime.fromtimestamp(value, tz=UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            pass
    if isinstance(value, str) and value:
        return value
    return datetime.now(tz=UTC).isoformat()


def _open_sqlite_ro(db_path: Path) -> sqlite3.Connection:
    """Open a sqlite file strictly read-only, WAL-friendly, with a timeout.

    Using the URI form with ``mode=ro`` lets us safely mount the OWUI
    database (which may be in WAL mode and written-to by another container)
    without risking a write-lock upgrade or an error-on-open when the
    filesystem mount is ``:ro``.
    """
    conn = sqlite3.connect(
        f"file:{db_path}?mode=ro",
        uri=True,
        timeout=SQLITE_READ_TIMEOUT_SEC,
    )
    conn.execute("PRAGMA query_only = ON")
    return conn


# ---------------------------------------------------------------------------
# Source extractors — each yields Record objects
# ---------------------------------------------------------------------------


def drain_owui(db_path: Path) -> Iterator[Record]:
    """Extract user + assistant turns from every OWUI conversation."""
    if not db_path.exists():
        logger.warning("[training_corpus] owui db missing at %s — skipping", db_path)
        return
    try:
        with closing(_open_sqlite_ro(db_path)) as conn:
            # Stream row-by-row; OWUI chat blobs can be multi-MB per row.
            for chat_id, blob in conn.execute("SELECT id, chat FROM chat"):
                if isinstance(blob, (str, bytes)) and len(blob) > MAX_JSON_BLOB_BYTES:
                    logger.warning(
                        "[training_corpus] owui row %s oversized (%d bytes) — skipping",
                        chat_id,
                        len(blob),
                    )
                    continue
                try:
                    doc = json.loads(blob) if isinstance(blob, (str, bytes)) else {}
                except (TypeError, json.JSONDecodeError):
                    continue

                messages: list = doc.get("messages") or []
                if not messages and isinstance(doc.get("history"), dict):
                    messages = list(doc["history"].get("messages", {}).values())

                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    role = msg.get("role")
                    if role not in {"user", "assistant"}:
                        continue
                    content = msg.get("content")
                    if isinstance(content, list):
                        content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
                    if not _accept(content):
                        continue
                    text = content.strip()
                    ts = _parse_ts(msg.get("timestamp"))
                    session_id = f"owui:{chat_id}"
                    yield Record(
                        id=_fingerprint("owui", session_id, role, text),
                        ts=ts,
                        source="owui",
                        session_id=session_id,
                        role=role,
                        text=text,
                    )
    except sqlite3.Error as e:
        logger.warning("[training_corpus] owui read failed (%s): %s", db_path, e)


def drain_state_store(db_path: Path) -> Iterator[Record]:
    """Extract user + assistant turns from brain_state.chat_messages."""
    if not db_path.exists():
        logger.warning("[training_corpus] state_store db missing at %s — skipping", db_path)
        return
    try:
        with closing(_open_sqlite_ro(db_path)) as conn:
            cursor = conn.execute(
                "SELECT conversation_id, role, content, created_at "
                "FROM chat_messages ORDER BY conversation_id, created_at"
            )
            for conv_id, role, content, created_at in cursor:
                if role not in {"user", "assistant"}:
                    continue
                if not _accept(content):
                    continue
                text = content.strip()
                session_id = f"ss:{conv_id}"
                yield Record(
                    id=_fingerprint("state_store", session_id, role, text),
                    ts=_parse_ts(created_at),
                    source="state_store",
                    session_id=session_id,
                    role=role,
                    text=text,
                )
    except sqlite3.Error as e:
        logger.warning("[training_corpus] state_store read failed (%s): %s", db_path, e)


def _cc_extract_text(content: object) -> str | None:
    """Claude Code jsonl content may be str or list-of-parts. Skip tool_result."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_result":
                return None
            if part.get("type") == "text":
                t = part.get("text")
                if isinstance(t, str):
                    texts.append(t)
        return "\n".join(texts) if texts else None
    return None


def drain_cc_sessions(root: Path) -> Iterator[Record]:
    """Extract user turns from Claude Code session jsonls.

    User-only for v1. Assistant turns are voluminous Claude output on
    technical work — useful later but a trivial extension.
    """
    if not root.exists():
        logger.warning("[training_corpus] cc dir missing at %s — skipping", root)
        return
    for fp in sorted(root.glob("*.jsonl")):
        session_id = f"cc:{fp.stem}"
        try:
            with fp.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if len(line) > MAX_CC_LINE_BYTES:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("type") != "user":
                        continue
                    msg = rec.get("message") or {}
                    if msg.get("role") != "user":
                        continue
                    text = _cc_extract_text(msg.get("content"))
                    if not _accept(text):
                        continue
                    text = text.strip()
                    if _looks_like_noise(text):
                        continue
                    yield Record(
                        id=_fingerprint("cc_session", session_id, "user", text),
                        ts=_parse_ts(rec.get("timestamp")),
                        source="cc_session",
                        session_id=session_id,
                        role="user",
                        text=text,
                    )
        except OSError as e:
            logger.warning("[training_corpus] cc read failed for %s: %s", fp, e)


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


def _monthly_file(out_dir: Path, now: datetime | None = None) -> Path:
    # Local-time month rollover matches the nightly-drain schedule (02:30 local)
    # so month boundaries land on the day the records were produced.
    now = now or datetime.now()
    return out_dir / f"{now.strftime('%Y-%m')}.jsonl"


def _load_existing_ids(out_dir: Path) -> set[str]:
    """Build dedup set from ALL monthly files. Backfilled records can land in
    a different month than the current one, so a single-month scan is unsafe.

    TODO: move to a sidecar index file once the corpus exceeds ~100k records
    to avoid a full re-scan on every drain.
    """
    seen: set[str] = set()
    if not out_dir.exists():
        return seen
    for fp in sorted(out_dir.glob("*.jsonl")):
        try:
            with fp.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rid = rec.get("id")
                    if isinstance(rid, str):
                        seen.add(rid)
        except OSError as e:
            logger.warning("[training_corpus] could not read %s: %s", fp, e)
    return seen


def _iter_all_sources(
    owui_db: Path,
    state_db: Path,
    cc_dir: Path,
) -> Iterable[Record]:
    yield from drain_owui(owui_db)
    yield from drain_state_store(state_db)
    yield from drain_cc_sessions(cc_dir)


def run_drain(
    out_dir: Path | None = None,
    owui_db: Path | None = None,
    state_db: Path | None = None,
    cc_dir: Path | None = None,
) -> dict[str, int]:
    """Synchronous drain. Returns per-source new-record counts."""
    out_dir = out_dir or Path(settings.training_corpus_dir)
    owui_db = owui_db or Path(settings.training_corpus_owui_db)
    state_db = state_db or Path(settings.training_corpus_state_db)
    cc_dir = cc_dir or Path(settings.training_corpus_cc_dir)

    out_dir.mkdir(parents=True, exist_ok=True)
    existing = _load_existing_ids(out_dir)
    target = _monthly_file(out_dir)

    counts: dict[str, int] = {"owui": 0, "state_store": 0, "cc_session": 0}
    new_ids_this_run: set[str] = set()

    # buffering=1 gives line-buffered writes so a crash mid-run preserves
    # everything written up to that point — important for an append-only
    # archive that will never be recovered from source state once the source
    # sqlite / jsonl is rotated.
    with target.open("a", encoding="utf-8", buffering=1) as f:
        for rec in _iter_all_sources(owui_db, state_db, cc_dir):
            if rec.id in existing or rec.id in new_ids_this_run:
                continue
            new_ids_this_run.add(rec.id)
            f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
            counts[rec.source] = counts.get(rec.source, 0) + 1
            TRAINING_CORPUS_RECORDS.labels(source=rec.source).inc()

    total = sum(counts.values())
    logger.info(
        "[training_corpus] drain complete: %d new records (owui=%d state_store=%d cc=%d) → %s",
        total,
        counts["owui"],
        counts["state_store"],
        counts["cc_session"],
        target.name,
    )
    if total >= OVERSIZE_RUN_WARN:
        logger.error(
            "[training_corpus] OVERSIZE RUN: %d new records in one drain "
            "(threshold=%d). Investigate: source injection, dedup miss, or "
            "first-time backfill of a very large history.",
            total,
            OVERSIZE_RUN_WARN,
        )
    return counts


async def drain_training_corpus() -> None:
    """Async wrapper for the APScheduler job."""
    try:
        await asyncio.to_thread(run_drain)
    except Exception:  # noqa: BLE001
        logger.exception("[training_corpus] drain failed")
