"""
Voice style for TTS — how Jess should *sound*, not what she says.

A style note is a short natural-language instruction ("calm, gentle and
reassuring") sent with each synthesis request. The VoxCPM2 server
(`tts/voxcpm_server.py`) speaks it in the cloned voice; the Qwen3-TTS server
accepts the same `emotion` field and ignores it, so sending a note is always
safe. "auto" (empty note) means no instruction — the model infers prosody
from the text, which is the right default for ordinary replies.

Resolution, highest first:
  1. a one-reply style ("say it angry", `voice_style` scope=this_reply) — set
     mid-turn by the tool, applies to the reply being spoken, and is cleared
     when the next user message is routed (`record_reply_style`) or after the
     reply TTL; RAM only; chat replies only, never announcements;
  2. the user's ongoing override ("talk softer from now on", scope=ongoing) —
     applies to everything she says until reset or until its duration runs out;
     stored in app_state so it survives restarts, expiry checked on read;
  3. the defaults, editable on the /settings Voice Style panel
     (`/api/config/voice_style`, YAML at VOICE_STYLE_PATH):
       * chat replies — by mode_router mode for the user's latest message,
         recorded by cloud_brain (`record_reply_style`) and read back by the
         dashboard / Telegram TTS calls that speak that reply;
       * announcements — by announcement_type.

Every value is a key into PRESETS — never free text from a user, the model,
or a web page — so the only phrases the TTS ever receives are the ones below.
Single-user system: the "current" reply mode is process-global with a TTL,
not per conversation.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from orchestrator.config import settings

logger = logging.getLogger(__name__)

VOICE_STYLE_PATH = os.environ.get("VOICE_STYLE_PATH", "/app/data/voice_style.yaml")
OVERRIDE_KEY = "voice_style_override"

# key → phrase sent to the TTS. "auto" = no note.
PRESETS: Dict[str, str] = {
    "auto": "",
    "calm": "calm and relaxed",
    "gentle": "gentle, soft and reassuring",
    "soothing": "very calm, soft and slow, soothing",
    "quiet": "soft and quiet",
    "warm": "warm and friendly",
    "thoughtful": "warm, thoughtful and unhurried",
    "encouraging": "warm and encouraging",
    "upbeat": "warm, upbeat and cheerful",
    "proud": "warm, upbeat and proud",
    "energetic": "energetic and enthusiastic",
    "firm": "firm, direct, no-nonsense but kind",
    # Acting tones — mostly for one-off "say it angry" requests.
    "excited": "super upbeat, excited and cheerful",
    "sad": "sad, downcast and quiet",
    "angry": "angry and furious, raised voice",
    "scared": "frightened, scared and trembling",
    "whisper": "whispering, very quiet and close",
    "sultry": "low, breathy and sultry",
    "sarcastic": "dry, deadpan and sarcastic",
}
# Acting tones never reach announcements via the chat override: a "whisper" or
# "angry" override must not make a medication reminder inaudible or shouted.
# (The settings panel can still assign them per type — an explicit owner choice.)
ACTING_PRESETS = frozenset({"excited", "sad", "angry", "scared", "whisper", "sultry", "sarcastic"})

# mode_router modes. explainer is the router's fallback for every
# non-emotional message, so it defaults to auto (model reads the text).
MODES = ("explainer", "mirror", "counterbalance", "challenge", "baseline")
DEFAULT_MODE_STYLES: Dict[str, str] = {
    "explainer": "auto",
    "mirror": "thoughtful",
    "counterbalance": "gentle",
    "challenge": "firm",
    "baseline": "soothing",
}

# announcement_type values passed to reminder_manager._announce_voice.
ANNOUNCEMENT_TYPES = (
    "reminder",
    "calendar",
    "selfcare",
    "progress",
    "routine",
    "focus",
    "briefing",  # morning briefing AND evening briefing / wind-down
    "greeting",
    "ambient",
    "interrupt",
    "email",
    "finance",
    "manual",
    "temperature",  # GPU/system temperature warnings (jobs_monitoring)
)
# Types not listed (e.g. "unknown", "test") resolve to "auto".
DEFAULT_ANNOUNCEMENT_STYLES: Dict[str, str] = {
    **{t: "auto" for t in ANNOUNCEMENT_TYPES},
    "progress": "proud",
    "greeting": "warm",
    "briefing": "warm",
    "selfcare": "gentle",
    "routine": "encouraging",
    "ambient": "quiet",
}

# How long a chat turn's mode applies to the TTS calls that follow it. The
# dashboard speaks a reply sentence by sentence as it streams; a long reply
# can take a minute or two to finish playing.
REPLY_STYLE_TTL_SECONDS = 300.0
MAX_OVERRIDE_HOURS = 24 * 7

_reply_mode: str = ""
_reply_mode_at: float = 0.0
_one_reply_style: str = ""  # preset key, "" = none
_one_reply_at: float = 0.0
_lock = threading.RLock()
_cache: Optional[Dict[str, Dict[str, str]]] = None


# ---------------------------------------------------------------------------
# Defaults (YAML, /settings panel)
# ---------------------------------------------------------------------------


def _lenient_map(raw: Any, allowed: tuple) -> Dict[str, str]:
    """Keep the valid entries of a saved map and drop the rest (a preset renamed
    in a later release shouldn't discard every other saved choice)."""
    if not isinstance(raw, dict):
        return {}
    kept = {k: v for k, v in raw.items() if k in allowed and isinstance(v, str) and v in PRESETS}
    if len(kept) != len(raw):
        logger.warning("[VOICE_STYLE] Ignored %d invalid saved entries", len(raw) - len(kept))
    return kept


def _validate_map(raw: Any, allowed: tuple, field: str) -> Dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{field} must be an object")
    out: Dict[str, str] = {}
    for key, value in raw.items():
        if key not in allowed:
            raise ValueError(f"{field}: unknown key {key!r}. Allowed: {list(allowed)}")
        if not isinstance(value, str) or value not in PRESETS:
            raise ValueError(f"{field}.{key}: unknown style {value!r}. Allowed: {list(PRESETS)}")
        out[key] = value
    return out


def validate(data: Any) -> Dict[str, Dict[str, str]]:
    """Validate a {modes, announcements} payload (partial maps allowed)."""
    if not isinstance(data, dict):
        raise ValueError("voice_style payload must be an object")
    unknown = sorted(set(data) - {"modes", "announcements"})
    if unknown:
        raise ValueError(f"unknown fields {unknown}; expected 'modes' and/or 'announcements'")
    return {
        "modes": _validate_map(data.get("modes"), MODES, "modes"),
        "announcements": _validate_map(data.get("announcements"), ANNOUNCEMENT_TYPES, "announcements"),
    }


def load_defaults() -> Dict[str, Dict[str, str]]:
    """Effective defaults: built-ins overlaid with the saved YAML."""
    global _cache
    with _lock:
        if _cache is None:
            saved: Dict[str, Any] = {}
            path = Path(VOICE_STYLE_PATH)
            if path.exists():
                try:
                    with open(path) as f:
                        saved = yaml.safe_load(f) or {}
                except (OSError, yaml.YAMLError) as e:
                    logger.error("[VOICE_STYLE] Bad config at %s (%s); using built-in defaults", path, type(e).__name__)
                if not isinstance(saved, dict):
                    saved = {}
            _cache = {
                "modes": {**DEFAULT_MODE_STYLES, **_lenient_map(saved.get("modes"), MODES)},
                "announcements": {
                    **DEFAULT_ANNOUNCEMENT_STYLES,
                    **_lenient_map(saved.get("announcements"), ANNOUNCEMENT_TYPES),
                },
            }
        return deepcopy(_cache)


def save_defaults(data: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """Merge a validated partial update into the saved YAML and return the effective map."""
    global _cache
    from orchestrator.config_writer import atomic_write_yaml

    update = validate(data)
    with _lock:  # read-merge-write as one step so concurrent saves can't drop an edit
        current = load_defaults()
        merged = {
            "modes": {**current["modes"], **update["modes"]},
            "announcements": {**current["announcements"], **update["announcements"]},
        }
        # Persist only choices that differ from the built-ins, so a later release
        # can improve a default without being shadowed by a stale copy.
        atomic_write_yaml(
            VOICE_STYLE_PATH,
            {
                "modes": {k: v for k, v in merged["modes"].items() if v != DEFAULT_MODE_STYLES[k]},
                "announcements": {
                    k: v for k, v in merged["announcements"].items() if v != DEFAULT_ANNOUNCEMENT_STYLES[k]
                },
            },
        )
        _cache = None
        return load_defaults()


# ---------------------------------------------------------------------------
# Override (chat tool)
# ---------------------------------------------------------------------------


def get_override() -> Optional[Dict[str, Any]]:
    """Active override {"style", "until"} (until = epoch seconds or None), or None."""
    from orchestrator import state_store

    try:
        raw = state_store.get_app_state(OVERRIDE_KEY)
    except Exception as e:
        logger.warning("[VOICE_STYLE] override read failed: %s", type(e).__name__)
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        style, until = data["style"], data.get("until")
        until = None if until is None else float(until)
        if until is not None and not math.isfinite(until):
            raise ValueError("non-finite until")
    except (ValueError, KeyError, TypeError):
        clear_override()
        return None
    if not isinstance(style, str) or style not in PRESETS or (until is not None and time.time() >= until):
        clear_override()
        return None
    return {"style": style, "until": until}


def _audit_override(before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]], changed_by: str) -> None:
    from orchestrator.config_writer import log_config_change

    try:
        log_config_change("voice_style_override", before or {}, after or {}, changed_by=changed_by)
    except Exception as e:
        logger.warning("[VOICE_STYLE] audit write failed: %s", type(e).__name__)


