"""First-chat welcome message generator.

When a user opens Open WebUI for the first time on a fresh install, the
orchestrator prepends a one-time welcome before the assistant's response.
This module is the pure formatter for that welcome — it takes the
assistant's configuration and the set of integrations the user has already
wired up, and renders a markdown string.

The welcome is "responsive" — it only mentions integrations that are NOT
yet configured, so a user who's already set up Home Assistant doesn't get
"want to set up Home Assistant?" pitched at them.

Side effects: none. Network calls: none. DB access: none. Just formatting.
The caller is responsible for detecting first-chat state and flipping the
`first_chat_completed` flag in `setup_state.json` afterwards.

Security note: `user_name` and `assistant_name` come from operator-settable
identity (`/api/config/identity`), but a future write path or compromised
operator could put markdown / HTML-injection payloads there. We strip
markdown control characters at format time so the welcome can't be
weaponized — defense in depth on top of whatever the chat client's
renderer (Open WebUI etc.) sanitizes.
"""

from __future__ import annotations

import re
from typing import Dict, Optional

# Markdown special chars that change rendering semantics if interpolated
# into a message. Stripped from operator-set identity fields before they
# land in the welcome string.
_MD_SPECIAL_RE = re.compile(r"[\[\]()<>*_`!\\#|~]")


def _safe_name(value: Optional[str], default: str, max_len: int = 64) -> str:
    """Return a markdown-safe version of an operator-set name.

    Strips characters that would change markdown rendering (brackets, parens,
    asterisks, backticks, angle brackets, etc.) and caps length. Falls back
    to `default` when input is empty after stripping.
    """
    if not value:
        return default
    cleaned = _MD_SPECIAL_RE.sub("", value).strip()
    if not cleaned:
        return default
    return cleaned[:max_len]


def _integration_status(env_overrides: Dict[str, str]) -> Dict[str, bool]:
    """Which optional integrations are configured?

    Reads from the already-loaded env overrides (not the full os.environ) so
    we only count what the user explicitly set up, not docker-compose defaults.

    Truthy values match Pydantic's bool coercion: `true`, `1`, `yes`, `on`
    (case-insensitive), so a hand-edited overrides file with `NTFY_ENABLED=1`
    is correctly recognized as enabled.
    """

    def truthy(v: str) -> bool:
        return v.strip().lower() in ("true", "1", "yes", "on")

    return {
        "ha": bool(env_overrides.get("HA_URL") and env_overrides.get("HA_TOKEN")),
        "ntfy": truthy(env_overrides.get("NTFY_ENABLED", "")),
        "pushover": truthy(env_overrides.get("PUSHOVER_ENABLED", "")),
        "paperless": bool(env_overrides.get("PAPERLESS_URL") and env_overrides.get("PAPERLESS_API_TOKEN")),
    }


_INTEGRATION_BLURBS = {
    "ha": ("Smart home control", "Home Assistant"),
    "ntfy": ("Phone push notifications (free, best for Android)", "ntfy"),
    "pushover": ("Phone push notifications (paid, best for iPhone)", "Pushover"),
    "paperless": ("Document storage with OCR + auto-tagging", "Paperless-ngx"),
}


def _settings_url(lan_ip: Optional[str]) -> str:
    """Return the full /settings URL with the LAN IP filled in if known."""
    if lan_ip:
        return f"http://{lan_ip}:3001/settings"
    return "http://<your-box-ip>:3001/settings  (the URL the installer printed)"


def generate_welcome(
    user_name: Optional[str],
    assistant_name: str,
    lan_ip: Optional[str],
    env_overrides: Dict[str, str],
) -> str:
    """Render the first-chat welcome message.

    Args:
        user_name: from /api/config/identity (None / "" if unset).
            Markdown control chars are stripped before interpolation.
        assistant_name: from /api/config/identity (defaults to "Jess").
            Markdown control chars are stripped before interpolation.
        lan_ip: the box's LAN IP for the /settings link (None if unknown).
            Renders into a clickable URL — caller should pass a validated
            IPv4 string here; this formatter does not validate it.
        env_overrides: setup_env.read_overrides() — for integration status.

    Returns:
        Markdown string to prepend to the assistant's first reply.
    """
    safe_user = _safe_name(user_name, default="")
    safe_assistant = _safe_name(assistant_name, default="Jess")
    status = _integration_status(env_overrides)
    settings = _settings_url(lan_ip)
    greeting = f"Welcome, {safe_user}!" if safe_user else "Welcome!"

    lines = [
        f"👋 {greeting} I'm {safe_assistant}, your private AI assistant — running entirely",
        "on your local hardware. Nothing leaves this box unless you explicitly enable it.",
        "",
        "**What's working right now:**",
        "- 💬 Chat (this conversation)",
        "- 🗣️ Voice — I can speak my replies aloud if you wire up speakers",
        "- 🧠 Memory — I'll remember things you tell me across conversations",
        '- ⏰ Reminders + focus timers — try "remind me to drink water in 30 min"',
    ]

    # List unconfigured optional integrations — direct the user to /settings
    # to wire them up. (Earlier versions of this welcome offered to walk
    # the user through configuration via chat tools; those were removed
    # after a hacker review found credential-exfiltration via prompt
    # injection. /settings is the secure post-install configuration surface.)
    unconfigured = [k for k, on in status.items() if not on]
    if unconfigured:
        lines += [
            "",
            "**Want to enable more?** Visit the Settings page to wire any of these up:",
        ]
        for k in unconfigured:
            blurb, name = _INTEGRATION_BLURBS[k]
            lines.append(f"- **{name}** — {blurb}")

    lines += [
        "",
        f"→ {settings}",
        "",
        "---",
        "",
    ]

    return "\n".join(lines)
