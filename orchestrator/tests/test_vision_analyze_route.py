"""POST /api/vision/analyze, JSON branch: only inline image data is analyzed.

A link (any scheme or letter case, protocol-relative included) or a
non-string ``image`` gets a 400 with one fixed message and never reaches
analyze_image; a data: URI or plain base64 is analyzed. A non-string
``image`` used to be a 500 (``.startswith`` on a dict).

Builds a minimal FastAPI app with only routes_vision.router; analyze_image is
an AsyncMock, so no vision model is called.
"""

from unittest.mock import AsyncMock

import pytest

from orchestrator import shared, vision_handler

URL = "/api/vision/analyze"
NOT_INLINE_ERROR = "Only inline image data (a data: URI or plain base64) accepted, not URLs"


@pytest.fixture
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator import routes_vision

    app = FastAPI()
    app.include_router(routes_vision.router)
    return TestClient(app)


@pytest.fixture
def analyze(monkeypatch):
    # The route imports analyze_image from vision_handler at call time.
    mock = AsyncMock(return_value="A red mug on a desk")
    monkeypatch.setattr(vision_handler, "analyze_image", mock)
    monkeypatch.setattr(shared, "VISION_ENABLED", True)
    return mock


@pytest.mark.parametrize(
    "image",
    [
        "http://127.0.0.1:9/ssrf-probe.png",
        "https://example.invalid/cat.png",
        "HTTP://127.0.0.1/upper.png",
        "Https://127.0.0.1/mixed.png",
        "//127.0.0.1/protocol-relative.png",
        "file:///etc/passwd",
        "gopher://127.0.0.1:70/x",
        {"url": "http://127.0.0.1/x.png"},
        ["http://127.0.0.1/x.png"],
        42,
        True,
    ],
    ids=[
        "http",
        "https",
        "upper-http",
        "mixed-case-https",
        "protocol-relative",
        "file",
        "gopher",
        "dict",
        "list",
        "int",
        "bool",
    ],
)
def test_link_or_non_string_image_is_rejected(client, analyze, image):
    resp = client.post(URL, json={"image": image, "prompt": "what is it?"})

    assert resp.status_code == 400
    assert resp.json() == {"ok": False, "error": NOT_INLINE_ERROR}
    assert "127.0.0.1" not in resp.text
    analyze.assert_not_awaited()


@pytest.mark.parametrize(
    "image",
    ["data:image/jpeg;base64,/9j/4AAQSkZJRg==", "data:image/png;base64,iVBORw0KGgo=", "/9j/4AAQSkZJRg=="],
    ids=["jpeg-data-uri", "png-data-uri", "raw-base64"],
)
def test_inline_image_data_is_analyzed(client, analyze, image):
    resp = client.post(URL, json={"image": image, "prompt": "what is it?"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "analysis": "A red mug on a desk"}
    analyze.assert_awaited_once_with(image, "what is it?")


@pytest.mark.parametrize("body", [{}, {"image": ""}, {"image": None}], ids=["missing", "empty", "null"])
def test_missing_image_is_a_400(client, analyze, body):
    resp = client.post(URL, json=body)

    assert resp.status_code == 400
    assert resp.json() == {"ok": False, "error": "No image data provided"}
    analyze.assert_not_awaited()


@pytest.mark.parametrize("body", [[1, 2], "data:image/png;base64,iVBORw0KGgo=", 7])
def test_non_object_json_body_is_a_400(client, analyze, body):
    resp = client.post(URL, json=body)

    assert resp.status_code == 400
    assert resp.json() == {"ok": False, "error": "JSON body must be an object"}
    analyze.assert_not_awaited()


def test_vision_disabled_is_a_503_before_any_check(client, analyze, monkeypatch):
    monkeypatch.setattr(shared, "VISION_ENABLED", False)

    resp = client.post(URL, json={"image": "http://127.0.0.1/x.png"})

    assert resp.status_code == 503
    assert resp.json()["ok"] is False
    analyze.assert_not_awaited()
