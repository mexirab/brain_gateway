"""First-boot setup-wizard env writer — `setup_overrides.env` overlay.

The setup wizard collects model / voice / push-channel / integration values
(some of them credentials) and needs to persist them somewhere the orchestrator
will pick them up on next start. The container mounts `.env` read-only and
host-systemd writers are out of scope for a first-boot UX, so this module owns
a small, gitignored overrides file under the `/app/data` rw mount:

    /app/data/setup_overrides.env   (.env-style: KEY=VALUE per line, chmod 600)

`orchestrator/config.py` applies this file to `os.environ` BEFORE Pydantic
Settings is instantiated, so writes here take effect on the next process boot.

Security posture — squarely the mainstream self-hosted appliance pattern,
matching Home Assistant's `secrets.yaml`, Gitea's `app.ini`, etc.:

- Endpoint is **first-boot only** — locked the moment `setup_completed` flips
  true (Gitea's `INSTALL_LOCK` pattern). After Finish, this module's write
  paths are inert and the routes return HTTP 410.
- **Allow-listed keys** — only the keys in `ALLOWED_KEYS` can be written. An
  LLM prompt-injection on the orchestrator's tool surface can't smuggle
  arbitrary env via this endpoint even during the open window.
- **Plaintext file with `0600` perms**, gitignored under `data/app/`. Same
  posture as `.env` today — no encryption-at-rest theatre on a single-box box.
- **Validate-before-persist** for credential-class keys: HA token is tested
  against HA's `/api/`, Pushover keys against `api.pushover.net`, etc. Stored
  secrets are known-good.
- **Presence-only on read-back** for secret keys (`GET` never echoes the value).
- **Redacted in logs** via `config_writer._redact()`.

This module exposes the file I/O + validators; the HTTP endpoints live in
`routes_setup.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Path module-level so tests can monkeypatch.
_OVERRIDES_PATH = "/app/data/setup_overrides.env"
_FILE_MODE = 0o600

# In-memory flag flipped on every successful write/delete since process start.
# The orchestrator caches `settings` at startup; new overrides only take effect
# on next boot, so any change since start means "restart required." Exposed via
# GET /api/setup/env so the wizard can surface it.
#
# Invariant: the orchestrator runs with a single uvicorn worker (see
# `orchestrator/Dockerfile` CMD — no `--workers` flag). This module-level flag
# is therefore process-wide. If the deployment ever moves to `--workers N`,
# this needs to become a shared-state lookup (file mtime, Redis, etc.) — each
# worker would otherwise see a stale flag.
_dirty_since_boot = False

# Serializes writes against `is_first_boot()` checks, so a concurrent
# `POST /api/setup/complete` can't slip in between the lock-check and the
# write. Single-worker asyncio means contention is essentially zero — this is
# defence-in-depth, not a hot path.
_write_lock = asyncio.Lock()


# Control characters (NUL, C0, DEL) are never legitimate in our allow-listed
# values (URLs, model ids, tokens). Reject on write — kills both the
# log-injection surface (operator-viewed logs / Loki) and the .env parser's
# lossy newline round-trip in one shot.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def is_dirty_since_boot() -> bool:
    """True if overrides have been written/deleted since the process started."""
    return _dirty_since_boot


# ----------------------------------------------------------------------------
# Allow-list
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class KeySpec:
    """Metadata for a wizard-writable env key.

    `secret=True` means GET never echoes the value (only `present`), and the
    audit logger redacts it.
    """

    secret: bool
    service: str  # logical group for the validator; e.g. "ha", "pushover"
    description: str


# v1 wizard scope. Order is the rough order the wizard collects them.
ALLOWED_KEYS: Dict[str, KeySpec] = {
    # Model step (non-secret)
    "VLLM_MODEL": KeySpec(False, "model", "Primary vLLM model id"),
    "VLLM_SERVED_NAME": KeySpec(False, "model", "vLLM --served-model-name"),
    "VLLM_QUANTIZATION": KeySpec(False, "model", "vLLM --quantization"),
    "VLLM_MAX_MODEL_LEN": KeySpec(False, "model", "vLLM context window"),
    "VLLM_GPU_MEM_UTIL": KeySpec(False, "model", "vLLM --gpu-memory-utilization"),
    "MODEL_NAME": KeySpec(False, "model", "Orchestrator-side primary model name"),
    # Voice step (non-secret)
    "TTS_VOICE": KeySpec(False, "voice", "Default TTS voice id"),
    # Push channel — ntfy
    "NTFY_ENABLED": KeySpec(False, "ntfy", "Enable ntfy push channel"),
    "NTFY_URL": KeySpec(False, "ntfy", "ntfy server base URL"),
    "NTFY_TOPIC": KeySpec(False, "ntfy", "ntfy topic"),
    "NTFY_HMAC_SECRET": KeySpec(True, "ntfy", "HMAC secret for ack/snooze callbacks"),
    # Push channel — Pushover
    "PUSHOVER_ENABLED": KeySpec(False, "pushover", "Enable Pushover push channel"),
    "PUSHOVER_USER_KEY": KeySpec(True, "pushover", "Pushover user key"),
    "PUSHOVER_APP_TOKEN": KeySpec(True, "pushover", "Pushover application token"),
    # Home Assistant
    "HA_URL": KeySpec(False, "ha", "Home Assistant URL"),
    "HA_TOKEN": KeySpec(True, "ha", "Home Assistant long-lived access token"),
    # Paperless-ngx
    "PAPERLESS_URL": KeySpec(False, "paperless", "Paperless-ngx URL"),
    "PAPERLESS_API_TOKEN": KeySpec(True, "paperless", "Paperless-ngx API token"),
}


def is_allowed(key: str) -> bool:
    return key in ALLOWED_KEYS


def is_secret(key: str) -> bool:
    spec = ALLOWED_KEYS.get(key)
    return bool(spec and spec.secret)


# ----------------------------------------------------------------------------
# File I/O — .env-style, atomic, 0600
# ----------------------------------------------------------------------------


def _parse_env_line(line: str) -> Optional[Tuple[str, str]]:
    """Parse one `KEY=VALUE` line from a .env-style file.

    Strips surrounding quotes; returns None for blanks/comments/malformed.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, _, value = stripped.partition("=")
    key = key.strip()
    value = value.strip()
    # Strip a single layer of surrounding quotes (single or double).
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    if not key:
        return None
    return key, value


