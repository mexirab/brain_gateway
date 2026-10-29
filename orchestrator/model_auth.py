"""Bearer key for requests to the local model server.

llama-server on Helios requires an API key (``--api-key-file``) once
``MODEL_API_KEY`` is set: every path except ``/health`` answers 401 without
it, including ``/v1/models`` and ``/metrics``. The chat backends already send
``Authorization`` from their config, but health probes, the ``/metrics`` read,
and vision calls go through plain httpx clients. ``ModelServerAuth`` is
attached to those clients (the shared ``_http`` client and the service
registry's probe clients).

It adds the header only when a request goes to the same origin (scheme, host,
port) as a configured OpenAI-compatible model URL with a key. Every other host
the same client talks to (Home Assistant, Google, a page fetched for
look_it_up) never sees the key. A request that already carries
``Authorization`` or ``X-Api-Key`` is left alone. ``VISION_MODEL_URL`` gets the
key when it shares the primary's origin (the live setup); a vision server on
another host gets none.

A keyed request that comes back 401/403 means MODEL_API_KEY and the server's
key file disagree: logged (rate-limited) and counted in
``bgw_model_auth_rejected_total``, because to every caller it otherwise looks
like the brain being asleep.

The key travels as plain HTTP on the LAN/tailnet: it stops callers that don't
have it, not someone who can read that traffic.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Generator

import httpx

from orchestrator import shared
from orchestrator.metrics import MODEL_AUTH_REJECTED

logger = logging.getLogger(__name__)

_DEFAULT_PORTS = {"http": 80, "https": 443}
_REJECT_LOG_INTERVAL_S = 300.0
_last_reject_log = 0.0


def _origin(url: httpx.URL | str) -> tuple[str, str, int] | None:
    try:
        u = url if isinstance(url, httpx.URL) else httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError):
        return None
    scheme = u.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not u.host:
        return None
    return scheme, u.host.lower(), u.port or _DEFAULT_PORTS[scheme]


def _is_local(backend: str) -> bool:
    return (backend or "openai_compatible").lower() == "openai_compatible"


def _configured() -> list[tuple[str, str, str]]:
    """(backend, url, key) for every model endpoint Jess is configured with.

    The live backends come first: their config can come from the profile YAML
    (``llm.model``), which wins over the env vars. The env pairs stay as a
    backstop (callers before ``init_backends``, one-off backends for
    ``MODEL_URL`` when the YAML moved the primary elsewhere), except when the
    env key is what a live cloud backend uses: with ``backend: anthropic`` in
    the YAML and no YAML key, MODEL_API_KEY is the Anthropic key, and the env
    MODEL_BACKEND default (openai_compatible) must not hand it to MODEL_URL.
    """
    out: list[tuple[str, str, str]] = []
    cloud_keys: set[str] = set()
    for backend in (shared.primary_backend, shared.fallback_backend):
        cfg = getattr(backend, "config", None)
        if cfg is None:
            continue
        out.append((cfg.backend, cfg.url, cfg.api_key))
        if cfg.api_key and not _is_local(cfg.backend):
            cloud_keys.add(cfg.api_key)
    for env_entry in (
        (shared.MODEL_BACKEND, shared.MODEL_URL, shared.MODEL_API_KEY),
        (shared.FALLBACK_MODEL_BACKEND, shared.FALLBACK_MODEL_URL, shared.FALLBACK_MODEL_API_KEY),
    ):
        if env_entry[2] not in cloud_keys:
            out.append(env_entry)
    return out


def key_for_url(url: httpx.URL | str) -> str:
    """The model-server key for ``url``'s origin, or "" if it isn't one.

    Only OpenAI-compatible backends count: a cloud backend's key (Anthropic,
    OpenAI) is sent by that backend itself and must never be attached here.
    """
    origin = _origin(url)
    if origin is None:
        return ""
    for backend, base, key in _configured():
        if key and _is_local(backend) and _origin(base) == origin:
            return key
    return ""


def _note_rejected(status: int) -> None:
    global _last_reject_log
    MODEL_AUTH_REJECTED.labels(status=str(status)).inc()
    now = time.monotonic()
    if now - _last_reject_log >= _REJECT_LOG_INTERVAL_S:
        _last_reject_log = now
        logger.warning(
            "[MODEL_AUTH] model server rejected MODEL_API_KEY (HTTP %d): the key in .env and "
            "llama-server's --api-key-file disagree; the brain will look offline until they match",
            status,
        )


class ModelServerAuth(httpx.Auth):
    """httpx auth that adds the model server's bearer key, and nothing else."""

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        if "authorization" in request.headers or "x-api-key" in request.headers:
            yield request
            return
        key = key_for_url(request.url)
        if not key:
            # A client-level auth stops httpx from turning URL userinfo
            # (http://user:pass@host) into Basic auth itself; keep that working.
            if request.url.username or request.url.password:
                yield from httpx.BasicAuth(request.url.username, request.url.password).auth_flow(request)
            else:
                yield request
            return
        request.headers["Authorization"] = f"Bearer {key}"
        response = yield request
        # The flow sees the final response after any redirects: only a 401/403
        # from the model server itself means the key was refused.
        if response.status_code in (401, 403) and _origin(response.request.url) == _origin(request.url):
            _note_rejected(response.status_code)


MODEL_SERVER_AUTH = ModelServerAuth()
