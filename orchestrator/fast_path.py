"""
Fast-Path: Simple Device Commands Bypass LLMs

Intercepts simple voice commands like "turn on the bedroom lights" and routes
them directly to Home Assistant via ha_integration.py, bypassing all LLM calls.

Result: ~100-200ms instead of 3-12s for ~40-50% of voice commands.

Conservative by design: any ambiguity -> falls through to Helios.
"""

import logging
import random
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class FastPathResult:
    """Result of a fast-path attempt."""

    handled: bool
    response_text: str = ""
    action: str = ""
    entity_name: str = ""


# --- Disqualifier patterns (anything matching these is NOT a simple device command) ---

# Questions about state
_QUESTION_RE = re.compile(
    r"\?$|^(?:is|are|was|were|do|does|did|can|could|will|would|what|how|why|when|where|which)\b", re.IGNORECASE
)

# Temporal qualifiers
_TEMPORAL_RE = re.compile(
    r"\b(?:in\s+\d+\s+(?:minute|hour|second|min|hr|sec)s?|at\s+\d{1,2}(?::\d{2})\s*(?:am|pm)?|at\s+\d{1,2}\s*(?:am|pm)|tomorrow|tonight|later|soon|after|before|every\s+\d|schedule|morning|evening|afternoon)\b",
    re.IGNORECASE,
)

# Multi-intent conjunctions followed by non-device verbs
_MULTI_INTENT_RE = re.compile(
    r"\b(?:and|then|also)\s+(?:remind|search|tell|find|look|show|check|ask|play\s+(?:some|a\s+song|music\s+from))\b",
    re.IGNORECASE,
)

# References to non-device tools
_NON_DEVICE_RE = re.compile(
    r"\b(?:remind(?:er)?|timer|focus\s+(?:mode|session|timer)|search|look\s+up|find\s+(?:out|me)|weather|news|remember|note|email|message|text|call|calendar|alarm)\b",
    re.IGNORECASE,
)

# Conversational framing
_CONVERSATIONAL_RE = re.compile(
    r"\b(?:i\s+think|maybe|what\s+if|could\s+you|please\s+(?:also|and)|can\s+you\s+also|should\s+i|do\s+you|tell\s+me|explain|help\s+me)\b",
    re.IGNORECASE,
)

MAX_COMMAND_LENGTH = 120

# --- Confirmation templates ---

_TEMPLATES = {
    "turn_on": [
        "Done! {name} {verb} now on.",
        "{name} {verb} on.",
        "Got it, {name} turned on.",
    ],
    "turn_off": [
        "Done! {name} {verb} now off.",
        "{name} {verb} off.",
        "Got it, {name} turned off.",
    ],
    "toggle": [
        "Done! Toggled {name}.",
        "{name} toggled.",
    ],
    "set_color": [
        "Done! {name} set to {value}.",
        "{name} {verb} now {value}.",
    ],
    "set_color_brightness": [
        "Done! {name} set to {color} at {brightness}% brightness.",
        "{name} {verb} now {color} at {brightness}%.",
    ],
    "set_brightness": [
        "Done! {name} at {value}% brightness.",
        "{name} set to {value}%.",
    ],
    "set_temperature": [
        "Done! Thermostat set to {value} degrees.",
        "Temperature set to {value}.",
    ],
    "set_volume": [
        "Done! {name} volume at {value}%.",
        "Volume on {name} set to {value}%.",
    ],
    "lock": [
        "Done! {name} {verb} locked.",
        "{name} locked.",
    ],
    "unlock": [
        "Done! {name} {verb} unlocked.",
        "{name} unlocked.",
    ],
    "activate_scene": [
        "Done! {name} scene activated.",
        "Activated {name}.",
    ],
    "open_cover": [
        "Done! {name} opening.",
        "{name} {verb} opening.",
    ],
    "close_cover": [
        "Done! {name} closing.",
        "{name} {verb} closing.",
    ],
    "media_play": [
        "Done! Playing on {name}.",
        "{name} playing.",
    ],
    "media_pause": [
        "Done! {name} paused.",
        "Paused {name}.",
    ],
    "media_stop": [
        "Done! {name} stopped.",
        "Stopped {name}.",
    ],
    "media_next": [
        "Done! Skipped to next track on {name}.",
        "Next track on {name}.",
    ],
    "media_previous": [
        "Done! Previous track on {name}.",
        "Back a track on {name}.",
    ],
}


def _plural_verb(name: str) -> str:
    """Return 'are' for plural names (ending in 's'), 'is' for singular."""
    return "are" if name.rstrip().lower().endswith("s") else "is"


def _build_confirmation(action: str, entity_name: str, value=None) -> str:
    """Build a natural confirmation message from templates."""
    verb = _plural_verb(entity_name)
    templates = _TEMPLATES.get(action, ["Done!"])
    template = random.choice(templates)

    if action == "set_color_brightness" and isinstance(value, dict):
        return template.format(
            name=entity_name,
            verb=verb,
            color=value.get("color", ""),
            brightness=value.get("brightness", ""),
        )

    return template.format(
        name=entity_name,
        verb=verb,
        value=value if value is not None else "",
    )


def is_fast_path_eligible(text: str) -> bool:
    """
    Lightweight pre-filter: returns True only if text looks like a simple device command.
    Conservative — rejects anything ambiguous.
    """
    if len(text) > MAX_COMMAND_LENGTH:
        return False

    if _QUESTION_RE.search(text):
        return False

    if _TEMPORAL_RE.search(text):
        return False

    if _MULTI_INTENT_RE.search(text):
        return False

    if _NON_DEVICE_RE.search(text):
        return False

    return not _CONVERSATIONAL_RE.search(text)


async def try_fast_path(text: str, ha_client) -> FastPathResult:
    """
    Attempt to handle a simple device command without any LLM.

    Returns FastPathResult with handled=True if the command was executed,
    or handled=False if it should fall through to the normal pipeline.
    """
    # 1. Pre-filter
    if not is_fast_path_eligible(text):
        logger.debug(f"[FAST-PATH] Not eligible: {text[:60]}")
        return FastPathResult(handled=False)

    # 2. Try regex parse (reuses ha_integration patterns)
    parsed = ha_client.parse_command(text)
    if not parsed:
        logger.debug(f"[FAST-PATH] No regex match: {text[:60]}")
        return FastPathResult(handled=False)

    logger.info(f"[FAST-PATH] Matched: action={parsed.action}, target='{parsed.target_text}', value={parsed.value}")

    # 3. Execute via HA (handles entity matching, compound commands, API call)
    try:
        result = await ha_client.execute_command(text)
    except Exception as e:
        logger.warning(f"[FAST-PATH] Execution error, falling through: {e}")
        return FastPathResult(handled=False)

    # 4. Check result
    if not result.success:
        logger.info(f"[FAST-PATH] Execution failed, falling through: {result.message}")
        return FastPathResult(handled=False)

    # 5. Build confirmation
    # Extract friendly name from the result message or entity_id
    entity = ha_client._entities.get(result.entity_id)
    entity_name = entity.friendly_name if entity else result.entity_id

    confirmation = _build_confirmation(parsed.action, entity_name, parsed.value)

    logger.info(f"[FAST-PATH] Success: {confirmation}")

    return FastPathResult(
        handled=True,
        response_text=confirmation,
        action=parsed.action,
        entity_name=entity_name,
    )
