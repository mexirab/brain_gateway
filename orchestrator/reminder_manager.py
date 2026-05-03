"""
Reminder Manager for Brain Gateway
Handles voice reminder scheduling, storage, and delivery via Home Assistant.
"""

import asyncio
import hashlib
import hmac
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from orchestrator import state_store
from orchestrator.user_profile import get_profile

logger = logging.getLogger(__name__)

# Home Assistant config — sourced from centralized settings
from orchestrator.config import settings as _settings

HA_URL = _settings.ha_url
HA_TOKEN = _settings.ha_token

# Orchestrator URL (for HA to call back)
ORCHESTRATOR_URL = _settings.orchestrator_url

# Delivery targets (configurable via env and profile)
_profile = get_profile()
REMINDER_SPEAKER = os.environ.get("REMINDER_SPEAKER", _profile.default_speaker)
# Support both single service (backward compat) and list of services
_NOTIFY_SERVICE_RE = re.compile(r"^[a-zA-Z0-9_]+\.[a-zA-Z0-9_]+$")
_MAX_NOTIFY_SERVICES = 10
_raw_services = _profile.mobile_notify_services or (
    [_profile.mobile_notify_service] if _profile.mobile_notify_service else []
)
MOBILE_NOTIFY_SERVICES: list[str] = [
    s for s in _raw_services[:_MAX_NOTIFY_SERVICES] if isinstance(s, str) and _NOTIFY_SERVICE_RE.match(s)
]


# =============================================================================
# TIME PARSING
# =============================================================================


def parse_time_expression(time_str: str) -> Tuple[Optional[datetime], Optional[str]]:
    """
    Parse a time expression into a datetime.

    Supports:
    - "in X minutes" / "in X min"
    - "in X hours" / "in X hour"
    - "at 3pm" / "at 3:30pm" / "at 15:00"
    - "HH:MM" (24-hour format)

    Returns: (datetime, error_message)
    """
    time_str = time_str.strip().lower()
    now = datetime.now()

    # Handle "in X minutes"
    match = re.match(r"in\s+(\d+)\s*(?:min(?:utes?)?|m)\b", time_str)
    if match:
        minutes = int(match.group(1))
        target = now + timedelta(minutes=minutes)
        return (target, None)

    # Handle "in X hours"
    match = re.match(r"in\s+(\d+)\s*(?:hours?|h)\b", time_str)
    if match:
        hours = int(match.group(1))
        target = now + timedelta(hours=hours)
        return (target, None)

    # Handle "in X hours and Y minutes"
    match = re.match(r"in\s+(\d+)\s*(?:hours?|h)\s*(?:and\s+)?(\d+)\s*(?:min(?:utes?)?|m)", time_str)
    if match:
        hours = int(match.group(1))
        minutes = int(match.group(2))
        target = now + timedelta(hours=hours, minutes=minutes)
        return (target, None)

    # Handle "at 3pm" / "at 3:30pm" / "at 3:30 pm"
    match = re.match(r"(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", time_str)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2)) if match.group(2) else 0
        period = match.group(3)

        if period == "pm" and hour != 12:
            hour += 12
        elif period == "am" and hour == 12:
            hour = 0

        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # If the time has already passed today, schedule for tomorrow
        if target <= now:
            target += timedelta(days=1)

        return (target, None)

    # Handle 24-hour format "HH:MM" or "at HH:MM"
    match = re.match(r"(?:at\s+)?(\d{1,2}):(\d{2})(?!\s*[ap]m)", time_str)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))

        if hour > 23 or minute > 59:
            return (None, f"Invalid time: {time_str}")

        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # If the time has already passed today, schedule for tomorrow
        if target <= now:
            target += timedelta(days=1)

        return (target, None)

    return (None, f"Could not parse time: '{time_str}'. Try 'in 5 minutes', 'at 3pm', or '14:30'.")


