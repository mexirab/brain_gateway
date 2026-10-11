"""Tests for the exercise how-to library (orchestrator/exercise_library.py),
its two routes in routes_workout.py, the workout_status "how_to" tool action,
and the backup exclusion of the (reconstructable) library dirs.

The library dir is redirected to tmp_path via settings.exercise_library_path;
the module-level mtime cache is reset around every test. The fetch path is
respx-mocked — no real network.
"""

import importlib.util
import json
import logging
import os
from pathlib import Path

import httpx
import pytest

from orchestrator import exercise_library as el

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16

_ENTRIES = [
    {
        "id": "Alternating_Renegade_Row",
        "name": "Alternating Renegade Row",
        "instructions": ["Get into a plank on two dumbbells.", "Row one, then the other."],
        "primary_muscles": ["middle back"],
        "equipment": "dumbbell",
        "frames": 2,
    },
    {
        "id": "Glute_Ham_Raise",
        "name": "Glute Ham Raise",
        "instructions": ["Lock your ankles.", "Lower under control."],
        "primary_muscles": ["hamstrings"],
        "equipment": "other",
        "frames": 2,
    },
    {
        # Same name as a catalog exercise whose alias is explicitly None.
        "id": "Kettlebell_Deadlift",
        "name": "Kettlebell Deadlift",
        "instructions": ["Wrong movement for our catalog."],
        "primary_muscles": ["hamstrings"],
        "equipment": "kettlebells",
        "frames": 1,
    },
]


def _reset_cache():
    with el._cache_lock:
        el._cache.update(mtime=None, by_id={}, by_name={})


@pytest.fixture
def lib_root(tmp_path, monkeypatch):
    """Point the library at tmp_path/exercise_library (not created)."""
    from orchestrator.config import settings

    root = tmp_path / "exercise_library"
    monkeypatch.setattr(settings, "exercise_library_path", str(root))
    _reset_cache()
    yield root
    _reset_cache()


def _write_library(root: Path, entries=None, meta_sha="deadbeef"):
    entries = _ENTRIES if entries is None else entries
    root.mkdir(parents=True, exist_ok=True)
    (root / "library.json").write_text(json.dumps(entries), encoding="utf-8")
    (root / "meta.json").write_text(json.dumps({"source_sha": meta_sha, "count": len(entries)}), encoding="utf-8")
    for e in entries:
        if isinstance(e, dict) and isinstance(e.get("frames"), int):
            d = root / "images" / e["id"]
            d.mkdir(parents=True, exist_ok=True)
            for n in range(e["frames"]):
                (d / f"{n}.jpg").write_bytes(JPEG)


@pytest.fixture
def library(lib_root):
    _write_library(lib_root)
    return lib_root


def _lookups(result):
    from orchestrator.metrics import EXERCISE_INFO_LOOKUPS

    return EXERCISE_INFO_LOOKUPS.labels(result=result)._value.get()


# ---------------------------------------------------------------------------
# _ALIASES drift guard
# ---------------------------------------------------------------------------


def test_every_seed_exercise_has_an_alias_decision():
    from orchestrator.exercises_seed import EXERCISES

    missing = [e["name"] for e in EXERCISES if e["name"] not in el._ALIASES]
    assert missing == [], f"add these to exercise_library._ALIASES (id or None): {missing}"


def test_alias_ids_are_valid_ids():
    bad = [v for v in el._ALIASES.values() if v is not None and not el._ID_RE.match(v)]
    assert bad == []


def test_norm():
    assert el._norm("  Farmer's  Carry! ") == "farmer s carry"
    assert el._norm("T-Bar Row") == el._norm("t bar row")
    assert el._norm("") == ""


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def test_alias_hit(library):
    info = el.get_info("Dumbbell Renegade Row")
    assert info["found"] is True
    assert info["library_available"] is True
    assert info["match"] == "Alternating Renegade Row"
    assert info["instructions"] == _ENTRIES[0]["instructions"]
    assert info["primary_muscles"] == ["middle back"]
    assert info["equipment"] == "dumbbell"
    sha7 = el.SOURCE_SHA[:7]
    assert info["frames"] == [
        f"/api/workouts/exercise-library/Alternating_Renegade_Row/0.jpg?v={sha7}",
        f"/api/workouts/exercise-library/Alternating_Renegade_Row/1.jpg?v={sha7}",
    ]
    assert info["video_url"].startswith("https://www.youtube.com/results?search_query=")


