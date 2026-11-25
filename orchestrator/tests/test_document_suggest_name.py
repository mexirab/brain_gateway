"""POST /api/documents/suggest-name — vision-suggested title + category for a document photo."""

import asyncio
import logging
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator import routes_documents as rd
from orchestrator.metrics import DOCUMENT_NAME_SUGGEST
from orchestrator.tests._helpers import metric_value
from orchestrator.tests.fixtures_memory import FakeEmbedder

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


@pytest.fixture
def client(monkeypatch):
    async def healthy(*_a, **_k):
        return True

    monkeypatch.setattr("orchestrator.vision_handler.check_vision_health", healthy)
    app = FastAPI()
    app.include_router(rd.router)
    return TestClient(app)


def _vision_reply(monkeypatch, reply):
    calls = []

    async def fake(image_data, prompt="", http_client=None, thinking=True):
        calls.append({"image_data": image_data, "prompt": prompt, "thinking": thinking})
        return reply

    monkeypatch.setattr("orchestrator.vision_handler.analyze_image", fake)
    return calls


def _post(client, data=JPEG):
    return client.post("/api/documents/suggest-name", files={"file": ("photo.jpg", data, "image/jpeg")})


def test_valid_reply_returns_title_and_category(client, monkeypatch):
    calls = _vision_reply(monkeypatch, '{"title": "State Farm auto renewal 2026", "category": "insurance"}')
    r = _post(client)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "title": "State Farm auto renewal 2026", "category": "insurance"}
    assert calls[0]["thinking"] is False
    assert calls[0]["image_data"].startswith("data:image/jpeg;base64,")


def test_title_markup_newlines_and_length_cleaned(client, monkeypatch):
    long_tail = "word " * 40
    _vision_reply(
        monkeypatch, '{"title": "<b>**Water bill**</b>\\n[click](x) ' + long_tail + '", "category": "HOUSING"}'
    )
    body = _post(client).json()
    assert body["ok"] is True
    assert body["category"] == "housing"
    title = body["title"]
    assert len(title) <= rd._TITLE_MAX
    assert "\n" not in title
    assert not any(ch in title for ch in "<>*[]`")
    assert title.startswith("Water bill")


@pytest.mark.parametrize(
    "reply,expected",
    [
        pytest.param(
            'Sure!\n```json\n{"title": "Lease", "category": "housing"}\n```',
            {"ok": True, "title": "Lease", "category": "housing"},
            id="json-in-prose",
        ),
        pytest.param(
            '{"title": "Gym card", "category": "fitness"}',
            {"ok": True, "title": "Gym card", "category": "other"},
            id="unknown-category-other",
        ),
        pytest.param(
            'Thinking {about it}. {"title": "Tax return 2025", "category": "financial"} done {x}',
            {"ok": True, "title": "Tax return 2025", "category": "financial"},
            id="second-object-after-prose",
        ),
        pytest.param(
            '[{"title": "Lease", "category": "housing"}]',
            {"ok": True, "title": "Lease", "category": "housing"},
            id="json-array",
        ),
        pytest.param(
            '{"title": "  **  ", "category": "auto"}', {"ok": False, "reason": "unreadable"}, id="empty-title"
        ),
        pytest.param("I can't tell what this is.", {"ok": False, "reason": "unreadable"}, id="garbage"),
        # thinking=False on the brain can still yield an empty content string
        pytest.param("", {"ok": False, "reason": "unreadable"}, id="empty-reply"),
        pytest.param(
            "[Vision model returned an error: 500]", {"ok": False, "reason": "vision_error"}, id="vision-error"
        ),
        pytest.param(
            "[Vision unavailable: no HTTP client.]", {"ok": False, "reason": "vision_error"}, id="no-http-client"
        ),
        pytest.param(
            "[Vision is disabled. Enable VISION_ENABLED=true to analyze images.]",
            {"ok": False, "reason": "vision_unavailable"},
            id="vision-disabled",
        ),
        pytest.param(
            "[Vision model timed out. The image may be too complex or the model may be busy.]",
            {"ok": False, "reason": "timeout"},
            id="vision-timeout",
        ),
    ],
)
def test_reply_classified(client, monkeypatch, reply, expected):
    _vision_reply(monkeypatch, reply)
    assert _post(client).json() == expected