def format_time_friendly(dt: datetime) -> str:
    """Format a datetime into a friendly spoken format."""
    now = datetime.now()

    # Calculate difference
    diff = dt - now

    if diff.total_seconds() < 60:
        return "less than a minute from now"
    elif diff.total_seconds() < 3600:
        minutes = int(diff.total_seconds() / 60)
        return f"in {minutes} minute{'s' if minutes != 1 else ''}"
    elif diff.total_seconds() < 7200:
        hours = int(diff.total_seconds() / 3600)
        minutes = int((diff.total_seconds() % 3600) / 60)
        if minutes > 0:
            return f"in {hours} hour and {minutes} minute{'s' if minutes != 1 else ''}"
        return f"in {hours} hour"
    else:
        # Format as actual time
        return f"at {dt.strftime('%-I:%M %p')}"


# =============================================================================
# PERSISTENT REMINDER STORAGE (SQLite via state_store)
# =============================================================================


def add_reminder(reminder_id: str, text: str, trigger_time: datetime, target: str = "both") -> Dict[str, Any]:
    """Add a new reminder to persistent storage."""
    state_store.save_reminder(reminder_id, text, trigger_time.isoformat(), target)
    reminder = {
        "id": reminder_id,
        "text": text,
        "time": trigger_time.strftime("%Y-%m-%d %H:%M"),
        "time_display": trigger_time.strftime("%-I:%M %p"),
        "target": target,
        "created": datetime.now().isoformat(),
        "status": "pending",
    }
    logger.info(f"Added reminder {reminder_id}: '{text}' at {trigger_time}")
    return reminder


def get_reminder(reminder_id: str) -> Optional[Dict[str, Any]]:
    """Get a single reminder by ID."""
    return state_store.get_reminder(reminder_id)


def remove_reminder(reminder_id: str) -> bool:
    """Remove a reminder by ID."""
    return state_store.delete_reminder(reminder_id)


def mark_reminder_completed(reminder_id: str) -> bool:
    """Mark a reminder as completed."""
    return state_store.complete_reminder(reminder_id)


def list_pending_reminders() -> List[Dict[str, Any]]:
    """Get all pending reminders."""
    return state_store.get_pending_reminders()


# =============================================================================
# REMINDER DELIVERY HELPERS
# =============================================================================