def test_alias_lookup_is_normalized(library):
    assert el.get_info("  dumbbell renegade-row ")["found"] is True


def test_alias_none_beats_same_named_library_entry(library):
    # The library has an entry literally named "Kettlebell Deadlift", but the
    # alias map says no faithful match → must not show it.
    info = el.get_info("Kettlebell Deadlift")
    assert info["found"] is False
    assert info["library_available"] is True
    assert "frames" not in info and "instructions" not in info
    assert "Kettlebell+Deadlift" in info["video_url"]


def test_alias_to_id_missing_from_library_is_not_found(library):
    # "Back Squat" → Barbell_Squat, which the fixture library doesn't have.
    # Must NOT fall through to name matching.
    assert el.get_info("Back Squat")["found"] is False


def test_exact_normalized_name_match(library):
    info = el.get_info("glute-ham   RAISE")
    assert info["found"] is True
    assert info["match"] == "Glute Ham Raise"


def test_no_fuzzy_match(library):
    assert el.get_info("Glute Ham Raises")["found"] is False


def test_name_is_stripped_and_capped(library):
    info = el.get_info("  " + "x" * 500 + "  ")
    assert info["name"] == "x" * 200


def test_library_missing(lib_root):
    assert el.is_available() is False
    info = el.get_info("Dumbbell Renegade Row")
    assert info["library_available"] is False
    assert info["found"] is False
    assert info["video_url"]


@pytest.mark.parametrize("body", ["{not json", json.dumps({"id": "x"}), json.dumps("str")])
def test_corrupt_library_is_empty_and_logged_once(lib_root, caplog, body):
    lib_root.mkdir(parents=True)
    (lib_root / "library.json").write_text(body, encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger="orchestrator.exercise_library"):
        first = el.get_info("Glute Ham Raise")
        second = el.get_info("Glute Ham Raise")
    for info in (first, second):
        assert info["library_available"] is False
        assert info["found"] is False
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "Unreadable" in r.getMessage()]
    assert len(errors) == 1


def test_malformed_entries_are_skipped(lib_root, caplog):
    good = _ENTRIES[1]
    bad = [
        "not a dict",
        {"id": "bad id with spaces", **{k: v for k, v in good.items() if k != "id"}},
        {**good, "id": "No_Frames", "frames": "2"},
        {**good, "id": "No_Name", "name": None},
        {**good, "id": "Bad_Instructions", "instructions": "do it"},
        {**good, "id": "Bad_Muscles", "primary_muscles": None},
        {**good, "id": "Bad_Equipment", "equipment": ["x"]},
    ]
    _write_library(lib_root, [good, *bad])
    with caplog.at_level(logging.WARNING, logger="orchestrator.exercise_library"):
        by_id, by_name = el._load()
    assert set(by_id) == {"Glute_Ham_Raise"}
    assert by_name == {"glute ham raise": "Glute_Ham_Raise"}
    assert any("Skipped 7 malformed" in r.getMessage() for r in caplog.records)
    assert el.get_info("Glute Ham Raise")["found"] is True


def test_reload_on_mtime_change(library):
    assert el.get_info("Glute Ham Raise")["found"] is True
    path = library / "library.json"
    path.write_text(json.dumps([_ENTRIES[0]]), encoding="utf-8")
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))
    assert el.get_info("Glute Ham Raise")["found"] is False
    assert el.get_info("Dumbbell Renegade Row")["found"] is True