def test_non_dict_json_is_not_ok(client, monkeypatch):
    # Classified as vision_error today: a reply opening with "[" reads like a
    # bracketed vision error sentence. Only "not ok" is pinned.
    _vision_reply(monkeypatch, '["a", "b"] {not json}')
    assert _post(client).json()["ok"] is False


def test_busy_while_another_read_runs(client, monkeypatch):
    calls = _vision_reply(monkeypatch, '{"title": "x"}')
    before = _metric("busy")
    loop = asyncio.new_event_loop()
    loop.run_until_complete(rd._suggest_lock.acquire())
    try:
        assert _post(client).json() == {"ok": False, "reason": "busy"}
    finally:
        rd._suggest_lock.release()
        loop.close()
    assert calls == []
    assert _metric("busy") == before + 1


def test_vision_unavailable_fails_fast(client, monkeypatch):
    async def down(*_a, **_k):
        return False

    calls = _vision_reply(monkeypatch, '{"title": "x"}')
    monkeypatch.setattr("orchestrator.vision_handler.check_vision_health", down)
    assert _post(client).json() == {"ok": False, "reason": "vision_unavailable"}
    assert calls == []


@pytest.mark.parametrize("data", [b"%PDF-1.7 not an image", b"", b"<svg></svg>"])
def test_non_image_rejected(client, monkeypatch, data):
    calls = _vision_reply(monkeypatch, '{"title": "x"}')
    r = _post(client, data)
    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert calls == []


def test_oversized_rejected(client, monkeypatch):
    calls = _vision_reply(monkeypatch, '{"title": "x"}')
    monkeypatch.setattr(rd, "_SUGGEST_MAX_BYTES", 32)
    r = _post(client)
    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert calls == []


def test_vision_size_cap_applies(client, monkeypatch):
    calls = _vision_reply(monkeypatch, '{"title": "x"}')
    monkeypatch.setattr("orchestrator.shared.VISION_MAX_IMAGE_SIZE", 32)
    assert _post(client).status_code == 400
    assert calls == []


def test_title_never_logged(client, monkeypatch, caplog):
    _vision_reply(monkeypatch, '{"title": "Secret Clinic Invoice", "category": "medical"}')
    with caplog.at_level(logging.DEBUG):
        assert _post(client).json()["ok"] is True
    assert "Secret Clinic" not in caplog.text


# ---------------------------------------------------------------------------
# _clean_title — truncation and cleanup edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", 0, "   ", "\n\t", "**__``"])
def test_clean_title_empty_inputs(raw):
    assert rd._clean_title(raw) == ""


def test_clean_title_non_string_is_stringified():
    assert rd._clean_title(2026) == "2026"


def test_clean_title_exactly_max_is_untouched():
    title = ("abcd " * 16).strip()  # 79 chars
    title = title + "e"  # 80 chars, last word "abcde"
    assert len(title) == rd._TITLE_MAX
    assert rd._clean_title(title) == title


def test_clean_title_truncates_at_word_boundary():
    title = "a" * 70 + " bbbbbbbbbbbbbbbbbbbb"  # 91 chars, second word straddles the cut
    out = rd._clean_title(title)
    assert out == "a" * 70
    assert len(out) <= rd._TITLE_MAX


def test_clean_title_cut_landing_on_space():
    title = "a" * 80 + " tail"  # text[:80] is one whole word, no space inside it
    assert rd._clean_title(title) == "a" * 80


def test_clean_title_cut_right_after_space():
    title = "a" * 79 + " tail"  # text[:80] ends with the space
    assert rd._clean_title(title) == "a" * 79


def test_clean_title_single_long_word_hard_cut():
    assert rd._clean_title("x" * 200) == "x" * rd._TITLE_MAX


def test_clean_title_strips_controls_markup_and_edge_punctuation():
    assert rd._clean_title('  "Lease\x00 <i>agreement</i>\t2026."  ') == "Lease agreement 2026"
    assert rd._clean_title("# Title | with ~pipes~ \\ and {braces}") == "Title with pipes and braces"


