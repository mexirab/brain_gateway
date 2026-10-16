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
import re
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


# ---------------------------------------------------------------------------
# Tone-request detector (deterministic, user message only)
# ---------------------------------------------------------------------------
# The model reliably calls voice_style for a tone request that opens a
# conversation, but NOT for follow-ups: the client history it sees carries only
# reply text (no tool calls), so after "say it angry" it imitates having done
# the tone by just repeating the line. This detector reads the latest user
# message before the model runs and sets the one-reply style itself, mapping
# onto the same fixed PRESETS. Lasting changes ("from now on") stay with the
# voice_style tool. Inputs composed by a relay rather than typed/spoken by the
# user (Telegram's "[I sent you a photo…]" + vision description) are skipped.

# (preset, word pattern) — first match wins, so specific phrases come first.
_TONE_WORDS: tuple[tuple[str, str], ...] = (
    ("excited", r"super[- ]?upbeat|super[- ]?happy|excited(?:ly)?|hyped|enthusiastic(?:ally)?"),
    ("whisper", r"whisper(?:ing|ed|y)?"),
    ("sarcastic", r"sarcastic(?:ally)?|sarcasm|deadpan"),
    ("sultry", r"sultry|seductive(?:ly)?|sexy|flirty|flirtatious(?:ly)?"),
    ("angry", r"angr(?:y|ier|ily)|anger|furious(?:ly)?|mad|pissed(?: off)?|enraged|yell(?:ing)?|shout(?:ing)?"),
    ("scared", r"scared|frightened|afraid|fearful(?:ly)?|terrified|fear"),
    ("sad", r"sad(?:ly|der)?|sorrowful(?:ly)?|melancholy|gloomy|depressed|crying|tearful(?:ly)?"),
    ("soothing", r"soothing(?:ly)?"),
    ("gentle", r"soft(?:er|ly)?|gentl(?:e|er|y)|tender(?:ly)?"),
    ("quiet", r"quiet(?:er|ly)?"),
    ("calm", r"calm(?:er|ly)?|relaxed"),
    ("firm", r"firm(?:er|ly)?|stern(?:er|ly)?|strict(?:er|ly)?"),
    ("proud", r"proud(?:ly)?"),
    ("energetic", r"energetic(?:ally)?"),
    ("upbeat", r"upbeat|cheerful(?:ly)?|happ(?:y|ier|ily)|peppy|bubbly"),
    ("warm", r"warm(?:er|ly)?|friendl(?:y|ier)"),
)
_TONE_RE = [(preset, re.compile(rf"\b(?:{words})\b")) for preset, words in _TONE_WORDS]