async def _announce_voice(
    text: str,
    speaker: str | None = None,
    announcement_type: str = "unknown",
    min_volume: float | None = None,
) -> Dict[str, Any]:
    """
    Announce via TTS on a speaker (defaults to REMINDER_SPEAKER).

    Generates full audio, saves to disk, serves via HTTP, and plays on an HA media_player.

    Args:
        text: The text to announce.
        speaker: Target speaker entity or room name (defaults to REMINDER_SPEAKER).
        announcement_type: Category for tracking (calendar, reminder, focus, progress, ambient, etc.).
        min_volume: If set (0.0–1.0), bump each target speaker's volume up to this
            level before play_media when its current volume is lower. Used by
            wake-time announcements (morning briefing, morning routine) to defeat
            "speaker still at sleep-sound volume" — see the 2026-04-30 incident
            where the briefing played at volume_level=0.10 and was inaudible.
            Doesn't lower an already-loud speaker. Failures are logged and the
            announcement still plays at whatever volume the speaker has.
    """
    import time as _time

    from orchestrator import shared

    t0 = _time.time()

    # Do Not Disturb — suppress all announcements when user said goodnight
    if shared.DND_ACTIVE:
        logger.info(f"[DND] Suppressed announcement ({announcement_type}): {text[:60]}")
        return {"success": True, "suppressed": True, "reason": "dnd_active"}

    # Mid-conversation guard — if the user is in an active voice session with
    # Jess (OWUI mic or HA Assist), suppress the announcement. Firing an
    # announcement over the house speakers while the user is talking to Jess
    # in the browser both (a) confuses the user — two Jesses, different rooms,
    # crosstalk — and (b) piles onto Qwen3-TTS, which is already handling
    # per-sentence synthesis for the live reply and will time out under
    # contention. VOICE_SESSION_WINDOW_SEC defaults to 60s.
    if shared.is_voice_session_active():
        logger.info(
            "[VOICE-ACTIVE] Suppressed announcement (%s): %s",
            announcement_type,
            text[:60],
        )
        return {"success": True, "suppressed": True, "reason": "voice_session_active"}

    try:
        backend = shared.tts_backend
        if backend is None:
            _record_announcement(text, announcement_type, None, False, "TTS backend not initialized", None)
            return {"success": False, "error": "TTS backend not initialized"}

        # =====================================================================
        # Cast delivery path — generate audio, serve via HTTP, play on HA media_player
        # =====================================================================
        headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}

        # Generate audio via backend
        audio_bytes = await backend.synthesize(text)

        # Save audio with UUID
        audio_id = str(uuid.uuid4())[:8]
        audio_dir = "/tmp/brain_audio"
        os.makedirs(audio_dir, exist_ok=True)
        ext = backend.file_extension
        audio_path = f"{audio_dir}/{audio_id}.{ext}"

        with open(audio_path, "wb") as f:
            f.write(audio_bytes)

        audio_url = f"{ORCHESTRATOR_URL}/api/audio/{audio_id}.{ext}"

        # Build speaker list.
        # The caller may pass:
        #   - a single entity_id       -> wrapped as [entity_id]
        #   - a comma-separated string -> split into a list
        #   - the literal "all"        -> alias for the configured route
        #   - None / empty             -> consult announcement_routes.route_for(announcement_type)
        # The route lookup itself falls back to the legacy REMINDER_SPEAKER
        # env var when no per-category override is configured (preserving
        # pre-Speakers-panel behavior).
        def _split_speakers(value: str) -> list[str]:
            return [s.strip() for s in value.split(",") if s.strip()]

        if speaker and speaker.strip().lower() != "all":
            broadcast_speakers = _split_speakers(speaker)
        else:
            from orchestrator.announcement_routes import route_for as _route_for

            routed = _route_for(announcement_type)
            broadcast_speakers = _split_speakers(routed) if routed else _split_speakers(REMINDER_SPEAKER)

        if not broadcast_speakers:
            err = (
                f"No speakers configured for announcement_type={announcement_type!r} "
                f"(check Speakers panel or REMINDER_SPEAKER env var)"
            )
            logger.error(err)
            _record_announcement(text, announcement_type, None, False, err, None)
            return {"success": False, "error": err}

        # Cast to all target speakers (don't stop at first success)
        succeeded = []
        last_error = None
        async with httpx.AsyncClient(timeout=30) as client:
            # Optional: bump-only volume floor for wake-time announcements.
            # Done before play_media so the speaker wakes from `off` already at
            # the right volume. Failures are logged and never block the play.
            if min_volume is not None:
                clamped = max(0.0, min(1.0, float(min_volume)))
                for try_speaker in broadcast_speakers:
                    try:
                        cur = await client.get(
                            f"{HA_URL}/api/states/{try_speaker}",
                            headers=headers,
                        )
                        current_vol = None
                        if cur.status_code == 200:
                            current_vol = (cur.json().get("attributes") or {}).get("volume_level")
                        if current_vol is None or float(current_vol) < clamped:
                            vol_resp = await client.post(
                                f"{HA_URL}/api/services/media_player/volume_set",
                                headers=headers,
                                json={"entity_id": try_speaker, "volume_level": clamped},
                            )
                            if vol_resp.status_code == 200:
                                logger.info(
                                    f"[VOLUME] Bumped {try_speaker}: "
                                    f"{current_vol if current_vol is not None else 'unknown'} -> {clamped} "
                                    f"({announcement_type})"
                                )
                            else:
                                logger.warning(
                                    f"[VOLUME] volume_set returned {vol_resp.status_code} for {try_speaker}"
                                )
                    except Exception as vol_err:  # noqa: BLE001
                        logger.warning(f"[VOLUME] floor check failed for {try_speaker}: {vol_err}")

            for try_speaker in broadcast_speakers:
                try:
                    ha_response = await client.post(
                        f"{HA_URL}/api/services/media_player/play_media",
                        headers=headers,
                        json={
                            "entity_id": try_speaker,
                            "media_content_id": audio_url,
                            "media_content_type": backend.audio_format,
                        },
                    )

                    if ha_response.status_code == 200:
                        logger.info(f"Played announcement on {try_speaker}")
                        succeeded.append(try_speaker)
                    else:
                        last_error = f"HA returned {ha_response.status_code} for {try_speaker}"
                        logger.warning(f"play_media failed: {last_error}")
                except Exception as speaker_err:
                    last_error = f"Connection error for {try_speaker}: {speaker_err}"
                    logger.warning(f"play_media failed: {last_error}")

        latency_ms = int((_time.time() - t0) * 1000)
        if succeeded:
            speaker_label = ",".join(succeeded)
            _record_announcement(text, announcement_type, speaker_label, True, None, latency_ms)
            return {"success": True, "speaker": speaker_label}

        _record_announcement(
            text,
            announcement_type,
            broadcast_speakers[0] if broadcast_speakers else "unknown",
            False,
            last_error,
            latency_ms,
        )
        return {"success": False, "error": last_error}

    except Exception as e:
        latency_ms = int((_time.time() - t0) * 1000)
        # Include exception type because some common failures (httpx.ReadTimeout,
        # httpx.ReadError) have empty str(), which otherwise logs as the useless
        # "Voice announcement failed: " with no signal. logger.exception also
        # attaches the traceback so we can see which call path failed.
        err_repr = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        logger.exception("Voice announcement failed (%s)", err_repr)
        _record_announcement(text, announcement_type, None, False, err_repr, latency_ms)
        return {"success": False, "error": err_repr}