def test_get_info_metric_increments(lib_root):
    before = _lookups("library_missing")
    el.get_info("Plank")
    assert _lookups("library_missing") == before + 1

    _write_library(lib_root)
    found0, nf0 = _lookups("found"), _lookups("not_found")
    el.get_info("Glute Ham Raise")
    el.get_info("Kettlebell Deadlift")
    el.get_info("Nonexistent Movement")
    assert _lookups("found") == found0 + 1
    assert _lookups("not_found") == nf0 + 2


# ---------------------------------------------------------------------------
# image_file
# ---------------------------------------------------------------------------


def test_image_file_valid(library):
    p = el.image_file("Alternating_Renegade_Row", 1)
    assert p is not None and p.read_bytes() == JPEG
    assert p == (library / "images" / "Alternating_Renegade_Row" / "1.jpg").resolve()


@pytest.mark.parametrize(
    "lib_id,frame",
    [
        ("Alternating_Renegade_Row", 2),  # out of range (frames=2)
        ("Alternating_Renegade_Row", -1),
        ("Unknown_Id", 0),
        ("../images/Glute_Ham_Raise", 0),
        ("", 0),
    ],
)
def test_image_file_rejects(library, lib_id, frame):
    assert el.image_file(lib_id, frame) is None


def test_image_file_missing_on_disk(library):
    (library / "images" / "Glute_Ham_Raise" / "1.jpg").unlink()
    assert el.image_file("Glute_Ham_Raise", 1) is None


def test_image_file_symlink_file_escaping_root(library, tmp_path):
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(JPEG)
    target = library / "images" / "Glute_Ham_Raise" / "0.jpg"
    target.unlink()
    target.symlink_to(outside)
    assert el.image_file("Glute_Ham_Raise", 0) is None


def test_image_file_symlink_dir_escaping_root(library, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "0.jpg").write_bytes(JPEG)
    d = library / "images" / "Kettlebell_Deadlift"
    for f in d.iterdir():
        f.unlink()
    d.rmdir()
    d.symlink_to(outside, target_is_directory=True)
    assert el.image_file("Kettlebell_Deadlift", 0) is None


def test_image_file_no_library(lib_root):
    assert el.image_file("Glute_Ham_Raise", 0) is None


# ---------------------------------------------------------------------------
# _clean_entry
# ---------------------------------------------------------------------------


def _raw(**over):
    raw = {
        "id": "Some_Lift",
        "name": "  Some Lift  ",
        "instructions": ["  Step one.  ", "", 3, "Step two."],
        "primaryMuscles": ["quadriceps", 7],
        "secondaryMuscles": ["glutes"],
        "equipment": "barbell",
        "category": "strength",
        "images": ["Some_Lift/0.jpg", "Some_Lift/1.jpg"],
    }
    raw.update(over)
    return raw


def test_clean_entry_happy():
    e = el._clean_entry(_raw())
    assert e == {
        "id": "Some_Lift",
        "name": "Some Lift",
        "instructions": ["Step one.", "Step two."],
        "primary_muscles": ["quadriceps"],
        "secondary_muscles": ["glutes"],
        "equipment": "barbell",
        "category": "strength",
        "frames": 2,
    }
    # The cleaned shape must pass the runtime loader's check.
    assert el._valid_loaded(e)


@pytest.mark.parametrize(
    "over",
    [
        {"id": "../etc"},
        {"id": "has space"},
        {"id": "a/b"},
        {"id": ""},
        {"id": None},
        {"id": "x" * 121},
        {"name": None},
        {"name": "   "},
        {"name": 5},
    ],
)
def test_clean_entry_rejects(over):
    assert el._clean_entry(_raw(**over)) is None


def test_clean_entry_rejects_non_dict():
    assert el._clean_entry(["Some_Lift"]) is None
    assert el._clean_entry(None) is None


