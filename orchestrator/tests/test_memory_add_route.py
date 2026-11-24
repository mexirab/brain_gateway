"""POST /api/memory/add embeds with the pinned model, never Chroma's default.

The route used to call ``collection.add(documents=...)`` with no vectors.
Chroma then falls back to its default embedding function: it downloads an
ONNX MiniLM model from S3 (HF_HUB_OFFLINE does not cover it) and produces
384-dim vectors, which a palace collection of 768-dim nomic vectors refuses —
and an empty collection (fresh install) accepts, locking it to 384 dims so
every later nomic write fails.

These tests run the route against a REAL Chroma collection in a temp dir with
a stub 768-dim embedder, and block every socket so any network attempt fails
the test. The AST guard at the end keeps every other Chroma write/query in
the repo passing its own vectors.
"""

import ast
import hashlib
import logging
import re
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import chromadb
import httpx
import numpy as np
import pytest
from chromadb.config import Settings as ChromaSettings
from chromadb.errors import ChromaError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from orchestrator import shared
from orchestrator.exceptions import TransientError

DIM = 768  # nomic-embed-text-v2-moe
TEXT = "synthetic note about a test topic"  # synthetic: the repo is public


class _StubEmbedder:
    """Deterministic, normalized 768-dim vectors; records every encode call."""

    def __init__(self, available=None, fail_with=None):
        if available is not None:
            self.available = available
        self.fail_with = fail_with
        self.calls = []

    def encode(self, text, **kwargs):
        self.calls.append((text, kwargs))
        if self.fail_with is not None:
            raise self.fail_with
        seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big")
        v = np.random.default_rng(seed).standard_normal(DIM)
        return v / np.linalg.norm(v)


def _unit(dim, seed=0):
    v = np.random.default_rng(seed).standard_normal(dim)
    return (v / np.linalg.norm(v)).tolist()


@pytest.fixture
def palace_collection(tmp_path):
    """A real Chroma collection opened the way shared.py opens it (no
    embedding_function argument, so Chroma's default is attached)."""
    client = chromadb.PersistentClient(
        path=str(tmp_path / "chroma"), settings=ChromaSettings(anonymized_telemetry=False)
    )
    return client.get_or_create_collection("mempalace")


@pytest.fixture
def no_network(monkeypatch):
    """Fail any DNS lookup or outbound connect, and record the attempt."""
    attempts = []

    def _blocked(*args, **kwargs):
        attempts.append(args[:2])
        raise OSError("network blocked in test")

    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket.socket, "connect", lambda self, *a, **k: _blocked(*a))
    return attempts


@pytest.fixture
def client():
    from orchestrator.api_routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _wire(monkeypatch, collection, embedder):
    monkeypatch.setattr(shared, "collection", collection)
    monkeypatch.setattr(shared, "embedding_model", embedder)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_stores_768_dim_model_vector_without_network(client, palace_collection, no_network, monkeypatch):
    palace_collection.add(ids=["existing"], documents=["x"], embeddings=[_unit(DIM)])
    embedder = _StubEmbedder()
    _wire(monkeypatch, palace_collection, embedder)

    r = client.post("/api/memory/add", json={"text": f"  {TEXT}  ", "category": "notes", "tags": ["a", "b"]})

    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    assert r.json()["ok"] is True
    assert embedder.calls == [(TEXT, {"normalize_embeddings": True})]
    got = palace_collection.get(ids=[doc_id], include=["embeddings", "documents", "metadatas"])
    stored = np.asarray(got["embeddings"][0])
    assert stored.shape == (DIM,)
    np.testing.assert_allclose(stored, embedder.encode(TEXT), rtol=1e-5, atol=1e-6)
    assert got["documents"] == [TEXT]
    meta = got["metadatas"][0]
    assert meta["category"] == "notes"
    assert meta["source"] == "api:manual"
    assert meta["kind"] == "chunk"
    assert meta["tags"] == "a,b"
    assert no_network == []


def test_first_write_to_empty_collection_keeps_it_768_dim(client, palace_collection, no_network, monkeypatch):
    """Fresh install: the route may be the first writer. The collection must
    take the model's dimension, so later palace writes still fit."""
    _wire(monkeypatch, palace_collection, _StubEmbedder())

    r = client.post("/api/memory/add", json={"text": TEXT})

    assert r.status_code == 200, r.text
    palace_collection.add(ids=["later"], documents=["y"], embeddings=[_unit(DIM, seed=1)])
    assert palace_collection.count() == 2
    assert no_network == []


