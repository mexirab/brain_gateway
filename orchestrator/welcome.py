"""First-chat welcome message generator.

When a user opens Open WebUI for the first time on a fresh install, the
orchestrator prepends a one-time welcome before the assistant's response.
This module is the pure formatter for that welcome — it takes the assistant's
configuration and the set of integrations the user has already wired up,
and renders a markdown string.

The welcome is "responsive" — it only mentions integrations that are NOT
yet configured, so a user who's already set up Home Assistant doesn't get
"want to set up Home Assistant?" pitched at them.

Side effects: none. Network calls: none. DB access: none. Just formatting.
The caller is responsible for detecting first-chat state and flipping the
`first_chat_completed` flag in `setup_state.json` afterwards.
"""

from __future__ import annotations

from typing import Dict, Optional


def _integration_status(env_overrides: Dict[str, str]) -> Dict[str, bool]:
    """Which optional integrations are configured?

    Reads from the already-loaded env overrides (not the full os.environ) so
    we only count what the user explicitly set up, not docker-compose defaults.
    """
    return {
        "ha": bool(env_overrides.get("HA_URL") and env_overrides.get("HA_TOKEN")),
        "ntfy": env_overrides.get("NTFY_ENABLED", "").lower() == "true",
        "pushover": env_overrides.get("PUSHOVER_ENABLED", "").lower() == "true",
        "paperless": bool(env_overrides.get("PAPERLESS_URL") and env_overrides.get("PAPERLESS_API_TOKEN")),
    }


# Human-readable descriptions for the "want to set up X?" list.
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
        user_name: from /api/config/identity (None if unset)
        assistant_name: from /api/config/identity (e.g. "Jess")
        lan_ip: the box's LAN IP for the /settings link (None if unknown)
        env_overrides: setup_env.read_overrides() — for integration status

    Returns:
        Markdown string to prepend to the assistant's first reply.
    """
    status = _integration_status(env_overrides)
    settings = _settings_url(lan_ip)
    greeting = f"Welcome, {user_name}!" if user_name else "Welcome!"

    lines = [
        f"👋 {greeting} I'm {assistant_name}, your private AI assistant — running entirely",
        "on your local hardware. Nothing leaves this box unless you explicitly enable it.",
        "",
        "**What's working right now:**",
        "- 💬 Chat (this conversation)",
        "- 🗣️ Voice — I can speak my replies aloud if you wire up speakers",
        "- 🧠 Memory — I'll remember things you tell me across conversations",
        '- ⏰ Reminders + focus timers — try "remind me to drink water in 30 min"',
    ]

    # Build the "want to set up X?" list from un-configured integrations.
    unconfigured = [k for k, on in status.items() if not on]
    if unconfigured:
        lines += [
            "",
            '**Want to enable any of these too? Just say *"set up X"* and I\'ll walk you through:**',
        ]
        for k in unconfigured:
            blurb, name = _INTEGRATION_BLURBS[k]
            lines.append(f"- **{name}** — {blurb}")

    lines += [
        "",
        f"Or visit the Settings page for the full panel:  \n→ {settings}",
        "",
        "---",
        "",
    ]

    return "\n".join(lines)