@pytest.mark.parametrize(
    "images,frames",
    [
        (["Some_Lift/0.jpg", "../x/1.jpg"], 1),
        (["Other/0.jpg", "Some_Lift/1.jpg"], 0),
        (["Some_Lift/1.jpg"], 0),  # wrong index
        (["Some_Lift/0.png"], 0),
        ([f"Some_Lift/{i}.jpg" for i in range(10)], el._MAX_FRAMES),
        ([], 0),
        (None, 0),
    ],
)
def test_clean_entry_frames(images, frames):
    assert el._clean_entry(_raw(images=images))["frames"] == frames


def test_clean_entry_caps_steps_and_strings():
    e = el._clean_entry(_raw(instructions=["y" * 5000] * 40, primaryMuscles=["m" * 100] * 20, equipment="e" * 100))
    assert len(e["instructions"]) == el._MAX_STEPS
    assert all(len(s) == el._MAX_STEP_CHARS for s in e["instructions"])
    assert len(e["primary_muscles"]) == 8
    assert all(len(m) == 40 for m in e["primary_muscles"])
    assert len(e["equipment"]) == 40


# ---------------------------------------------------------------------------
# _get_capped / _fetch (respx)
# ---------------------------------------------------------------------------

_JSON_URL = f"{el._RAW_BASE}/dist/exercises.json"
_IMG_PREFIX = f"{el._RAW_BASE}/exercises/"


def _upstream(n_entries, frames=2):
    return [
        {
            "id": f"Lift_{i}",
            "name": f"Lift {i}",
            "instructions": [f"Do lift {i}."],
            "primaryMuscles": ["quadriceps"],
            "equipment": "barbell",
            "images": [f"Lift_{i}/{n}.jpg" for n in range(frames)],
        }
        for i in range(n_entries)
    ]


def _mock_upstream(respx_mock, upstream, image_body=lambda rel: httpx.Response(200, content=JPEG)):
    respx_mock.get(_JSON_URL).mock(return_value=httpx.Response(200, content=json.dumps(upstream).encode()))
    respx_mock.get(url__startswith=_IMG_PREFIX).mock(
        side_effect=lambda request: image_body(str(request.url)[len(_IMG_PREFIX) :])
    )


def _snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def old_library(lib_root):
    """An installed library at an older pin (so fetch doesn't short-circuit)."""
    _write_library(lib_root, meta_sha="0ld0ld0")
    return _snapshot(lib_root)


def _assert_untouched(root: Path, snap: dict):
    assert _snapshot(root) == snap
    assert not root.with_name(root.name + ".new").exists()
    assert not root.with_name(root.name + ".old").exists()


async def test_get_capped_ok_and_oversized():
    import respx

    with respx.mock:
        respx.get("https://example.test/a").mock(return_value=httpx.Response(200, content=b"x" * 10))
        async with httpx.AsyncClient() as client:
            assert await el._get_capped(client, "https://example.test/a", 10) == b"x" * 10
            with pytest.raises(ValueError, match="larger than 9 bytes"):
                await el._get_capped(client, "https://example.test/a", 9)


async def test_get_capped_raises_on_http_error():
    import respx

    with respx.mock:
        respx.get("https://example.test/a").mock(return_value=httpx.Response(404))
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await el._get_capped(client, "https://example.test/a", 100)


async def test_fetch_success_installs_library_and_meta(old_library, lib_root, capsys):
    import respx

    with respx.mock(assert_all_called=True) as m:
        _mock_upstream(m, _upstream(3))
        rc = await el._fetch()
    assert rc == 0
    lib = json.loads((lib_root / "library.json").read_text())
    assert [e["id"] for e in lib] == ["Lift_0", "Lift_1", "Lift_2"]
    assert all(e["frames"] == 2 for e in lib)
    meta = json.loads((lib_root / "meta.json").read_text())
    assert meta["source_sha"] == el.SOURCE_SHA and meta["count"] == 3
    assert (lib_root / "images" / "Lift_2" / "1.jpg").read_bytes() == JPEG
    # Old library fully replaced; staging/.old cleaned up.
    assert not (lib_root / "images" / "Glute_Ham_Raise").exists()
    assert not lib_root.with_name(lib_root.name + ".new").exists()
    assert not lib_root.with_name(lib_root.name + ".old").exists()
    # And the runtime loader picks it up.
    assert el.get_info("lift 1")["found"] is True
    assert el.image_file("Lift_1", 1) is not None