def set_override(style: str, hours: Optional[float] = None, changed_by: str = "chat") -> Dict[str, Any]:
    from orchestrator import state_store

    if not isinstance(style, str) or style not in PRESETS:
        raise ValueError(f"unknown style {style!r}")
    until = None
    if hours is not None:
        if isinstance(hours, bool):
            raise ValueError("duration_hours must be a number")
        hours = float(hours)
        if not 0 < hours <= MAX_OVERRIDE_HOURS:
            raise ValueError(f"duration_hours must be between 0 and {MAX_OVERRIDE_HOURS}")
        until = time.time() + hours * 3600
    before = get_override()
    override = {"style": style, "until": until}
    state_store.set_app_state(OVERRIDE_KEY, json.dumps(override))
    _audit_override(before, override, changed_by)
    return override


def reset_override(changed_by: str) -> None:
    """User-initiated reset (chat tool or settings panel) — audited."""
    before = get_override()
    clear_override()
    if before:
        _audit_override(before, None, changed_by)


def clear_override() -> None:
    """Remove the override (also used internally for expired/corrupt entries)."""
    from orchestrator import state_store

    try:
        state_store.delete_app_state(OVERRIDE_KEY)
    except Exception as e:
        logger.warning("[VOICE_STYLE] override clear failed: %s", type(e).__name__)