def test_clean_title_keeps_unicode_and_inner_punctuation():
    assert rd._clean_title("Café receipt — Dr. O'Neil, Mar 2026") == "Café receipt — Dr. O'Neil, Mar 2026"


# ---------------------------------------------------------------------------
# _first_json_object / _parse_suggestion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "no braces", "{", "{not json} {also: not}", "[1, 2, 3]", '"just a string"'])
def test_first_json_object_none(text):
    assert rd._first_json_object(text) is None


def test_first_json_object_skips_leading_non_dict_json():
    assert rd._first_json_object('[1, 2] 42 {"title": "X"}') == {"title": "X"}


def test_first_json_object_returns_outer_dict_when_nested():
    assert rd._first_json_object('{"meta": {"title": "inner"}, "title": "outer"}')["title"] == "outer"


def test_first_json_object_finds_dict_inside_array():
    # Scans for "{", so an object inside a top-level array is still returned.
    assert rd._first_json_object('[{"title": "Inner"}]') == {"title": "Inner"}


def test_first_json_object_braces_inside_strings():
    assert rd._first_json_object('{"title": "a {b} c", "category": "auto"}') == {
        "title": "a {b} c",
        "category": "auto",
    }


def test_parse_suggestion_category_normalised():
    assert rd._parse_suggestion('{"title": "Pay stub", "category": "  Financial "}') == {
        "title": "Pay stub",
        "category": "financial",
    }


@pytest.mark.parametrize("cat", ["null", "123", '["medical"]', '""'])
def test_parse_suggestion_odd_category_becomes_other(cat):
    assert rd._parse_suggestion('{"title": "Thing", "category": ' + cat + "}")["category"] == "other"


def test_parse_suggestion_missing_title_none():
    assert rd._parse_suggestion('{"category": "medical"}') is None
    assert rd._parse_suggestion(None) is None


def test_parse_suggestion_outer_without_title_does_not_fall_through_to_inner():
    # The first dict wins even if only a nested one carries a title.
    assert rd._parse_suggestion('{"doc": {"title": "inner"}}') is None


# ---------------------------------------------------------------------------
# Route: classification details, lock release, metric per result
# ---------------------------------------------------------------------------


def test_png_and_webp_accepted_with_right_mime(client, monkeypatch):
    calls = _vision_reply(monkeypatch, '{"title": "Bill", "category": "housing"}')
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    webp = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 32
    assert _post(client, png).json()["ok"] is True
    assert _post(client, webp).json()["ok"] is True
    assert calls[0]["image_data"].startswith("data:image/png;base64,")
    assert calls[1]["image_data"].startswith("data:image/webp;base64,")


def test_prompt_lists_categories_with_other_last(client, monkeypatch):
    from orchestrator.document_processor import VALID_CATEGORIES

    calls = _vision_reply(monkeypatch, '{"title": "x", "category": "auto"}')
    _post(client)
    prompt = calls[0]["prompt"]
    for c in VALID_CATEGORIES:
        assert c in prompt
    assert ", other." in prompt
    assert "{categories}" not in prompt


def test_image_exactly_at_cap_is_accepted(client, monkeypatch):
    _vision_reply(monkeypatch, '{"title": "Edge", "category": "auto"}')
    monkeypatch.setattr(rd, "_SUGGEST_MAX_BYTES", len(JPEG))
    assert _post(client).json()["ok"] is True
    monkeypatch.setattr(rd, "_SUGGEST_MAX_BYTES", len(JPEG) - 1)
    assert _post(client).status_code == 400