async def test_fetch_into_fresh_dir(lib_root):
    import respx

    with respx.mock as m:
        _mock_upstream(m, _upstream(1))
        assert await el._fetch() == 0
    assert (lib_root / "library.json").is_file()


async def test_fetch_network_error_leaves_library_intact(old_library, lib_root):
    import respx

    with respx.mock as m:
        m.get(_JSON_URL).mock(side_effect=httpx.ConnectError("boom"))
        assert await el._fetch() == 1
    _assert_untouched(lib_root, old_library)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, content=b"{not json"),
        httpx.Response(200, content=json.dumps({"id": "x"}).encode()),  # not a list
        httpx.Response(200, content=json.dumps([{"id": "bad id"}, 5]).encode()),  # no usable entries
    ],
)
async def test_fetch_bad_index_leaves_library_intact(old_library, lib_root, response):
    import respx

    with respx.mock as m:
        m.get(_JSON_URL).mock(return_value=response)
        assert await el._fetch() == 1
    _assert_untouched(lib_root, old_library)


async def test_fetch_too_many_photo_failures_leaves_library_intact(old_library, lib_root):
    import respx

    # 4 photos, 1 failure = 25% > 5%.
    def body(rel):
        return httpx.Response(503) if rel == "Lift_1/0.jpg" else httpx.Response(200, content=JPEG)

    with respx.mock as m:
        _mock_upstream(m, _upstream(2), body)
        assert await el._fetch() == 1
    _assert_untouched(lib_root, old_library)


async def test_fetch_non_jpeg_counts_as_failure(old_library, lib_root):
    import respx

    def body(rel):
        if rel == "Lift_0/1.jpg":
            return httpx.Response(200, content=b"<html>rate limited</html>")
        return httpx.Response(200, content=JPEG)

    with respx.mock as m:
        _mock_upstream(m, _upstream(2), body)
        assert await el._fetch() == 1
    _assert_untouched(lib_root, old_library)


async def test_fetch_oversized_photo_counts_as_failure(old_library, lib_root, monkeypatch):
    import respx

    monkeypatch.setattr(el, "_MAX_IMAGE_BYTES", len(JPEG) + 5)

    def body(rel):
        content = JPEG + b"\x00" * 100 if rel == "Lift_0/0.jpg" else JPEG
        return httpx.Response(200, content=content)

    with respx.mock as m:
        _mock_upstream(m, _upstream(2), body)
        assert await el._fetch() == 1
    _assert_untouched(lib_root, old_library)


async def test_fetch_partial_under_threshold_installs_and_returns_1(old_library, lib_root):
    import respx

    # 11 entries × 2 = 22 photos; 1 failure = 4.5% ≤ 5% → installs.
    # Frame 0 of Lift_3 failed → Lift_3 keeps 0 frames (0..k-1 contiguous),
    # even though frame 1 landed.
    def body(rel):
        return httpx.Response(404) if rel == "Lift_3/0.jpg" else httpx.Response(200, content=JPEG)

    with respx.mock as m:
        _mock_upstream(m, _upstream(11), body)
        assert await el._fetch() == 1
    lib = {e["id"]: e for e in json.loads((lib_root / "library.json").read_text())}
    assert len(lib) == 11
    assert lib["Lift_3"]["frames"] == 0
    assert lib["Lift_4"]["frames"] == 2
    assert el.image_file("Lift_3", 1) is None
    assert el.get_info("Lift 3")["frames"] == []


async def test_fetch_already_up_to_date_short_circuits(lib_root, capsys):
    import respx

    _write_library(lib_root, meta_sha=el.SOURCE_SHA)
    snap = _snapshot(lib_root)
    with respx.mock as m:  # no routes: any request would raise
        assert await el._fetch() == 0
        assert m.calls.call_count == 0
    assert "Already up to date" in capsys.readouterr().out
    assert _snapshot(lib_root) == snap