# ---------------------------------------------------------------------------
# Resolution (TTS call sites)
# ---------------------------------------------------------------------------


def _phrase(key: Optional[str]) -> str:
    return PRESETS.get(key or "auto", "")


def record_reply_style(mode: str) -> None:
    """Remember the mode of the message just routed, for speaking its reply.

    A new user message also ends any one-reply style from the previous turn.
    """
    global _reply_mode, _reply_mode_at, _one_reply_style
    with _lock:
        _reply_mode = mode if mode in MODES else ""
        _reply_mode_at = time.monotonic()
        _one_reply_style = ""


def set_one_reply_style(style: str) -> None:
    """Style for the reply currently being generated only (voice_style scope=this_reply)."""
    global _one_reply_style, _one_reply_at
    if not isinstance(style, str) or style not in PRESETS:
        raise ValueError(f"unknown style {style!r}")
    with _lock:
        _one_reply_style = style
        _one_reply_at = time.monotonic()


def get_one_reply_style() -> str:
    """Active one-reply style key, or "" (also "" once the reply TTL has passed)."""
    with _lock:
        if _one_reply_style and time.monotonic() - _one_reply_at <= REPLY_STYLE_TTL_SECONDS:
            return _one_reply_style
        return ""


def clear_one_reply_style() -> None:
    global _one_reply_style
    with _lock:
        _one_reply_style = ""


def _count(source: str, key: str) -> str:
    """Count the resolved style (bounded labels: 5 sources x len(PRESETS)) and return its phrase."""
    from orchestrator.metrics import TTS_STYLE_APPLIED

    key = key if key in PRESETS else "auto"
    TTS_STYLE_APPLIED.labels(source=source, style=key).inc()
    return _phrase(key)


def current_reply_style() -> str:
    """Style phrase for speaking the latest chat reply ("" = no note)."""
    if not settings.tts_style_enabled:
        return ""
    one_reply = get_one_reply_style()
    if one_reply:
        return _count("this_reply", one_reply)
    override = get_override()
    if override:
        return _count("override", override["style"])
    if not _reply_mode or time.monotonic() - _reply_mode_at > REPLY_STYLE_TTL_SECONDS:
        return _count("none", "auto")
    return _count("mode", load_defaults()["modes"].get(_reply_mode, "auto"))


def style_for_announcement(announcement_type: str) -> str:
    """Style phrase for a proactive announcement ("" = no note)."""
    if not settings.tts_style_enabled:
        return ""
    override = get_override()
    if override and override["style"] not in ACTING_PRESETS:
        return _count("override", override["style"])
    return _count("announcement", load_defaults()["announcements"].get(announcement_type, "auto"))