# The message must explicitly ASK for a delivery, and the tone word must be
# attached to that ask. Describing a feeling or someone else's tone ("I feel like
# you're mad at me", "her tone was sarcastic", "be happy for me") must NOT
# match: speaking a reassurance in an angry voice is the worst failure here.
# Four grammars, each with the tone word in a fixed position:
#  1. delivery verb + object + filler + TONE: "say it [again but] angry",
#     "repeat that sadly", "try it angry", "do it again but angry";
#  2. delivery verb … "like you're / if you were" + TONE:
#     "say the same thing like you're scared", "how would you say it if you were sad";
#  3. "in/use/switch to/go to a" TONE "tone|voice": "switch to a sultry tone";
#  4. imperative be/sound/talk + COMPARATIVE TONE (or "more" TONE, or TONE "for
#     the next response"): "be softer", "now be firmer", "sound more excited".
_DELIVERY = r"(?:say|read|repeat|try|do|tell me)"
_OBJECT = r"(?:it|that|this|those|the same(?: thing)?|the line|that line|something)"
_DELIVERY_OBJ_RE = re.compile(rf"\b{_DELIVERY} {_OBJECT}(?: again)?\b")
_AS_IF_RE = re.compile(
    rf"\b{_DELIVERY}\b[^.!?]{{0,60}}?\b(?:like (?:you'?re|you are|you were|you'?d be)|(?:as )?if you (?:were|are|'re))\b"
)
_TONE_PHRASE_RE = re.compile(r"\b(?:in|with|use|using|try|switch to|go to|change to|into) (?:a|an|your)\s+")
_TONE_NOUN_RE = re.compile(r"[\s-]+(?:tone|voice)\b")
_IMPERATIVE_RE = re.compile(
    r"(?:^|[.!?,;]\s*|\b(?:now|okay|ok|please|can you|could you|will you|would you|try to|let'?s|lets|and)\s+)"
    r"(?:be|sound|talk|speak)\b"
)
_NEXT_REPLY_RE = re.compile(r"\s+for (?:the|your|this) next (?:response|reply|answer|one|line)\b")
# Only filler may sit between a cue and its tone word.
_FILLER_RE = re.compile(
    r"(?:\s|,|again\b|but\b|and\b|now\b|in an?\b|a (?:bit|little)\b|even\b"
    r"|really\b|so\b|very\b|extra\b|all\b|kinda\b|sort of\b)*"
)
# "whisper it", "now whisper…", "read it in a whisper" — the verb is the request.
_WHISPER_CMD_RE = re.compile(
    r"(?:^|[.!?,;]\s*|\b(?:now|okay|ok|please|can you|could you|and)\s+)whisper\b|\bin a whisper\b"
)
# Lasting / negated / reset phrasing is the tool's job (or no change at all);
# writing tasks ("draft a text in a firm tone") are about the text, not her voice;
# "be quiet" means stop talking.
_SKIP_RE = re.compile(
    r"\b(?:from now on|for the (?:next|rest) (?:hour|hours|day|days|evening|night|week)"
    r"|for (?:an?|the|\d+) (?:hour|hours|minutes?|day|days|evening|night)"
    r"|until|always|keep (?:it|talking|being)|anymore"
    r"|don'?t (?:be|sound|say|talk|use)|do not (?:be|sound|say|talk|use)|stop|quit|no more|less"
    r"|back to normal|talk normally|normal (?:voice|tone)|regular (?:voice|tone)"
    r"|draft|write|rewrite|reply to|respond to|e-?mail|letter|message to|text (?:to|him|her|them|my|back)"
    r"|be quiet|quiet for)\b"
)


def _tone_at(text: str, pos: int) -> tuple[str, int]:
    """(preset, end) for a tone word starting at pos (after filler), else ("", pos)."""
    start = _FILLER_RE.match(text, pos).end()
    for preset, rx in _TONE_RE:
        m = rx.match(text, start)
        if m:
            return preset, m.end()
    return "", pos


def detect_tone_request(user_text: str) -> str:
    """Preset key for a one-off tone request in the user's own message, else "".

    "say it angry", "say the same thing like you're scared", "switch to a sultry
    tone", "be softer for the next response" → angry / scared / sultry / gentle.
    """
    if not isinstance(user_text, str) or not user_text.strip() or len(user_text) > 500:
        return ""  # long messages are content (pasted text), not delivery requests
    text = user_text.lower().replace("’", "'").strip()
    if text.startswith("[") or _SKIP_RE.search(text):
        return ""  # "[…]" = composed by a relay (e.g. Telegram photo + vision text)
    if _WHISPER_CMD_RE.search(text):
        return "whisper"
    for rx in (_AS_IF_RE, _DELIVERY_OBJ_RE):  # 2 before 1: "say it if you were sad"
        for m in rx.finditer(text):
            tone, _ = _tone_at(text, m.end())
            if tone:
                return tone
    for m in _TONE_PHRASE_RE.finditer(text):  # 3
        tone, end = _tone_at(text, m.end())  # "a super upbeat tone" -> excited
        if tone and _TONE_NOUN_RE.match(text, end):
            return tone
    for m in _IMPERATIVE_RE.finditer(text):  # 4
        after = _FILLER_RE.match(text, m.end()).end()
        more = text.startswith("more ", after)
        tone, end = _tone_at(text, after + (5 if more else 0))
        if not tone:
            continue
        word = text[after:end].split()[-1]
        if more or word.endswith(("er", "ier")) or _NEXT_REPLY_RE.match(text, end):
            return tone
    return ""


def apply_tone_request(user_text: str) -> str:
    """Detect a tone request and set it as the one-reply style. Returns the key ("" = none).

    Never raises: it runs on every chat turn, ahead of the model.
    """
    if not settings.tts_style_enabled:
        return ""
    try:
        style = detect_tone_request(user_text)
        if style:
            set_one_reply_style(style)
            logger.info("[VOICE_STYLE] Tone request detected: style=%s", style)
        return style
    except Exception as e:
        logger.error("[VOICE_STYLE] Tone detection failed: %s", type(e).__name__)
        return ""


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