async def test_fetch_force_ignores_up_to_date(lib_root):
    import respx

    _write_library(lib_root, meta_sha=el.SOURCE_SHA)
    with respx.mock as m:
        _mock_upstream(m, _upstream(1))
        assert await el._fetch(force=True) == 0
    assert [e["id"] for e in json.loads((lib_root / "library.json").read_text())] == ["Lift_0"]


async def test_fetch_corrupt_meta_does_not_short_circuit(lib_root):
    import respx

    _write_library(lib_root)
    (lib_root / "meta.json").write_text("{nope", encoding="utf-8")
    with respx.mock as m:
        _mock_upstream(m, _upstream(1))
        assert await el._fetch() == 0


async def test_fetch_install_failure_restores_previous(old_library, lib_root, monkeypatch):
    import respx

    real_replace = os.replace

    def flaky_replace(src, dst):
        if str(src).endswith(".new"):
            raise OSError("disk says no")
        return real_replace(src, dst)

    monkeypatch.setattr(el.os, "replace", flaky_replace)
    with respx.mock as m:
        _mock_upstream(m, _upstream(1))
        assert await el._fetch() == 1
    assert _snapshot(lib_root) == old_library
    assert not lib_root.with_name(lib_root.name + ".old").exists()


# ---------------------------------------------------------------------------
# main(check)
# ---------------------------------------------------------------------------


def test_main_check_without_library(lib_root, capsys):
    assert el.main(["check"]) == 1
    assert "run `fetch` first" in capsys.readouterr().out