def _record_announcement(
    text: str,
    announcement_type: str,
    speaker: str | None,
    success: bool,
    error: str | None,
    latency_ms: int | None,
) -> None:
    """Record announcement to DB and metrics (fire-and-forget)."""
    try:
        state_store.record_announcement(
            text=text,
            announcement_type=announcement_type,
            speaker=speaker,
            success=success,
            error=error,
            latency_ms=latency_ms,
        )
    except Exception as e:
        logger.warning(f"Failed to record announcement: {e}")

    try:
        from orchestrator.metrics import TTS_ANNOUNCEMENTS_TOTAL, TTS_ERRORS_TOTAL, TTS_LATENCY

        # Sanitize speaker label to prevent Prometheus cardinality explosion from untrusted input
        safe_speaker = re.sub(r"[^a-zA-Z0-9_:.\-]", "_", speaker or "none")[:50]
        TTS_ANNOUNCEMENTS_TOTAL.labels(
            type=announcement_type,
            speaker=safe_speaker,
            success="true" if success else "false",
        ).inc()

        if latency_ms is not None:
            TTS_LATENCY.observe(latency_ms / 1000)

        if not success and error:
            if "HA returned" in error:
                error_type = "ha_error"
            elif "Connection" in error:
                error_type = "connection"
            else:
                error_type = "tts_error"
            TTS_ERRORS_TOTAL.labels(error_type=error_type).inc()
    except Exception:
        pass


_raw_webui_url = os.environ.get("WEBUI_URL", "")
WEBUI_URL = _raw_webui_url if re.match(r"^https?://", _raw_webui_url) else ""
if _raw_webui_url and not WEBUI_URL:
    logger.warning(f"[SECURITY] WEBUI_URL rejected (not http/https): {_raw_webui_url!r}")