def test_lock_released_after_timeout_and_vision_off(client, monkeypatch):
    async def slow(*_a, **_k):
        await asyncio.sleep(5)

    monkeypatch.setattr("orchestrator.vision_handler.analyze_image", slow)
    monkeypatch.setattr(rd, "_SUGGEST_TIMEOUT_S", 0.05)
    assert _post(client).json() == {"ok": False, "reason": "timeout"}
    assert not rd._suggest_lock.locked()

    async def down(*_a, **_k):
        return False

    monkeypatch.setattr("orchestrator.vision_handler.check_vision_health", down)
    assert _post(client).json()["reason"] == "vision_unavailable"
    assert not rd._suggest_lock.locked()

    _vision_reply(monkeypatch, '{"title": "Next", "category": "auto"}')
    monkeypatch.setattr("orchestrator.vision_handler.check_vision_health", lambda *_a, **_k: _true())
    assert _post(client).json()["ok"] is True


async def _true():
    return True


def test_lock_released_when_analyze_image_raises(monkeypatch):
    async def healthy(*_a, **_k):
        return True

    async def boom(*_a, **_k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("orchestrator.vision_handler.check_vision_health", healthy)
    monkeypatch.setattr("orchestrator.vision_handler.analyze_image", boom)
    app = FastAPI()
    app.include_router(rd.router)
    c = TestClient(app, raise_server_exceptions=False)
    assert c.post("/api/documents/suggest-name", files={"file": ("p.jpg", JPEG, "image/jpeg")}).status_code == 500
    assert not rd._suggest_lock.locked()


def _metric(result):
    return metric_value(DOCUMENT_NAME_SUGGEST, result=result)


async def _false(*_a, **_k):
    return False


@pytest.mark.parametrize(
    "result,setup",
    [
        ("ok", lambda mp: _vision_reply(mp, '{"title": "T", "category": "auto"}')),
        ("unparseable", lambda mp: _vision_reply(mp, "nope")),
        ("vision_error", lambda mp: _vision_reply(mp, "[Vision analysis failed. Please try again.]")),
        ("timeout", lambda mp: _vision_reply(mp, "[Vision model timed out. x]")),
        ("vision_off", lambda mp: _vision_reply(mp, "[Vision is disabled. x]")),
        ("vision_off", lambda mp: mp.setattr("orchestrator.vision_handler.check_vision_health", _false)),
        ("bad_image", None),
    ],
)
def test_metric_incremented_once_per_result(client, monkeypatch, result, setup):
    if setup:
        setup(monkeypatch)
    else:
        _vision_reply(monkeypatch, "{}")
    all_results = ("ok", "vision_off", "timeout", "vision_error", "unparseable", "busy", "bad_image")
    before = {r: _metric(r) for r in all_results}
    _post(client, b"not an image" if result == "bad_image" else JPEG)
    after = {r: _metric(r) for r in all_results}
    assert after[result] == before[result] + 1
    assert all(after[r] == before[r] for r in all_results if r != result)


# ---------------------------------------------------------------------------
# Middleware: per-path 16 MB cap on Content-Length
# ---------------------------------------------------------------------------


@pytest.fixture
def size_app():
    from orchestrator.orchestrator import MAX_UPLOAD_SIZE, RequestSizeLimitMiddleware

    app = FastAPI()
    app.add_middleware(RequestSizeLimitMiddleware)

    @app.post("/api/documents/suggest-name")
    async def _suggest():
        return {"ok": True}

    @app.post("/api/documents")
    async def _upload():
        return {"ok": True}

    return TestClient(app), MAX_UPLOAD_SIZE


def test_suggest_name_over_16mb_is_413(size_app):
    c, _ = size_app
    limit = 16 * 1024 * 1024
    r = c.post("/api/documents/suggest-name", content=b"x", headers={"content-length": str(limit + 1)})
    assert r.status_code == 413
    assert str(limit) in r.json()["error"]


def test_suggest_name_at_16mb_passes_middleware(size_app):
    c, _ = size_app
    r = c.post("/api/documents/suggest-name", content=b"x", headers={"content-length": str(16 * 1024 * 1024)})
    assert r.status_code == 200


def test_suggest_name_cap_is_tighter_than_general_upload(size_app):
    c, max_upload = size_app
    size = str(20 * 1024 * 1024)
    assert max_upload > 20 * 1024 * 1024  # precondition: default DOCUMENT_MAX_SIZE_MB
    assert c.post("/api/documents/suggest-name", content=b"x", headers={"content-length": size}).status_code == 413
    assert c.post("/api/documents", content=b"x", headers={"content-length": size}).status_code == 200


# ---------------------------------------------------------------------------
# POST /api/documents happy path — chunks indexed with vault_doc_id
# ---------------------------------------------------------------------------


def _upload_env(monkeypatch, extracted, add_error=None):
    from orchestrator import document_processor as dp
    from orchestrator import shared, state_store

    rec = {"main": threading.get_ident(), "embedder": FakeEmbedder([0.1, 0.2])}

    class Coll:
        def add(self, **kw):
            rec["add_thread"] = threading.get_ident()
            if add_error is not None:
                raise add_error
            rec["add"] = kw

    def extract(_path):
        rec["extract_thread"] = threading.get_ident()
        return extracted

    def save(d):
        rec["saved"] = dict(d)
        return dict(d)

    monkeypatch.setattr(dp, "validate_upload", lambda *_a: None)
    monkeypatch.setattr(dp, "save_uploaded_file", lambda *_a: "medical/x.jpg")
    monkeypatch.setattr(dp, "extract_text", extract)
    monkeypatch.setattr(shared, "collection", Coll())
    monkeypatch.setattr(shared, "embedding_model", rec["embedder"])
    monkeypatch.setattr(state_store, "save_document", save)
    return rec


def _upload(client, category="medical"):
    return client.post(
        "/api/documents",
        files={"file": ("scan.jpg", JPEG, "image/jpeg")},
        data={"title": "Clinic invoice", "category": category},
    )


def test_upload_indexes_chunks_with_vault_doc_id(client, monkeypatch):
    rec = _upload_env(monkeypatch, "lorem ipsum " * 400)  # ~4800 chars → several chunks
    r = _upload(client)
    assert r.status_code == 200
    body = r.json()
    doc_id = body["id"]
    assert body["rag_doc_id"] == f"vault_{doc_id}"
    assert "extracted_text" not in body

    add = rec["add"]
    n = len(add["documents"])
    assert n >= 2
    assert add["ids"] == [f"vault_{doc_id}_chunk_{i}" for i in range(n)]
    assert len(add["embeddings"]) == n and add["embeddings"][0] == [0.1, 0.2]
    assert len(add["metadatas"]) == n
    for md in add["metadatas"]:
        assert md["vault_doc_id"] == doc_id
        assert md["source"] == "document_vault"
        assert md["kind"] == "chunk"
        assert (md["wing"], md["room"]) == ("personal", "health")
        assert md["category"] == "medical"
    assert rec["add_thread"] != rec["main"]
    assert rec["extract_thread"] != rec["main"]
    assert all(t != rec["main"] for t in rec["embedder"].threads)
    assert rec["saved"]["id"] == doc_id
    assert rec["saved"]["extracted_text"].startswith("lorem ipsum")


def test_upload_without_text_skips_index_but_keeps_ids(client, monkeypatch):
    rec = _upload_env(monkeypatch, "")
    body = _upload(client, "auto").json()
    assert "add" not in rec
    assert body["rag_doc_id"] == f"vault_{body['id']}"


def test_upload_validation_error_is_400(client, monkeypatch):
    from orchestrator import document_processor as dp

    rec = _upload_env(monkeypatch, "text")
    monkeypatch.setattr(dp, "validate_upload", lambda *_a: "Invalid category")
    r = _upload(client, "bogus")
    assert r.status_code == 400
    assert "saved" not in rec and "add" not in rec


def test_upload_survives_index_failure_without_logging_its_text(client, monkeypatch, caplog):
    """The doc id exists even when RAG indexing blows up; the error text is not logged."""
    rec = _upload_env(
        monkeypatch, "Invoice from Example Clinic", add_error=RuntimeError("chunk text Example Clinic leaked")
    )
    with caplog.at_level(logging.DEBUG):
        r = _upload(client)
    assert r.status_code == 200
    body = r.json()
    assert body["id"] and body["rag_doc_id"] == f"vault_{body['id']}"
    assert rec["extract_thread"] != rec["main"]  # OCR runs off the event loop
    assert "Example Clinic" not in caplog.text