def test_main_check_with_library(library, capsys):
    from orchestrator.exercises_seed import EXERCISES

    assert el.main(["check"]) == 0
    out = capsys.readouterr().out
    assert f"/{len(EXERCISES)} catalog exercises have photos." in out
    assert "video link only: Kettlebell Deadlift" in out


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.routes_workout import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_route_exercise_info_found(client, library):
    r = client.get("/api/workouts/exercise-info", params={"name": "Dumbbell Renegade Row"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["found"] is True
    assert body["match"] == "Alternating Renegade Row"
    assert len(body["frames"]) == 2
    # The frame URL the payload advertises must actually serve.
    img = client.get(body["frames"][0])
    assert img.status_code == 200
    assert img.content == JPEG


def test_route_exercise_info_not_found(client, library):
    r = client.get("/api/workouts/exercise-info", params={"name": "Kettlebell Deadlift"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["found"] is False
    assert "success" not in body
    assert body["video_url"]


def test_route_exercise_info_library_missing(client, lib_root):
    body = client.get("/api/workouts/exercise-info", params={"name": "Plank"}).json()
    assert body["ok"] is True
    assert body["library_available"] is False


def test_route_exercise_info_whitespace_name_is_400(client, library):
    r = client.get("/api/workouts/exercise-info", params={"name": "   "})
    assert r.status_code == 400
    assert r.json() == {"ok": False, "error": "name is required"}


def test_route_exercise_info_missing_or_empty_name_is_422(client, library):
    assert client.get("/api/workouts/exercise-info").status_code == 422
    assert client.get("/api/workouts/exercise-info", params={"name": ""}).status_code == 422


def test_route_photo_ok_headers(client, library):
    r = client.get("/api/workouts/exercise-library/Glute_Ham_Raise/1.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"].startswith("private")
    assert "max-age=" in r.headers["cache-control"]
    assert r.content == JPEG


@pytest.mark.parametrize(
    "path",
    [
        "/api/workouts/exercise-library/Glute_Ham_Raise/2.jpg",
        "/api/workouts/exercise-library/Glute_Ham_Raise/-1.jpg",
        "/api/workouts/exercise-library/Nope/0.jpg",
    ],
)
def test_route_photo_404(client, library, path):
    r = client.get(path)
    assert r.status_code == 404
    assert r.json() == {"ok": False, "error": "not found"}


def test_route_photo_traversal_is_404(client, library, tmp_path):
    # An encoded slash doesn't match the route (framework 404); either way no file.
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "0.jpg").write_bytes(JPEG)
    r = client.get("/api/workouts/exercise-library/..%2F..%2Fsecret/0.jpg")
    assert r.status_code == 404
    assert r.content != JPEG


def test_route_photo_404_without_library(client, lib_root):
    r = client.get("/api/workouts/exercise-library/Glute_Ham_Raise/0.jpg")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# workout_status tool, action="how_to"
# ---------------------------------------------------------------------------


@pytest.fixture
def workouts_on(monkeypatch):
    from orchestrator import shared

    monkeypatch.setattr(shared, "WORKOUTS_ENABLED", True)


async def _how_to(exercise):
    from orchestrator import tool_handlers

    return await tool_handlers._reg_workout_status({"action": "how_to", "exercise": exercise})


async def test_tool_how_to_found_caps_steps(lib_root, workouts_on):
    long_entry = {**_ENTRIES[1], "instructions": [f"{i}" + "s" * 900 for i in range(12)]}
    _write_library(lib_root, [long_entry])
    out = json.loads(await _how_to("Glute Ham Raise"))
    assert out["exercise"] == "Glute Ham Raise"
    assert out["reference"] == "Glute Ham Raise"
    assert len(out["steps"]) == 8
    assert all(len(s) == 500 for s in out["steps"])
    assert out["steps"][0].startswith("0")
    assert out["muscles"] == ["hamstrings"]
    assert len(json.dumps(out)) < 8000


async def test_tool_how_to_not_found(library, workouts_on):
    out = json.loads(await _how_to("Kettlebell Deadlift"))
    assert out["steps"] is None
    assert out["video_url"].startswith("https://www.youtube.com/")
    assert "don't improvise" in out["note"]
    assert "isn't downloaded" not in out["note"]


async def test_tool_how_to_library_missing(lib_root, workouts_on):
    out = json.loads(await _how_to("Plank"))
    assert out["steps"] is None
    assert "isn't downloaded yet" in out["note"]
    assert "don't improvise" in out["note"]


@pytest.mark.parametrize("exercise", ["", "   ", None])
async def test_tool_how_to_requires_exercise(library, workouts_on, exercise):
    assert await _how_to(exercise) == "exercise is required for how_to."


async def test_tool_how_to_respects_workouts_disabled(library, monkeypatch):
    from orchestrator import shared

    monkeypatch.setattr(shared, "WORKOUTS_ENABLED", False)
    out = await _how_to("Glute Ham Raise")
    assert out == "Workout tracking isn't enabled on this install."


async def test_tool_how_to_via_registry(library, workouts_on):
    from orchestrator.tool_registry import execute_tool

    out = json.loads(await execute_tool("workout_status", {"action": "how_to", "exercise": "Dumbbell Renegade Row"}))
    assert out["reference"] == "Alternating Renegade Row"


# ---------------------------------------------------------------------------
# scripts/backup_state.py exclusion
# ---------------------------------------------------------------------------

_BACKUP_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "backup_state.py"


@pytest.fixture
def backup_state():
    spec = importlib.util.spec_from_file_location("backup_state_excl_under_test", _BACKUP_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "rel",
    [
        "app/exercise_library/library.json",
        "app/exercise_library/images/Plank/0.jpg",
        "app/exercise_library.new/library.json",
        "app/exercise_library.old/images/x/0.jpg",
        "exercise_library",
    ],
)
def test_backup_excludes_exercise_library(backup_state, rel):
    assert backup_state._is_excluded(Path(rel)) is True


@pytest.mark.parametrize(
    "rel",
    ["app/brain_state.db", "app/meal_photos/a.jpg", "app/exercise_library_notes.txt", "app/my_exercise_library/x"],
)
def test_backup_keeps_neighbours(backup_state, rel):
    assert backup_state._is_excluded(Path(rel)) is False
