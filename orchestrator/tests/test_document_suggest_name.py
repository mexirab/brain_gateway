"""POST /api/documents/suggest-name — vision-suggested title + category for a document photo."""

import asyncio
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator import routes_documents as rd

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


def test_json_wrapped_in_prose_is_found(client, monkeypatch):
    _vision_reply(monkeypatch, 'Sure!\n```json\n{"title": "Lease", "category": "housing"}\n```')
    assert _post(client).json() == {"ok": True, "title": "Lease", "category": "housing"}


def test_unknown_category_becomes_other(client, monkeypatch):
    _vision_reply(monkeypatch, '{"title": "Gym card", "category": "fitness"}')
    assert _post(client).json()["category"] == "other"


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


def test_empty_title_is_unparseable(client, monkeypatch):
    _vision_reply(monkeypatch, '{"title": "  **  ", "category": "auto"}')
    assert _post(client).json() == {"ok": False, "reason": "unreadable"}


def test_garbage_reply_is_unparseable(client, monkeypatch):
    _vision_reply(monkeypatch, "I can't tell what this is.")
    assert _post(client).json() == {"ok": False, "reason": "unreadable"}


def test_non_dict_json_is_unparseable(client, monkeypatch):
    _vision_reply(monkeypatch, '["a", "b"] {not json}')
    assert _post(client).json()["ok"] is False


def test_vision_error_sentence_is_vision_error(client, monkeypatch):
    _vision_reply(monkeypatch, "[Vision model returned an error: 500]")
    assert _post(client).json() == {"ok": False, "reason": "vision_error"}


def test_vision_disabled_sentence_is_vision_off(client, monkeypatch):
    _vision_reply(monkeypatch, "[Vision is disabled. Enable VISION_ENABLED=true to analyze images.]")
    assert _post(client).json() == {"ok": False, "reason": "vision_unavailable"}


def test_second_object_after_prose_still_found(client, monkeypatch):
    _vision_reply(monkeypatch, 'Thinking {about it}. {"title": "Tax return 2025", "category": "financial"} done {x}')
    assert _post(client).json() == {"ok": True, "title": "Tax return 2025", "category": "financial"}


def test_busy_while_another_read_runs(client, monkeypatch):
    calls = _vision_reply(monkeypatch, '{"title": "x"}')

    async def held():
        await rd._suggest_lock.acquire()

    loop = asyncio.new_event_loop()
    loop.run_until_complete(held())
    try:
        assert _post(client).json() == {"ok": False, "reason": "busy"}
    finally:
        rd._suggest_lock.release()
        loop.close()
    assert calls == []


def test_vision_timeout_sentence(client, monkeypatch):
    _vision_reply(monkeypatch, "[Vision model timed out. The image may be too complex or the model may be busy.]")
    assert _post(client).json() == {"ok": False, "reason": "timeout"}


def test_outer_timeout(client, monkeypatch):
    async def slow(*_a, **_k):
        await asyncio.sleep(5)
        return '{"title": "late"}'

    monkeypatch.setattr("orchestrator.vision_handler.analyze_image", slow)
    monkeypatch.setattr(rd, "_SUGGEST_TIMEOUT_S", 0.05)
    assert _post(client).json() == {"ok": False, "reason": "timeout"}


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


def test_route_has_own_cap_and_length_required():
    from orchestrator.orchestrator import _LENGTH_REQUIRED_PATHS, _PATH_UPLOAD_LIMITS

    assert _PATH_UPLOAD_LIMITS["/api/documents/suggest-name"] == 16 * 1024 * 1024
    assert "/api/documents/suggest-name" in _LENGTH_REQUIRED_PATHS