async def _send_notification(text: str) -> Dict[str, Any]:
    """Send a mobile push notification to all configured phones via HA Companion App.

    Includes a deep link to Open WebUI so tapping the notification opens the
    chat interface. Works with both iOS and Android Companion Apps.
    Fans out to all services in MOBILE_NOTIFY_SERVICES concurrently.
    """
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}

    if not MOBILE_NOTIFY_SERVICES:
        logger.warning("No mobile_notify_services configured, skipping notification")
        return {"success": False, "error": "No mobile notification services configured"}

    # Build notification data with deep link for both platforms
    notification_data: Dict[str, Any] = {
        # iOS (HA Companion)
        "push": {"sound": "default", "interruption-level": "time-sensitive"},
    }
    if WEBUI_URL:
        # iOS: opens URL when notification is tapped
        notification_data["url"] = WEBUI_URL
        # Android: opens URL when notification is tapped
        notification_data["clickAction"] = WEBUI_URL

    payload = {
        "message": text,
        "title": _profile.notification_title,
        "data": notification_data,
    }

    async def _post_one(client: httpx.AsyncClient, service: str) -> tuple[str, bool, str | None]:
        service_path = service.replace(".", "/", 1)
        try:
            response = await client.post(
                f"{HA_URL}/api/services/{service_path}",
                headers=headers,
                json=payload,
            )
            if response.status_code == 200:
                return service, True, None
            return service, False, f"{service}: HA returned {response.status_code}"
        except Exception as e:
            return service, False, f"{service}: {e}"

    successes = []
    errors = []

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            results = await asyncio.gather(*[_post_one(client, s) for s in MOBILE_NOTIFY_SERVICES])
        for svc, ok, err in results:
            if ok:
                successes.append(svc)
            else:
                errors.append(err)

    except Exception as e:
        logger.error(f"Mobile notification failed: {e}")
        return {"success": False, "error": str(e)}

    if successes:
        logger.info(f"Sent notification to {len(successes)} phone(s): {text[:50]}...")
    if errors:
        logger.warning(f"Notification failed for: {errors}")

    return {"success": len(successes) > 0, "delivered": successes, "errors": errors}


# =============================================================================
# NTFY FEEDBACK LOOP (F-011)
# =============================================================================
# Third delivery channel. Pushes reminders to an ntfy topic with HMAC-signed
# Done/Snooze action buttons. Tapping a button on the phone POSTs back to the
# orchestrator, which closes the loop (selfcare bridge on ack, reschedule on
# snooze). Runs alongside TTS + HA Companion push — not a replacement.


def _sign_callback(reminder_id: str, action: str, exp: int, extra: str = "") -> str:
    """HMAC-SHA256 over "reminder_id|action|exp|extra", truncated to 32 hex chars.

    `extra` lets callers bind additional fields into the signature so they
    can't be tampered with in transit — e.g. snooze URLs include the
    minutes value there so an attacker who grabs a valid URL can't replay
    it with a different minutes param. `extra` is empty for ack.

    Truncation is intentional: 128 bits of keyspace is more than enough for
    a single-reminder, 30-minute-window secret, and short sigs keep the ntfy
    payload compact. Secret is `settings.ntfy_hmac_secret`.
    """
    secret = _settings.ntfy_hmac_secret.encode("utf-8")
    msg = f"{reminder_id}|{action}|{exp}|{extra}".encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()[:32]


def verify_callback_signature(
    reminder_id: str, action: str, exp: int, sig: str, extra: str = ""
) -> Optional[str]:
    """Validate an ntfy callback URL. Returns None on success, error string otherwise.

    Separate function so tests can exercise it without a live request.
    Constant-time compare to avoid timing oracle on the truncated HMAC.
    Callers that signed with an `extra` field MUST pass the same value here.
    """
    if not _settings.ntfy_hmac_secret:
        return "signing_disabled"
    if exp < int(time.time()):
        return "expired"
    expected = _sign_callback(reminder_id, action, exp, extra)
    if not hmac.compare_digest(expected, sig):
        return "bad_signature"
    return None