def _escape_value(value: str) -> str:
    """Quote a value if it contains chars that .env-style parsers choke on."""
    if value == "":
        return ""
    if any(c in value for c in (" ", "\t", "#", "'", '"', "\n")):
        # Double-quote and escape embedded double-quote + backslash + newline.
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    return value


def read_overrides() -> Dict[str, str]:
    """Read the overrides file as a dict. Missing/unreadable → empty dict.

    Keys not in the allow-list are silently dropped — defence-in-depth in case
    the file is hand-edited.
    """
    try:
        with open(_OVERRIDES_PATH, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("[SETUP_ENV] could not read %s: %s", _OVERRIDES_PATH, e)
        return {}

    out: Dict[str, str] = {}
    for line in lines:
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        key, value = parsed
        if is_allowed(key):
            out[key] = value
    return out


def _atomic_write_overrides(values: Dict[str, str]) -> None:
    """Write the overrides file atomically with `0600` perms.

    Pattern matches `config_writer.atomic_write_yaml` and `routes_setup.
    _atomic_write_json` — tmpfile + flush + fsync + os.replace.
    """
    target = Path(_OVERRIDES_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        os.chmod(tmp_path, _FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(
                "# setup_overrides.env — written by the first-boot setup wizard.\n"
                "# DO NOT commit. Treat like .env — plaintext credentials may live here.\n"
            )
            for key in sorted(values):
                f.write(f"{key}={_escape_value(values[key])}\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
        # `os.replace` preserves the dest's perms if it existed; for the new-file
        # case our chmod above on tmp_path carries over. Re-chmod to be sure.
        with contextlib.suppress(OSError):
            os.chmod(target, _FILE_MODE)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def set_keys(updates: Dict[str, str]) -> List[str]:
    """Merge `updates` into the overrides file. Returns the list of keys written.

    Leading/trailing whitespace on values is stripped (pasted tokens often
    carry a trailing newline). After stripping, any remaining control char
    rejects the whole batch.

    Raises `ValueError` for: a non-allow-listed key, an empty value, or a
    value containing control characters. Fail-loud — the caller's HTTP
    handler turns this into a 400.
    """
    global _dirty_since_boot
    bad = [k for k in updates if not is_allowed(k)]
    if bad:
        raise ValueError(f"not in allow-list: {sorted(bad)}")
    cleaned: Dict[str, str] = {}
    for k, v in updates.items():
        s = str(v).strip()
        if s == "":
            raise ValueError(f"empty value for {k}; use DELETE /api/setup/env/{k} to unset")
        if _CONTROL_CHAR_RE.search(s):
            raise ValueError(f"control characters not allowed in value for {k}")
        cleaned[k] = s
    current = read_overrides()
    current.update(cleaned)
    _atomic_write_overrides(current)
    _dirty_since_boot = True
    return sorted(cleaned.keys())


def delete_key(key: str) -> bool:
    """Remove a key from the overrides file. Returns True if it was present."""
    global _dirty_since_boot
    if not is_allowed(key):
        raise ValueError(f"not in allow-list: {key}")
    current = read_overrides()
    if key not in current:
        return False
    del current[key]
    _atomic_write_overrides(current)
    _dirty_since_boot = True
    return True


def key_status_map() -> Dict[str, Dict[str, object]]:
    """Per-key state for GET /api/setup/env.

    For non-secret keys we return the value (the wizard re-pre-fills the input).
    For secret keys we return only `present: bool` — never the value back to the
    browser.
    """
    current = read_overrides()
    out: Dict[str, Dict[str, object]] = {}
    for key, spec in ALLOWED_KEYS.items():
        present = key in current
        entry: Dict[str, object] = {
            "secret": spec.secret,
            "service": spec.service,
            "description": spec.description,
            "present": present,
        }
        if present and not spec.secret:
            entry["value"] = current[key]
        out[key] = entry
    return out


# ----------------------------------------------------------------------------
# Validators — live-test credential combos before they're persisted
# ----------------------------------------------------------------------------


ValidatorResult = Tuple[bool, str]
Validator = Callable[[Dict[str, str]], Awaitable[ValidatorResult]]


def _http_client(timeout: float = 8.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


# Generic detail string for any network-level failure. Used in place of the
# httpx exception class name — the class name (ConnectError /
# RemoteProtocolError / ReadError / etc.) leaked enough information to use the
# validator as a port-scan oracle (distinguishing closed-port from
# open-non-HTTP from DNS-timeout). The HTTP status-code path still echoes the
# real code, which is fine — it's information the caller would learn from
# their own browser anyway.
_NETWORK_ERROR_DETAIL = "unreachable or timed out"


def _validate_url(url: str) -> Tuple[bool, str]:
    """Reject URLs the validators can't safely append a path to.

    The validators build request URLs by appending `/api/` or `/<topic>` to
    the operator-supplied base. A fragment / query / params component in the
    input would push the appended path INSIDE the fragment, never reaching
    the server — silently turning a "did this credential work" check into a
    "did `/` return 200" probe (the URL-fragment validator false-positive
    the hacker review found). We also pin to http(s) so `file://` and
    friends can't be smuggled in, reject userinfo (`user:pass@host`) so
    creds aren't leaked into the URL alongside the Bearer/Token header,
    and pre-reject whitespace/control chars so the operator gets a
    precise error rather than a misleading "unreachable or timed out".
    """
    if url != url.strip():
        return False, "URL has leading or trailing whitespace."
    if _CONTROL_CHAR_RE.search(url):
        return False, "URL contains control characters."
    try:
        p = urlparse(url)
    except ValueError:
        return False, "URL is malformed."
    if p.scheme not in ("http", "https"):
        return False, "URL must use http or https."
    if not p.netloc:
        return False, "URL is missing a host."
    if p.username is not None or p.password is not None:
        return False, "URL must not contain credentials (use the token field)."
    if p.fragment or p.query or p.params:
        return False, "URL must not contain a fragment, query, or params."
    return True, ""


async def _validate_ha(values: Dict[str, str]) -> ValidatorResult:
    raw = values.get("HA_URL") or ""
    token = values.get("HA_TOKEN") or ""
    if not raw or not token:
        return False, "Both HA_URL and HA_TOKEN are required."
    ok, why = _validate_url(raw)
    if not ok:
        return False, f"HA_URL invalid: {why}"
    url = raw.rstrip("/")
    try:
        async with _http_client() as client:
            r = await client.get(f"{url}/api/", headers={"Authorization": f"Bearer {token}"})
    except (httpx.HTTPError, httpx.InvalidURL):
        return False, f"Could not reach Home Assistant: {_NETWORK_ERROR_DETAIL}."
    if r.status_code == 200:
        return True, "Home Assistant reachable and token accepted."
    if r.status_code == 401:
        return False, "Home Assistant rejected the token."
    return False, f"Home Assistant returned HTTP {r.status_code}."


async def _validate_pushover(values: Dict[str, str]) -> ValidatorResult:
    user_key = values.get("PUSHOVER_USER_KEY") or ""
    app_token = values.get("PUSHOVER_APP_TOKEN") or ""
    if not user_key or not app_token:
        return False, "Both PUSHOVER_USER_KEY and PUSHOVER_APP_TOKEN are required."
    # Pushover's validation endpoint — verifies token + user without sending a
    # message. https://pushover.net/api#verification
    try:
        async with _http_client() as client:
            r = await client.post(
                "https://api.pushover.net/1/users/validate.json",
                data={"token": app_token, "user": user_key},
            )
    except (httpx.HTTPError, httpx.InvalidURL):
        return False, f"Could not reach Pushover: {_NETWORK_ERROR_DETAIL}."
    if r.status_code == 200:
        try:
            body = r.json()
        except ValueError as e:
            logger.warning("[SETUP_ENV] Pushover non-JSON response: %s", e)
            body = {}
        if body.get("status") == 1:
            return True, "Pushover credentials accepted."
        errors = body.get("errors") or ["unknown error"]
        return False, f"Pushover rejected: {'; '.join(errors)}"
    return False, f"Pushover returned HTTP {r.status_code}."


async def _validate_ntfy(values: Dict[str, str]) -> ValidatorResult:
    """Validate by publishing a min-priority "setup wizard test" message.

    NOTE — unlike the other validators this side-effects the user's devices:
    every subscribed ntfy client will see a (silent, low-priority) push.
    The wizard UI should warn before calling this so the user isn't surprised.
    """
    raw = values.get("NTFY_URL") or ""
    topic = values.get("NTFY_TOPIC") or ""
    if not raw or not topic:
        return False, "Both NTFY_URL and NTFY_TOPIC are required."
    ok, why = _validate_url(raw)
    if not ok:
        return False, f"NTFY_URL invalid: {why}"
    url = raw.rstrip("/")
    # ntfy doesn't have auth on public topics, so we just verify the topic URL
    # is reachable. POST a minimal silent message at min priority.
    try:
        async with _http_client() as client:
            r = await client.post(
                f"{url}/{topic}",
                content="setup wizard test",
                headers={"Title": "Jess setup", "Priority": "min", "Tags": "gear"},
            )
    except (httpx.HTTPError, httpx.InvalidURL):
        return False, f"Could not reach ntfy: {_NETWORK_ERROR_DETAIL}."
    if r.status_code in (200, 202):
        return True, "ntfy topic reachable (test message sent)."
    if r.status_code in (401, 403):
        return False, "ntfy server rejected the publish — auth required?"
    return False, f"ntfy returned HTTP {r.status_code}."


async def _validate_paperless(values: Dict[str, str]) -> ValidatorResult:
    raw = values.get("PAPERLESS_URL") or ""
    token = values.get("PAPERLESS_API_TOKEN") or ""
    if not raw or not token:
        return False, "Both PAPERLESS_URL and PAPERLESS_API_TOKEN are required."
    ok, why = _validate_url(raw)
    if not ok:
        return False, f"PAPERLESS_URL invalid: {why}"
    url = raw.rstrip("/")
    try:
        async with _http_client() as client:
            r = await client.get(f"{url}/api/", headers={"Authorization": f"Token {token}"})
    except (httpx.HTTPError, httpx.InvalidURL):
        return False, f"Could not reach Paperless: {_NETWORK_ERROR_DETAIL}."
    if r.status_code in (200, 301, 302):
        return True, "Paperless-ngx reachable and token accepted."
    if r.status_code == 401:
        return False, "Paperless rejected the token."
    return False, f"Paperless returned HTTP {r.status_code}."


_VALIDATORS: Dict[str, Validator] = {
    "ha": _validate_ha,
    "pushover": _validate_pushover,
    "ntfy": _validate_ntfy,
    "paperless": _validate_paperless,
}


def has_validator(service: str) -> bool:
    return service in _VALIDATORS


async def validate_service(service: str, values: Dict[str, str]) -> ValidatorResult:
    """Run the validator for `service` against the given candidate values.

    Services without a validator (`model`, `voice`) return `(True, "no
    validation required")` — the caller can persist directly. Unknown services
    return `(False, ...)`.
    """
    service = service.lower()
    validator = _VALIDATORS.get(service)
    if validator is None:
        known_services = {spec.service for spec in ALLOWED_KEYS.values()}
        if service in known_services:
            return True, "No live validation required for this service."
        return False, f"Unknown service: {service}"
    return await validator(values)


# ----------------------------------------------------------------------------
# Bootstrap: applied before Pydantic Settings reads env
# ----------------------------------------------------------------------------


def apply_to_environ(env: Optional[Dict[str, str]] = None) -> List[str]:
    """Read the overrides file and set each KEY in `env` (default: `os.environ`).

    Called from `config.py` BEFORE `settings = Settings()` runs so the overrides
    win over the compose-injected env block (Pydantic Settings: process env >
    env_file). Returns the list of keys applied.
    """
    target = env if env is not None else os.environ
    overrides = read_overrides()
    applied: List[str] = []
    for key, value in overrides.items():
        target[key] = value
        applied.append(key)
    return applied


# ----------------------------------------------------------------------------
# Log redaction — defence-in-depth around audit logging of writes
# ----------------------------------------------------------------------------


def redact_for_log(updates: Dict[str, str]) -> Dict[str, str]:
    """Return a copy of `updates` safe to write to logs.

    Secret values become `"***"`. Non-secret values pass through with control
    chars replaced by `?` — an attacker can't smuggle ANSI escapes through a
    URL/model-id field into operator-viewed logs (this is defence-in-depth on
    top of `set_keys` already rejecting them).
    """
    return {k: ("***" if is_secret(k) else _CONTROL_CHAR_RE.sub("?", str(v))) for k, v in updates.items()}


__all__ = [
    "ALLOWED_KEYS",
    "KeySpec",
    "apply_to_environ",
    "delete_key",
    "has_validator",
    "is_allowed",
    "is_dirty_since_boot",
    "is_secret",
    "key_status_map",
    "read_overrides",
    "redact_for_log",
    "set_keys",
    "validate_service",
]