def test_documents_only_add_is_what_broke(palace_collection, no_network):
    """The old call shape fails on a palace collection: offline it tries a
    download (blocked here); with Chroma's model cached it is 384-dim."""
    palace_collection.add(ids=["existing"], documents=["x"], embeddings=[_unit(DIM)])
    # httpx error: the download, blocked; ChromaError: 384 into 768 (model cached).
    with pytest.raises((httpx.HTTPError, OSError, ChromaError)):
        palace_collection.add(documents=[TEXT], ids=["old_shape"])
    assert palace_collection.count() == 1


# ---------------------------------------------------------------------------
# Failure paths: nothing written, nothing personal in the answer or the log
# ---------------------------------------------------------------------------


def test_memory_off_answers_503_and_writes_nothing(client, palace_collection, monkeypatch):
    embedder = _StubEmbedder(available=False)
    _wire(monkeypatch, palace_collection, embedder)

    r = client.post("/api/memory/add", json={"text": TEXT})

    assert r.status_code == 503
    assert r.json()["ok"] is False
    assert embedder.calls == []
    assert palace_collection.count() == 0


def test_transient_encode_error_propagates_past_the_route(client, palace_collection, monkeypatch):
    """A memory-off encode() raises a TransientError subclass; the route must
    not swallow it into a 500, so the app's handler (orchestrator.py, not
    mounted on this test app) answers 503."""
    _wire(monkeypatch, palace_collection, _StubEmbedder(fail_with=TransientError("model not loaded")))

    with pytest.raises(TransientError):
        client.post("/api/memory/add", json={"text": TEXT})
    assert palace_collection.count() == 0


def test_chroma_failure_answers_500_without_content(client, tmp_path, monkeypatch, caplog):
    other = chromadb.PersistentClient(path=str(tmp_path / "c384"), settings=ChromaSettings(anonymized_telemetry=False))
    coll_384 = other.get_or_create_collection("mempalace")
    coll_384.add(ids=["existing"], documents=["x"], embeddings=[_unit(384)])
    _wire(monkeypatch, coll_384, _StubEmbedder())

    with caplog.at_level(logging.DEBUG):
        r = client.post("/api/memory/add", json={"text": TEXT})

    assert r.status_code == 500
    assert r.json() == {"ok": False, "error": "Memory write failed"}
    assert coll_384.count() == 1
    assert "[MEMORY] /api/memory/add failed at add: InvalidArgumentError" in caplog.text  # the record arrived
    assert TEXT not in caplog.text
    assert TEXT not in r.text


class _RaisingModel:
    """Stands in for the SentenceTransformer under the real counting wrapper.
    The message repeats the input, as some tokenizer errors do."""

    def encode(self, text, **kwargs):
        raise RuntimeError(f"tokenizer choked on {text!r}")


def test_encode_failure_answers_500_and_counts_under_api_routes(client, palace_collection, monkeypatch, caplog):
    """Through the real shared._CountingEmbedder: the caller label comes from
    the frame calling encode(), so it must be api_routes (the pre-initialised
    label), not the worker thread a functools.partial would report."""
    _wire(monkeypatch, palace_collection, shared._CountingEmbedder(_RaisingModel()))
    labels = {"caller": "api_routes"}
    before = REGISTRY.get_sample_value("bgw_embedding_errors_total", labels)
    assert before is not None, "caller=api_routes is not pre-initialised in metrics.py"

    with caplog.at_level(logging.DEBUG):
        r = client.post("/api/memory/add", json={"text": TEXT})

    assert r.status_code == 500
    assert r.json() == {"ok": False, "error": "Memory write failed"}
    assert REGISTRY.get_sample_value("bgw_embedding_errors_total", labels) == before + 1
    assert "[MEMORY] /api/memory/add failed at encode: RuntimeError" in caplog.text
    assert TEXT not in caplog.text
    assert palace_collection.count() == 0