def test_upload_runs_ocr_off_loop_and_survives_index_failure(client, monkeypatch, caplog):
    """POST /api/documents: doc id exists even when RAG indexing blows up; error text not logged."""
    import threading

    from orchestrator import document_processor as dp
    from orchestrator import shared, state_store

    main_thread = threading.get_ident()
    seen = {}

    def fake_extract(_path):
        seen["thread"] = threading.get_ident()
        return "Invoice from Example Clinic"

    class BoomCollection:
        def add(self, **_k):
            raise RuntimeError("chunk text Example Clinic leaked")

    class Enc:
        def encode(self, _c, normalize_embeddings=True):
            class V:
                def tolist(self):
                    return [0.0]

            return V()

    monkeypatch.setattr(dp, "validate_upload", lambda *_a: None)
    monkeypatch.setattr(dp, "save_uploaded_file", lambda *_a: "other/x.jpg")
    monkeypatch.setattr(dp, "extract_text", fake_extract)
    monkeypatch.setattr(shared, "collection", BoomCollection())
    monkeypatch.setattr(shared, "embedding_model", Enc())
    monkeypatch.setattr(state_store, "save_document", lambda d: dict(d))

    with caplog.at_level(logging.DEBUG):
        r = client.post(
            "/api/documents",
            files={"file": ("scan.jpg", JPEG, "image/jpeg")},
            data={"title": "Clinic invoice", "category": "medical"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["id"] and body["rag_doc_id"] == f"vault_{body['id']}"
    assert seen["thread"] != main_thread
    assert "Example Clinic" not in caplog.text


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


def test_no_http_client_sentence_is_vision_error_not_crash(client, monkeypatch):
    _vision_reply(monkeypatch, "[Vision unavailable: no HTTP client.]")
    assert _post(client).json() == {"ok": False, "reason": "vision_error"}


def test_empty_reply_is_unparseable(client, monkeypatch):
    # thinking=False on the brain can still yield an empty content string
    _vision_reply(monkeypatch, "")
    assert _post(client).json() == {"ok": False, "reason": "unreadable"}


def test_json_array_reply_is_parsed(client, monkeypatch):
    _vision_reply(monkeypatch, '[{"title": "Lease", "category": "housing"}]')
    assert _post(client).json() == {"ok": True, "title": "Lease", "category": "housing"}


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
    assert _post(client).json()["reason"] == "timeout"
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
    from orchestrator.metrics import DOCUMENT_NAME_SUGGEST

    return DOCUMENT_NAME_SUGGEST.labels(result=result)._value.get()


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
    from orchestrator.metrics import DOCUMENT_NAME_SUGGEST

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
    assert DOCUMENT_NAME_SUGGEST is not None


def test_metric_busy(client, monkeypatch):
    _vision_reply(monkeypatch, '{"title": "x"}')
    before = _metric("busy")
    loop = asyncio.new_event_loop()
    loop.run_until_complete(rd._suggest_lock.acquire())
    try:
        _post(client)
    finally:
        rd._suggest_lock.release()
        loop.close()
    assert _metric("busy") == before + 1


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


def test_suggest_name_chunked_body_411(size_app):
    c, _ = size_app
    assert c.post("/api/documents/suggest-name", content=(b for b in [b"a", b"b"])).status_code == 411


# ---------------------------------------------------------------------------
# POST /api/documents happy path — chunks indexed with vault_doc_id
# ---------------------------------------------------------------------------


def _upload_env(monkeypatch, extracted):
    import threading

    from orchestrator import document_processor as dp
    from orchestrator import shared, state_store

    rec = {"main": threading.get_ident()}

    class Coll:
        def add(self, **kw):
            rec["add"] = kw
            rec["add_thread"] = threading.get_ident()

    class Enc:
        def encode(self, _c, normalize_embeddings=True):
            rec["enc_thread"] = threading.get_ident()

            class V:
                def tolist(self):
                    return [0.1, 0.2]

            return V()

    def save(d):
        rec["saved"] = dict(d)
        return dict(d)

    monkeypatch.setattr(dp, "validate_upload", lambda *_a: None)
    monkeypatch.setattr(dp, "save_uploaded_file", lambda *_a: "medical/x.jpg")
    monkeypatch.setattr(dp, "extract_text", lambda _p: extracted)
    monkeypatch.setattr(shared, "collection", Coll())
    monkeypatch.setattr(shared, "embedding_model", Enc())
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
    assert rec["enc_thread"] != rec["main"]
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