def _build_callback_url(
    reminder_id: str,
    action: str,
    extra_params: Optional[Dict[str, str]] = None,
    signed_extra: str = "",
) -> str:
    """Build a signed callback URL for an ntfy action button.

    `signed_extra` is bound into the HMAC so its corresponding query param
    is tamper-evident (used for snooze's `minutes`).
    """
    exp = int(time.time()) + _settings.ntfy_ack_exp_seconds
    sig = _sign_callback(reminder_id, action, exp, signed_extra)
    base = _settings.ntfy_callback_base_url.rstrip("/")
    # Path-segment-encode reminder_id so ids with weird chars don't break the URL.
    rid = quote(reminder_id, safe="")
    url = f"{base}/api/reminder/{action}/{rid}?sig={sig}&exp={exp}"
    if extra_params:
        for key, val in extra_params.items():
            url += f"&{quote(key, safe='')}={quote(str(val), safe='')}"
    return url


async def deliver_via_ntfy(reminder_id: str, text: str, priority: Optional[int] = None) -> Dict[str, Any]:
    """Push a reminder to the ntfy topic with Done/Snooze action buttons.

    Fire-and-forget semantics: always returns a dict, never raises. Failures
    log and increment the fail metric but don't block the main reminder
    delivery path. No-op (returns `skipped=True`) when `NTFY_ENABLED=false`
    or required config is missing.
    """
    # Import here to avoid circular import at module load (metrics imports shared)
    from orchestrator.metrics import NTFY_PUSH_LATENCY, NTFY_PUSH_TOTAL

    if not _settings.ntfy_enabled:
        return {"success": False, "skipped": True, "reason": "disabled"}

    missing = [
        name
        for name, val in (
            ("NTFY_URL", _settings.ntfy_url),
            ("NTFY_CALLBACK_BASE_URL", _settings.ntfy_callback_base_url),
            ("NTFY_HMAC_SECRET", _settings.ntfy_hmac_secret),
        )
        if not val
    ]
    if missing:
        logger.warning(f"[NTFY] Enabled but missing config: {missing}; skipping push")
        NTFY_PUSH_TOTAL.labels(result="skipped", kind="reminder").inc()
        return {"success": False, "skipped": True, "reason": f"missing:{','.join(missing)}"}

    done_url = _build_callback_url(reminder_id, "ack")
    # Bind minutes into the HMAC so an eavesdropper on the open-tailnet ntfy
    # topic can't replay with a modified minutes value to burn the snooze
    # budget.
    snooze_url = _build_callback_url(
        reminder_id, "snooze", {"minutes": "10"}, signed_extra="10"
    )

    # ntfy action-button format: semicolon-separated, comma-separated fields within
    # See https://docs.ntfy.sh/publish/#action-buttons
    actions_header = f"http, Done, {done_url}, clear=true; http, Snooze 10m, {snooze_url}, clear=true"

    push_prio = priority if priority is not None else _settings.ntfy_default_priority
    push_prio = max(1, min(5, push_prio))

    url = f"{_settings.ntfy_url.rstrip('/')}/{_settings.ntfy_topic}"
    headers = {
        "Title": "Jess reminder",
        "Priority": str(push_prio),
        "Tags": "bell",
        "Actions": actions_header,
    }

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, content=text.encode("utf-8"), headers=headers)
        latency = time.time() - t0
        NTFY_PUSH_LATENCY.labels(kind="reminder").observe(latency)
        if resp.status_code in (200, 201, 202):
            logger.info(f"[NTFY] Pushed reminder {reminder_id} ({len(text)} chars, prio={push_prio})")
            NTFY_PUSH_TOTAL.labels(result="ok", kind="reminder").inc()
            return {"success": True, "latency_ms": int(latency * 1000)}
        logger.warning(f"[NTFY] Push returned {resp.status_code}: {resp.text[:200]}")
        NTFY_PUSH_TOTAL.labels(result="fail", kind="reminder").inc()
        return {"success": False, "status_code": resp.status_code, "body": resp.text[:200]}
    except Exception as e:
        NTFY_PUSH_TOTAL.labels(result="fail", kind="reminder").inc()
        logger.error(f"[NTFY] Push failed: {type(e).__name__}: {e}")
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