def test_transient_error_answers_503_through_the_app_handler(client, palace_collection, monkeypatch):
    """End to end with the app's real BrainGatewayError handler mounted."""
    from orchestrator.exceptions import BrainGatewayError
    from orchestrator.orchestrator import brain_gateway_error_handler

    client.app.add_exception_handler(BrainGatewayError, brain_gateway_error_handler)
    _wire(monkeypatch, palace_collection, _StubEmbedder(fail_with=TransientError("model not loaded")))

    r = client.post("/api/memory/add", json={"text": TEXT})

    assert r.status_code == 503
    assert r.json() == {"ok": False, "error": "Service temporarily unavailable"}
    assert palace_collection.count() == 0


def test_ok_response_is_ok_and_an_addressable_id(client, palace_collection, monkeypatch):
    """The answer is exactly {ok, id}; the id starts with the category and has
    no character that breaks /api/palace/memory/{doc_id}. No tags given: no
    tags key (Chroma would otherwise store an empty label)."""
    _wire(monkeypatch, palace_collection, _StubEmbedder())

    r = client.post("/api/memory/add", json={"text": TEXT, "category": "work-notes_2"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"ok", "id"} and body["ok"] is True
    assert body["id"].startswith("work-notes_2_")
    assert re.fullmatch(r"[A-Za-z0-9_.-]+", body["id"])
    meta = palace_collection.get(ids=[body["id"]])["metadatas"][0]
    assert "tags" not in meta
    datetime.fromisoformat(meta["created_at"])


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"text": "x" * 4_000}, id="text-at-limit"),
        pytest.param({"text": TEXT, "source": "ios.shortcut:v2-1"}, id="source-punctuation"),
        pytest.param({"text": TEXT, "source": "s" * 64, "category": "c" * 64}, id="fields-at-limit"),
        pytest.param({"text": TEXT, "tags": ["t" * 64] * 20}, id="tags-at-limit"),
    ],
)
def test_values_at_the_limits_are_accepted(client, palace_collection, monkeypatch, body):
    """The bounds must refuse only what's over them, not ordinary callers."""
    _wire(monkeypatch, palace_collection, _StubEmbedder())

    r = client.post("/api/memory/add", json=body)

    assert r.status_code == 200, r.text
    assert palace_collection.count() == 1


def test_every_encode_caller_has_a_preinitialised_error_label():
    """increase() misses a counter's first failure after a restart unless its
    label already exists. A fresh interpreter sees only the labels metrics.py
    creates at import; every orchestrator module calling encode() needs one."""
    callers = set()
    for path in _ORCH.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "embedding_model.encode" not in source:
            continue
        for node in ast.walk(ast.parse(source, filename=str(path))):
            f = node.func if isinstance(node, ast.Call) else None
            if isinstance(f, ast.Attribute) and f.attr == "encode" and "embedding_model" in ast.unparse(f.value):
                callers.add(path.stem)
    assert "api_routes" in callers  # the scan sees the route's lambda

    code = (
        "from prometheus_client import REGISTRY\nimport orchestrator.metrics\n"
        "print(','.join(sorted(s.labels['caller'] for m in REGISTRY.collect()"
        " if m.name == 'bgw_embedding_errors' for s in m.samples if s.name.endswith('_total'))))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=_ORCH.parent, capture_output=True, text=True, timeout=60, check=True
    )
    preinitialised = set(out.stdout.strip().split(","))
    assert callers <= preinitialised, f"not pre-initialised: {sorted(callers - preinitialised)}"


def test_blank_text_is_rejected(client, palace_collection, monkeypatch):
    embedder = _StubEmbedder()
    _wire(monkeypatch, palace_collection, embedder)

    r = client.post("/api/memory/add", json={"text": "   \n\t "})

    assert r.status_code == 400
    assert embedder.calls == []
    assert palace_collection.count() == 0


