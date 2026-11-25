"""Shared pytest fixtures and plain helpers for the memory/config/finance/paperless test files.

conftest.py star-imports this module, so every fixture listed in ``__all__``
is available to every test file. Give fixtures names unique across the suite
(prefix with the area) — a clash with another fixtures_*.py module is caught
by conftest at import time. Plain helper functions can be imported directly.

Fixtures (``__all__``):

- ``memory_palace``: factory for a ``MemPalace`` over a fake ``shared`` (no
  Chroma, fake embedder), patched in for the whole test;
- ``memory_fernet``: a fresh Fernet cipher installed as auto_learn's cipher;
- ``memory_key_file``: auto_learn pointed at a tmp key file, encryption on;
- ``memory_paperless_on``: Paperless enabled on the live settings singleton.

Helpers (imported by name):

- ``real_palace_yaml()``: the repo's data/palace.yaml, wherever it is mounted;
- ``make_settings(**env)``: a ``Settings`` built from exactly ``env``;
- ``FakeEmbedder``: stands in for ``shared.embedding_model``.

Orchestrator modules are imported inside the fixtures, never at module level:
conftest imports this file before any test runs.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from orchestrator.tests._helpers import REPO_ROOT

__all__ = ["memory_palace", "memory_fernet", "memory_key_file", "memory_paperless_on"]

# ---------------------------------------------------------------------------
# palace.yaml + MemPalace
# ---------------------------------------------------------------------------

PALACE_CANDIDATES = [
    REPO_ROOT / "data" / "palace.yaml",  # repo checkout / CI
    Path("/app/data_repo/palace.yaml"),  # throwaway test container mount
    Path("/app/config/palace.yaml"),  # orchestrator image layout
]


def real_palace_yaml() -> Path:
    """The repo's data/palace.yaml (the real config is what these tests guard)."""
    for p in PALACE_CANDIDATES:
        if p.is_file():
            return p
    pytest.fail(f"repo data/palace.yaml not found in any of {PALACE_CANDIDATES}")


class FakeEmbedder:
    """Stands in for ``shared.embedding_model``.

    ``encode(text)`` returns one vector, ``encode([texts])`` one row per text,
    both numpy arrays (so ``.tolist()`` works like the real model's output).
    Records each call's input and thread.
    """

    def __init__(self, vector=(0.1, 0.2)):
        self.vector = list(vector)
        self.calls: list = []
        self.threads: list[int] = []

    def encode(self, texts, normalize_embeddings=True, **_kw):
        self.calls.append(texts)
        self.threads.append(threading.get_ident())
        if isinstance(texts, list):
            return np.asarray([self.vector] * len(texts))
        return np.asarray(self.vector)


def _empty_collection() -> MagicMock:
    col = MagicMock()
    col.query.return_value = {"documents": [[]], "distances": [[]], "ids": [[]], "metadatas": [[]]}
    col.get.return_value = {"ids": [], "documents": [], "metadatas": []}
    col.add.return_value = None
    return col


@pytest.fixture
def memory_palace(monkeypatch):
    """Factory: ``build(config=None, yaml_path=...) -> MemPalace``.

    ``orchestrator.mempalace.shared`` is a fake for the whole test (empty
    MagicMock collection as ``palace._collection``, ``FakeEmbedder``,
    encryption off), and mempalace's ``encrypt_text`` / ``decrypt_text`` are
    the identity. ``maybe_decrypt`` (auto_learn's) stays real. ``config``
    replaces whatever the YAML gave; ``build.shared`` is the fake.
    """
    from orchestrator import mempalace
    from orchestrator import shared as real_shared

    fake = MagicMock()
    fake.PALACE_ENABLED = True
    fake.PALACE_WAKEUP_ENABLED = False
    fake.PALACE_DEDUP_THRESHOLD = 0.85
    fake.AUTO_LEARN_ENCRYPT = False
    fake.MEMORY_FACT_SOURCES = real_shared.MEMORY_FACT_SOURCES
    fake.embedding_model = FakeEmbedder([0.1] * 8)
    monkeypatch.setattr(mempalace, "shared", fake)
    monkeypatch.setattr(mempalace, "encrypt_text", lambda t: t)
    monkeypatch.setattr(mempalace, "decrypt_text", lambda t: t)

    def build(config=None, yaml_path="/nonexistent"):
        fake.PALACE_YAML_PATH = str(yaml_path)
        fake.collection = _empty_collection()
        palace = mempalace.MemPalace()
        if config is not None:
            palace._config = config
            palace._routing_rules = config.get("routing_rules", [])
        return palace

    build.shared = fake
    return build


# ---------------------------------------------------------------------------
# auto_learn encryption
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_fernet(monkeypatch):
    """A fresh Fernet cipher installed as auto_learn's cipher."""
    from cryptography.fernet import Fernet

    from orchestrator import auto_learn

    f = Fernet(Fernet.generate_key())
    monkeypatch.setattr(auto_learn, "_cipher", f)
    return f


@pytest.fixture
def memory_key_file(monkeypatch, tmp_path):
    """auto_learn pointed at ``tmp_path/keys/auto_learn.key`` (not created), encryption
    on, no env key, no cached cipher. Returns ``(auto_learn, key_file)``."""
    from orchestrator import auto_learn, shared

    key_file = tmp_path / "keys" / "auto_learn.key"
    monkeypatch.setattr(auto_learn, "_KEY_FILE", str(key_file))
    monkeypatch.setattr(auto_learn, "_cipher", None)
    monkeypatch.setattr(shared, "AUTO_LEARN_ENCRYPT", True)
    monkeypatch.setattr(shared, "AUTO_LEARN_ENCRYPTION_KEY", "")
    return auto_learn, key_file


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def make_settings(**env: str):
    """A fresh ``Settings`` from exactly ``env`` plus MODEL_URL / MODEL_NAME.

    No host process env (``clear=True``) and no deployed /app/.env
    (``_env_file=None``), so it behaves the same in CI, the throwaway test
    container and a configured box. Never reloads the module, so the shared
    ``settings`` singleton other tests hold is untouched.
    """
    from orchestrator.config import Settings

    full = {"MODEL_URL": "http://localhost:8080/v1", "MODEL_NAME": "test-model", **env}
    with patch.dict(os.environ, full, clear=True):
        return Settings(_env_file=None)


# ---------------------------------------------------------------------------
# Paperless
# ---------------------------------------------------------------------------

PAPERLESS_BASE = "http://paperless.test:8777"


@pytest.fixture
def memory_paperless_on(monkeypatch):
    """Paperless enabled on the live settings singleton. The URL carries a
    trailing slash, which every client path must strip."""
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "paperless_enabled", True, raising=False)
    monkeypatch.setattr(settings, "paperless_url", PAPERLESS_BASE + "/", raising=False)
    monkeypatch.setattr(settings, "paperless_api_token", "tok-abcdefgh", raising=False)
    monkeypatch.setattr(settings, "paperless_default_tags", "", raising=False)
    monkeypatch.setattr(settings, "paperless_upload_timeout_seconds", 30, raising=False)
    return settings
