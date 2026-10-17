"""Shared guard for every Home Assistant service call that a user or the model
can steer: the chat ``home_assistant`` tool (``tool_handlers``), the legacy
natural-language path (``ha_integration.execute_command`` — fast path and
``POST /api/ha/command``) and the structured ``POST /api/ha/command`` body.

One policy, enforced in one place:
- only the domains listed in the tool schema (``HA_LISTED_DOMAINS``);
- never a denied entity (``HA_TOOL_DENY_ENTITIES`` globs + the Helios plug,
  which only the gated ``helios_power`` tool controls);
- only allow-listed services per domain (no unlock, no cover open);
- targeting keys stripped from ``data`` so a call can't re-aim at another entity;
- ``media_player.play_media`` only plays media-source:// ids or the
  orchestrator's own audio/stream URLs; ``media_player.join`` only groups
  listed, non-denied media players.

Internal callers (reminder announcements, focus audio, routines) go through
``HomeAssistantClient.call_service`` directly and are not subject to this —
their targets come from operator config, not user text.

``shared`` is imported lazily: ``shared`` imports ``ha_integration``, which
imports this module.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any, Dict, Optional, Tuple

from orchestrator.config import settings

# Domains listed in the home_assistant tool schema, in listing order.
HA_LISTED_DOMAINS = ("light", "switch", "fan", "climate", "cover", "scene", "lock", "media_player")

# Keys that would re-target a service call at other entities/areas.
HA_TARGET_KEYS = (
    "entity_id",
    "area_id",
    "device_id",
    "label_id",
    "floor_id",
    "target",
    "entities",
    "snapshot_entities",
    "scene_id",
)

# Services that may be called, per domain. An allow-list, not a deny-list:
# services like scene.apply / scene.create take their own entity map and
# would bypass every check here.
HA_ALLOWED_SERVICES = {
    "light": {"turn_on", "turn_off", "toggle"},
    "switch": {"turn_on", "turn_off", "toggle"},
    "fan": {"turn_on", "turn_off", "toggle", "set_percentage", "oscillate", "set_preset_mode", "set_direction"},
    "climate": {"set_temperature", "set_hvac_mode", "set_fan_mode", "set_preset_mode", "turn_on", "turn_off"},
    "scene": {"turn_on"},
    "media_player": {
        "play_media",
        "media_play",
        "media_pause",
        "media_stop",
        "media_next_track",
        "media_previous_track",
        "volume_set",
        "volume_up",
        "volume_down",
        "volume_mute",
        "turn_on",
        "turn_off",
        "select_source",
        "shuffle_set",
        "repeat_set",
        "join",
        "unjoin",
    },
    # Closing/locking only. Opening or unlocking needs a confirmation flow chat
    # doesn't have yet (no such entities today — this guards the day one is added).
    "cover": {"close_cover", "stop_cover", "close_cover_tilt", "stop_cover_tilt"},
    "lock": {"lock"},
}

_ENTITY_RE = re.compile(r"[a-z_]+\.[a-z0-9_]{1,128}")
_SERVICE_RE = re.compile(r"[a-z_]{1,64}")
_JOIN_MAX_MEMBERS = 16


def ha_tool_denied(entity_id: str) -> bool:
    """Entities chat/NLP control must never touch (and never list):
    HA_TOOL_DENY_ENTITIES globs plus the Helios plug, which is controlled only
    through the gated helios_power tool."""
    from orchestrator import shared

    patterns = [p.strip().lower() for p in (shared.HA_TOOL_DENY_ENTITIES or "").split(",") if p.strip()]
    if shared.HELIOS_PLUG_ENTITY:
        # Any domain: a switch_as_x / group wrapper keeps the object id.
        patterns.append("*." + shared.HELIOS_PLUG_ENTITY.split(".", 1)[-1])
    return any(fnmatch.fnmatchcase(entity_id, p) for p in patterns)


def ha_entity_allowed(entity_id: str) -> bool:
    """Listed domain and not denied — what may be shown or targeted at all."""
    return (
        isinstance(entity_id, str) and entity_id.split(".", 1)[0] in HA_LISTED_DOMAINS and not ha_tool_denied(entity_id)
    )


def _allowed_media_url(media_id: str) -> bool:
    """media-source:// ids (HA's own library, TTS, radio browser) plus the
    URLs the orchestrator itself plays: its /api/audio/ clips and the
    configured focus streams. Anything else would turn a speaker into a
    fetcher of an arbitrary URL (HA does the GET, on the LAN)."""
    from orchestrator import shared

    if media_id.startswith("media-source://"):
        return True
    if any(c.isspace() for c in media_id) or ".." in media_id:
        return False
    streams = {getattr(shared, "FOCUS_AUDIO_LOFI_URL", ""), getattr(shared, "FOCUS_AUDIO_COFFEE_URL", "")}
    if media_id in {s for s in streams if s}:
        return True
    base = (settings.orchestrator_url or "").rstrip("/")
    return bool(base) and media_id.startswith(f"{base}/api/audio/")


def _check_media_data(service: str, data: Dict[str, Any]) -> Optional[str]:
    if service == "play_media":
        media_id = data.get("media_content_id")
        if not isinstance(media_id, str) or not media_id or len(media_id) > 2048:
            return "Failed: play_media needs a media_content_id. Nothing was changed."
        if not _allowed_media_url(media_id):
            return (
                "Failed: play_media can only play media-source:// items or Jess's own audio, "
                "not an arbitrary URL. Nothing was changed."
            )
    elif service == "join":
        members = data.get("group_members")
        if isinstance(members, str):
            members = [members]
        if not isinstance(members, list) or not members or len(members) > _JOIN_MAX_MEMBERS:
            return "Failed: join needs a group_members list of media players. Nothing was changed."
        for m in members:
            if (
                not isinstance(m, str)
                or not _ENTITY_RE.fullmatch(m)
                or not m.startswith("media_player.")
                or ha_tool_denied(m)
            ):
                return "Failed: join can only group media players that chat may control. Nothing was changed."
        data["group_members"] = members
    return None


def check_ha_call(entity_id: Any, service: Any, data: Any = None) -> Tuple[Optional[str], Dict[str, Any]]:
    """Validate one steerable HA service call.

    Returns ``(refusal, data)``: ``refusal`` is a user-facing "Failed: …
    Nothing was changed." string, or None when the call may proceed with the
    returned, sanitised ``data`` (targeting keys removed). Never logs the data
    values (they can be user text).
    """
    if not entity_id or not service:
        return "Missing entity_id or service", {}
    if not isinstance(entity_id, str) or not isinstance(service, str):
        return "Failed: entity_id and service must be strings. Nothing was changed.", {}
    if data is not None and not isinstance(data, dict):
        return 'Failed: data must be an object, e.g. {"brightness": 128}. Nothing was changed.', {}
    if not _ENTITY_RE.fullmatch(entity_id) or not _SERVICE_RE.fullmatch(service):
        return "Failed: entity_id must look like 'light.kitchen' and service like 'turn_on'. Nothing was changed.", {}

    domain = entity_id.split(".", 1)[0]
    if domain not in HA_LISTED_DOMAINS:
        return f"Failed: the {domain} domain can't be controlled from chat. Nothing was changed.", {}
    if ha_tool_denied(entity_id):
        return f"Failed: {entity_id} can't be controlled from chat. Nothing was changed.", {}
    if service not in HA_ALLOWED_SERVICES.get(domain, set()):
        if domain in ("cover", "lock"):
            return (
                f"Failed: {domain}.{service} needs a confirmation step chat doesn't have yet. Nothing was changed.",
                {},
            )
        return f"Failed: {domain}.{service} isn't available from chat. Nothing was changed.", {}

    clean = {k: v for k, v in (data or {}).items() if k not in HA_TARGET_KEYS}
    if domain == "media_player":
        refusal = _check_media_data(service, clean)
        if refusal:
            return refusal, {}
    return None, clean