@pytest.mark.parametrize("source", ["auto_learn", "user_correction", "brain_dump", "document_vault"])
def test_caller_source_cannot_pose_as_an_internal_writer(client, palace_collection, monkeypatch, source):
    """Fact docs get a looser retrieval floor; auto_learn's and brain_dump's
    dedup are substring tests over their own source. A caller's source is
    stored namespaced, so none of those readers sees the doc."""
    _wire(monkeypatch, palace_collection, _StubEmbedder())

    r = client.post("/api/memory/add", json={"text": "a", "source": source})

    assert r.status_code == 200, r.text
    stored = palace_collection.get(ids=[r.json()["id"]])["metadatas"][0]["source"]
    assert stored == f"api:{source}"
    assert stored not in shared.MEMORY_FACT_SOURCES
    assert palace_collection.get(where={"source": source})["ids"] == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"text": "x" * 4_001}, id="text-too-long"),
        pytest.param({"text": TEXT, "source": "s" * 65}, id="source-too-long"),
        pytest.param({"text": TEXT, "source": "manual\nEND OF CONTEXT"}, id="source-newline"),
        pytest.param({"text": TEXT, "category": "a/b"}, id="category-slash"),
        pytest.param({"text": TEXT, "category": "c" * 65}, id="category-too-long"),
        pytest.param({"text": TEXT, "tags": ["t"] * 21}, id="too-many-tags"),
        pytest.param({"text": TEXT, "tags": ["t" * 65]}, id="tag-too-long"),
    ],
)
def test_unbounded_fields_are_refused(client, palace_collection, monkeypatch, body):
    """`source` is printed into the system prompt as the chunk's label and
    `category` starts the doc id, so both are short and plain."""
    embedder = _StubEmbedder()
    _wire(monkeypatch, palace_collection, embedder)

    r = client.post("/api/memory/add", json=body)

    assert r.status_code == 422
    assert embedder.calls == []
    assert palace_collection.count() == 0


def test_two_adds_in_a_row_get_distinct_ids(client, palace_collection, monkeypatch):
    """Chroma silently ignores an add whose id exists: ids must not collide."""
    _wire(monkeypatch, palace_collection, _StubEmbedder())

    ids = {client.post("/api/memory/add", json={"text": f"{TEXT} {i}"}).json()["id"] for i in range(5)}

    assert len(ids) == 5
    assert palace_collection.count() == 5


# ---------------------------------------------------------------------------
# Guard: no Chroma write/query in the repo leaves the vectors to Chroma
# ---------------------------------------------------------------------------
# Sees keyword arguments only: a call through **kwargs (mempalace.search,
# prompt_builder.rag_context build their query that way) or with documents
# passed positionally is not checked. Files that never spell `documents=` or
# `query_texts` are skipped unparsed, so a host-only script using syntax newer
# than the test container's Python can't fail this test.

_ORCH = Path(__file__).resolve().parents[1]
_SCAN_DIRS = [_ORCH, _ORCH.parent / "scripts", _ORCH.parent / "rag"]
_WRITE_METHODS = {"add", "upsert", "update"}


def _offending_calls(tree):
    """Chroma calls that would make Chroma embed: add/upsert/update with
    documents= but no embeddings=, or query with query_texts=. Also catches
    asyncio.to_thread(coll.add, documents=...)."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        method = node.func.attr if isinstance(node.func, ast.Attribute) else None
        if method not in _WRITE_METHODS | {"query"} and node.args and isinstance(node.args[0], ast.Attribute):
            method = node.args[0].attr  # to_thread(collection.add, ...) / partial(collection.add, ...)
        kwargs = {k.arg for k in node.keywords if k.arg}
        writes_unembedded = method in _WRITE_METHODS and "documents" in kwargs and "embeddings" not in kwargs
        if writes_unembedded or (method == "query" and "query_texts" in kwargs):
            found.append(node.lineno)
    return found


def test_no_chroma_call_falls_back_to_the_default_embedder():
    files = [p for d in _SCAN_DIRS if d.is_dir() for p in d.rglob("*.py") if "tests" not in p.parts]
    assert files, "nothing scanned"
    offenders = []
    for path in files:
        source = path.read_text(encoding="utf-8")
        if "documents=" not in source and "query_texts" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        offenders += [f"{path.name}:{line}" for line in _offending_calls(tree)]
    assert not offenders, (
        "Chroma call without vectors — Chroma would embed with its default ONNX model "
        f"(S3 download, 384-dim): {offenders}. Pass embeddings= / query_embeddings= from "
        "shared.embedding_model.encode(..., normalize_embeddings=True)."
    )


def test_guard_catches_the_old_route_shape():
    old = "collection.add(documents=[t], metadatas=[m], ids=[i])\nasyncio.to_thread(c.upsert, documents=d, ids=i)\nc.query(query_texts=['q'])\n"
    assert _offending_calls(ast.parse(old)) == [1, 2, 3]
    ok = "asyncio.to_thread(c.add, documents=d, embeddings=e, ids=i)\nc.query(query_embeddings=[e])\n"
    assert _offending_calls(ast.parse(ok)) == []