def infer_selfcare_action_from_text(text: str) -> Optional[str]:
    """Return the selfcare action a reminder body corresponds to, or None.

    Case-insensitive word-boundary match against the canonical
    selfcare_manager.ACTION_KEYWORDS map (shared with the routine-step
    matcher). First hit wins; iteration order is medication > meal > water
    > movement, reflecting how painful a missed one is (loosely). Used by
    the ack route to fire the selfcare bridge.
    """
    if not text:
        return None
    # Import here so this module has no hard dependency on selfcare_manager
    # at load time (both modules import state_store; selfcare_manager also
    # imports routine_manager — lazy import prevents cycles).
    from orchestrator.selfcare_manager import ACTION_KEYWORDS

    haystack = text.lower()
    for action, keywords in ACTION_KEYWORDS.items():
        for kw in keywords:
            if re.search(rf"\b{re.escape(kw)}\b", haystack):
                return action
    return None


async def deliver_ack_confirm(
    title: str, message: str, reminder_id: Optional[str] = None
) -> Dict[str, Any]:
    """Push a low-priority confirmation notification (F-011 ack/snooze feedback).

    Fire-and-forget: called from the ack/snooze routes via
    `asyncio.create_task` after the state mutation succeeds. Never
    raises. Gated by both `ntfy_enabled` (top-level kill-switch) AND
    `ntfy_confirm_enabled` (opt-in for this side-channel specifically,
    since some users will find the follow-up notification noisy).

    Uses priority=1 (iOS delivers quietly / auto-summarizes) and no
    action buttons. Separate from `deliver_via_ntfy` because the
    shape is intentionally different — we don't want a Done button
    on the confirmation itself.

    **Privacy note (security review finding):** the title stays
    generic (never contains action category like "Medication logged")
    because the ntfy topic is open-tailnet and titles are visible on
    lockscreens/notification lists without the body. Action-specific
    detail belongs in the body only. Callers already constrain the
    title on their side; we also enforce a 120-char cap here.
    """
    from orchestrator.metrics import NTFY_PUSH_TOTAL

    if not _settings.ntfy_enabled or not _settings.ntfy_confirm_enabled:
        return {"success": False, "skipped": True, "reason": "disabled"}

    if not _settings.ntfy_url:
        NTFY_PUSH_TOTAL.labels(result="skipped", kind="confirm").inc()
        return {"success": False, "skipped": True, "reason": "missing_url"}

    url = f"{_settings.ntfy_url.rstrip('/')}/{_settings.ntfy_topic}"
    # httpx defaults to ASCII-encoded headers; non-ASCII chars (emoji U+2713,
    # U+1F4A4, etc.) would crash the POST before it ever hits the wire. ntfy
    # server decodes Title as UTF-8, so we build the headers as a list of
    # (name, utf-8 bytes) tuples — httpx will accept raw bytes unchanged.
    # The 120-char cap is on the pre-encoded string so the UTF-8 byte length
    # can be larger (but still reasonable).
    title_bytes = title[:120].encode("utf-8")
    headers = [
        ("Title", title_bytes),  # cap so a huge title can't bloat the push
        ("Priority", b"1"),
        ("Tags", b"white_check_mark"),
    ]

    # Include reminder_id in failure logs for Loki correlation with the
    # original push line (`[NTFY] Pushed reminder <id>`). Success stays
    # silent to avoid log noise on frequent ack taps.
    rid_hint = f" rid={reminder_id}" if reminder_id else ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, content=message.encode("utf-8"), headers=headers)
        if resp.status_code in (200, 201, 202):
            NTFY_PUSH_TOTAL.labels(result="ok", kind="confirm").inc()
            return {"success": True}
        logger.warning(f"[NTFY-CONFIRM] Push returned {resp.status_code}{rid_hint}")
        NTFY_PUSH_TOTAL.labels(result="fail", kind="confirm").inc()
        return {"success": False, "status_code": resp.status_code}
    except Exception as e:
        NTFY_PUSH_TOTAL.labels(result="fail", kind="confirm").inc()
        logger.error(f"[NTFY-CONFIRM] Push failed{rid_hint}: {type(e).__name__}: {e}")
        return {"success": False, "error": f"{type(e).__name__}: {e}"}
