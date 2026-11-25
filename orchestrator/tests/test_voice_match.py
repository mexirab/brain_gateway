"""Voice match (orchestrator/voice_match.py): voiceprint file, speech span,
scoring, enrollment, the model download, recording and the CLI. The speaker
model itself is stubbed (embed, or a stand-in onnxruntime module with the
pinned checksum swapped for the stub's), except in the fbank tests (need
kaldi-native-fbank, a requirement) and the optional real-model tests
(VOICE_MATCH_TEST_MODEL=<path to the pinned .onnx>). No network: the download
is a respx mock and the satellite a stand-in aioesphomeapi.APIClient.
The bridge side is in test_voice_satellite.py ("Voice match")."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
import sys
import threading
import types
import wave
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
import respx

from orchestrator import voice_match as vm
from orchestrator.config import settings

RATE = vm.SAMPLE_RATE


def _unit(seed: int) -> np.ndarray:
    v = np.random.default_rng(seed).standard_normal(vm.EMBED_DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def _prints_file(tmp_path, prints: dict, **override) -> str:
    path = str(tmp_path / "voiceprints.json")
    payload = {
        "version": vm.VOICEPRINTS_VERSION,
        "model_sha256": vm.MODEL_SHA256,
        "voiceprints": {k: [float(x) for x in v] for k, v in prints.items()},
        **override,
    }
    with open(path, "w") as f:
        json.dump(payload, f)
    return path


def _tone(level: float, seconds: float) -> np.ndarray:
    n = int(RATE * seconds)
    amp = level * 32768
    return np.where(np.arange(n) % 2 == 0, amp, -amp).astype(np.float32)


class StubEmbed:
    """Deterministic stand-in for the model: one base voice + a little per-segment noise."""

    def __init__(self, base: np.ndarray | None = None, noise: float = 0.02) -> None:
        self.base = base if base is not None else _unit(1)
        self.noise = noise
        self.calls = 0

    def __call__(self, samples: np.ndarray) -> np.ndarray:
        self.calls += 1
        rng = np.random.default_rng(self.calls)
        v = self.base + self.noise * rng.standard_normal(vm.EMBED_DIM).astype(np.float32)
        return v / np.linalg.norm(v)


# -- speech span --------------------------------------------------------------------


def _pcm(ms: int) -> bytes:
    return bytes(ms * RATE * 2 // 1000)


def test_speech_span_trims_to_the_speech_with_margins():
    # 3000 ms capture: speech from 500 ms, 1200 ms of quiet at the end
    span = vm.speech_span(_pcm(3000), speech_at_ms=500, quiet_ms=1200, audio_ms=3000)
    # 300..2100 ms: LEAD_MS before speech, TAIL_MS into the quiet
    assert len(span) == (2100 - 300) * 32


def test_speech_span_short_speech_scores_the_whole_capture():
    pcm = _pcm(2000)
    assert vm.speech_span(pcm, speech_at_ms=1000, quiet_ms=1200, audio_ms=2000) == pcm


def test_speech_span_without_a_speech_start_is_the_whole_capture():
    pcm = _pcm(2000)
    assert vm.speech_span(pcm, speech_at_ms=None, quiet_ms=0, audio_ms=2000) == pcm


def test_speech_span_at_the_cap_keeps_to_the_end():
    span = vm.speech_span(_pcm(15000), speech_at_ms=100, quiet_ms=0, audio_ms=15000)
    assert len(span) == 15000 * 32


# -- voiceprint file ------------------------------------------------------------------


def test_voiceprints_roundtrip_mode_600_and_unit_vectors(tmp_path):
    path = str(tmp_path / "vm" / "voiceprints.json")
    vm.save_voiceprints(path, {"owner": _unit(1) * 3, "guest": _unit(2)})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    prints = vm.load_voiceprints(path)
    assert sorted(prints) == ["guest", "owner"]
    assert np.isclose(np.linalg.norm(prints["owner"]), 1.0)
    assert np.allclose(prints["owner"], _unit(1), atol=1e-5)
    assert not [n for n in os.listdir(os.path.dirname(path)) if n.endswith(".tmp")]


def test_missing_voiceprints_file(tmp_path):
    with pytest.raises(vm.VoiceMatchUnavailable) as e:
        vm.load_voiceprints(str(tmp_path / "nope.json"))
    assert e.value.reason == "no_voiceprints"


def test_voiceprints_from_another_model_are_stale(tmp_path):
    path = _prints_file(tmp_path, {"owner": _unit(1)}, model_sha256="0" * 64)
    with pytest.raises(vm.VoiceMatchUnavailable) as e:
        vm.load_voiceprints(path)
    assert e.value.reason == "voiceprints_stale"


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"version": 2}, id="version"),
        pytest.param({"voiceprints": []}, id="not-a-dict"),
        pytest.param({"voiceprints": {"owner": [0.1] * 10}}, id="wrong-length"),
        pytest.param({"voiceprints": {"owner": [float("nan")] * vm.EMBED_DIM}}, id="nan"),
        pytest.param({"voiceprints": {"owner": [0.0] * vm.EMBED_DIM}}, id="zero"),
        pytest.param({"voiceprints": {"owner": ["x"] * vm.EMBED_DIM}}, id="strings"),
        pytest.param({"voiceprints": {"Bad Name": [0.1] * vm.EMBED_DIM}}, id="bad-id"),
        pytest.param({"voiceprints": {f"p{i}": [0.1] * vm.EMBED_DIM for i in range(9)}}, id="too-many"),
    ],
)
def test_invalid_voiceprints_are_refused(tmp_path, override):
    path = _prints_file(tmp_path, {"owner": _unit(1)}, **override)
    with pytest.raises(vm.VoiceMatchUnavailable) as e:
        vm.load_voiceprints(path)
    assert e.value.reason == "voiceprints_invalid"


def test_voiceprints_that_are_not_json(tmp_path):
    path = tmp_path / "voiceprints.json"
    path.write_text("{not json")
    with pytest.raises(vm.VoiceMatchUnavailable) as e:
        vm.load_voiceprints(str(path))
    assert e.value.reason == "voiceprints_invalid"


def test_oversized_voiceprints_file_is_not_read(tmp_path):
    path = tmp_path / "voiceprints.json"
    path.write_bytes(b" " * (vm.VOICEPRINTS_MAX_BYTES + 1))
    with pytest.raises(vm.VoiceMatchUnavailable) as e:
        vm.load_voiceprints(str(path))
    assert e.value.reason == "voiceprints_invalid"


# -- matcher -----------------------------------------------------------------------------


def test_score_is_the_best_match_over_voiceprints(tmp_path, monkeypatch):
    owner, guest = _unit(1), _unit(2)
    m = vm.VoiceMatcher("/nonexistent.onnx", _prints_file(tmp_path, {"owner": owner, "guest": guest}))
    monkeypatch.setattr(m, "embed", lambda samples: guest)
    assert m.score(_pcm(1500)) == pytest.approx(1.0, abs=1e-5)
    monkeypatch.setattr(m, "embed", lambda samples: -owner)
    assert m.score(_pcm(1500)) == pytest.approx(float(np.max(np.stack([owner, guest]) @ -owner)), abs=1e-5)


def test_score_without_voiceprints_never_runs_the_model(tmp_path, monkeypatch):
    m = vm.VoiceMatcher("/nonexistent.onnx", str(tmp_path / "missing.json"))
    called = []
    monkeypatch.setattr(m, "embed", lambda samples: called.append(1))
    with pytest.raises(vm.VoiceMatchUnavailable) as e:
        m.score(_pcm(1500))
    assert e.value.reason == "no_voiceprints" and not called


def test_voiceprints_reload_when_the_file_changes(tmp_path, monkeypatch):
    path = _prints_file(tmp_path, {"owner": _unit(1)})
    m = vm.VoiceMatcher("/nonexistent.onnx", path)
    monkeypatch.setattr(m, "embed", lambda samples: _unit(2))
    first = m.score(_pcm(1500))
    vm.save_voiceprints(path, {"owner": _unit(2)})
    os.utime(path, ns=(os.stat(path).st_atime_ns, os.stat(path).st_mtime_ns + 10**9))
    assert first < 0.5 and m.score(_pcm(1500)) == pytest.approx(1.0, abs=1e-5)


def test_score_caps_the_audio_length(tmp_path, monkeypatch):
    m = vm.VoiceMatcher("/nonexistent.onnx", _prints_file(tmp_path, {"owner": _unit(1)}))
    seen = []
    monkeypatch.setattr(m, "embed", lambda samples: seen.append(len(samples)) or _unit(1))
    m.score(_pcm(30_000))
    assert seen == [vm.MAX_SCORE_S * RATE]


def test_missing_model(tmp_path):
    m = vm.VoiceMatcher(str(tmp_path / "model.onnx"), str(tmp_path / "p.json"))
    with pytest.raises(vm.VoiceMatchUnavailable) as e:
        m._load_session()
    assert e.value.reason == "model_missing"


def test_model_with_the_wrong_checksum_is_refused(tmp_path):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"not the pinned model")
    m = vm.VoiceMatcher(str(model), str(tmp_path / "p.json"))
    with pytest.raises(vm.VoiceMatchUnavailable) as e:
        m._load_session()
    assert e.value.reason == "model_checksum"


def test_preload_reports_the_first_missing_piece(tmp_path):
    m = vm.VoiceMatcher(str(tmp_path / "model.onnx"), str(tmp_path / "p.json"))
    assert m.preload() == "model_missing"


def test_features_missing_when_kaldi_native_fbank_is_absent(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "kaldi_native_fbank":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(vm.VoiceMatchUnavailable) as e:
        vm.fbank(_tone(0.1, 1.0))
    assert e.value.reason == "features_missing"


def test_fbank_shape_and_mean_normalised():
    pytest.importorskip("kaldi_native_fbank")
    feats = vm.fbank(_tone(0.1, 1.0))
    assert feats.shape[1] == 80 and 95 <= feats.shape[0] <= 100  # 10 ms frames
    assert np.allclose(feats.mean(axis=0), 0.0, atol=1e-3)


def test_fbank_too_short_audio():
    pytest.importorskip("kaldi_native_fbank")
    with pytest.raises(vm.VoiceMatchUnavailable) as e:
        vm.fbank(_tone(0.1, 0.1))
    assert e.value.reason == "too_short"


@pytest.mark.skipif(not os.path.isfile(os.environ.get("VOICE_MATCH_TEST_MODEL", "")), reason="no model file")
def test_real_model_embedding_is_a_unit_vector():
    pytest.importorskip("kaldi_native_fbank")
    m = vm.VoiceMatcher(os.environ["VOICE_MATCH_TEST_MODEL"], "/unused.json")
    emb = m.embed(_tone(0.1, 2.0))
    assert emb.shape == (vm.EMBED_DIM,) and np.isclose(np.linalg.norm(emb), 1.0, atol=1e-4)


# -- enrollment -------------------------------------------------------------------------


def test_speech_segments_skip_silence():
    clip = np.concatenate([_tone(0.0, 6.0), _tone(0.1, 6.0)])
    segs = vm.speech_segments(clip)
    assert segs and all(len(s) == int(vm.ENROLL_SEGMENT_S * RATE) for s in segs)
    assert len(segs) < len(vm.speech_segments(_tone(0.1, 12.0)))


def test_build_voiceprint_reports_leave_one_out_quality(monkeypatch):
    m = vm.VoiceMatcher("/nonexistent.onnx", "/unused.json")
    monkeypatch.setattr(m, "embed", StubEmbed())
    voiceprint, quality = vm.build_voiceprint(m, [_tone(0.1, 9.0)] * 3)
    assert np.isclose(np.linalg.norm(voiceprint), 1.0)
    assert len(quality) == 3 and all(q > 0.8 for q in quality)


def test_build_voiceprint_needs_two_clips_and_enough_speech(monkeypatch):
    m = vm.VoiceMatcher("/nonexistent.onnx", "/unused.json")
    monkeypatch.setattr(m, "embed", StubEmbed())
    with pytest.raises(ValueError, match="at least 2"):
        vm.build_voiceprint(m, [_tone(0.1, 20.0)])
    with pytest.raises(ValueError, match="not enough speech"):
        vm.build_voiceprint(m, [_tone(0.1, 4.0), _tone(0.1, 4.0)])
    with pytest.raises(ValueError, match="not enough speech"):
        vm.build_voiceprint(m, [_tone(0.1, 20.0), _tone(0.0, 20.0)])  # one silent clip


def test_read_wav_refuses_other_formats(tmp_path):
    path = str(tmp_path / "stereo.wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(RATE * 4))
    with pytest.raises(ValueError, match="16 kHz mono"):
        vm.read_wav(path)


# -- CLI ----------------------------------------------------------------------------------


@pytest.fixture
def cli(tmp_path, monkeypatch):
    prints = str(tmp_path / "voice_match" / "voiceprints.json")
    monkeypatch.setattr(settings, "voice_match_voiceprints_path", prints)
    monkeypatch.setattr(settings, "voice_match_model_path", str(tmp_path / "model.onnx"))
    monkeypatch.setattr(settings, "voice_match_threshold", 0.35)
    stub = StubEmbed()
    monkeypatch.setattr(vm.VoiceMatcher, "embed", lambda self, samples: stub(samples))
    return prints


def _clip(prints: str, name: str, samples: np.ndarray) -> str:
    return _wav_at(vm.clips_dir(prints), name + ".wav", samples)


def _wav_at(directory: str, name: str, samples: np.ndarray) -> str:
    path = os.path.join(directory, name)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.astype("<i2").tobytes())
    return path


def test_clips_dir_sits_next_to_the_voiceprints_mode_700(tmp_path):
    prints = str(tmp_path / "voice_match" / "voiceprints.json")
    d = vm.clips_dir(prints)
    assert d == str(tmp_path / "voice_match" / "clips")
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


@pytest.mark.parametrize("name", ["../me", "/tmp/me", "me/../../x", "Me 1", ".hidden", ""])
def test_clip_names_cannot_leave_the_clips_dir(tmp_path, name):
    with pytest.raises(ValueError, match="bare name"):
        vm._clip_path(name, str(tmp_path / "voice_match" / "voiceprints.json"))


def test_cli_enroll_list_score_remove_and_clips_are_deleted(cli, capsys):
    clips = [_clip(cli, f"me{i}", _tone(0.1, 9.0)) for i in range(3)]
    _clip(cli, "check", _tone(0.1, 9.0))
    assert vm.main(["enroll", "owner", "me0", "me1", "me2.wav"]) == 0
    out = capsys.readouterr().out
    assert "enrolled; 1 voiceprint(s)" in out and "clips deleted" in out
    assert not any(os.path.exists(c) for c in clips)
    assert stat.S_IMODE(os.stat(cli).st_mode) == 0o600

    assert vm.main(["list"]) == 0
    assert capsys.readouterr().out.strip() == "owner"

    assert vm.main(["score", "check"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.endswith(" recognized") and "not recognized" not in out

    assert vm.main(["remove", "owner"]) == 0
    assert vm.load_voiceprints(cli) == {}
    assert vm.main(["remove", "owner"]) == 1


def test_cli_enroll_keep_clips(cli, capsys):
    clips = [_clip(cli, f"me{i}", _tone(0.1, 9.0)) for i in range(2)]
    assert vm.main(["enroll", "owner", "me0", "me1", "--keep-clips"]) == 0
    assert all(os.path.exists(c) for c in clips)
    assert "delete them when done" in capsys.readouterr().out


def test_cli_enroll_refuses_a_bad_id_and_thin_clips(cli, capsys):
    _clip(cli, "me", _tone(0.1, 9.0))
    assert vm.main(["enroll", "Not An Id", "me", "me"]) == 2
    assert vm.main(["enroll", "owner", "me"]) == 1
    assert "at least 2" in capsys.readouterr().err
    assert not os.path.exists(cli)
    assert os.path.exists(os.path.join(vm.clips_dir(cli), "me.wav"))  # kept: nothing was enrolled


def test_cli_enroll_replaces_a_stale_file(cli):
    os.makedirs(os.path.dirname(cli), exist_ok=True)
    with open(cli, "w") as f:
        json.dump({"version": 1, "model_sha256": "0" * 64, "voiceprints": {}}, f)
    for i in range(2):
        _clip(cli, f"me{i}", _tone(0.1, 9.0))
    assert vm.main(["enroll", "owner", "me0", "me1"]) == 0
    assert list(vm.load_voiceprints(cli)) == ["owner"]


def test_cli_enroll_never_overwrites_an_unreadable_file(cli, capsys):
    """Enrolling one person must not delete the others' voiceprints."""
    os.makedirs(os.path.dirname(cli), exist_ok=True)
    with open(cli, "w") as f:
        f.write("{hand-edited")
    for i in range(2):
        _clip(cli, f"me{i}", _tone(0.1, 9.0))
    assert vm.main(["enroll", "owner", "me0", "me1"]) == 1
    assert "voiceprints_invalid" in capsys.readouterr().err
    with open(cli) as f:
        assert f.read() == "{hand-edited"


def test_cli_reports_a_missing_model_without_a_traceback(tmp_path, monkeypatch, capsys):
    prints = str(tmp_path / "voice_match" / "voiceprints.json")
    vm.save_voiceprints(prints, {"owner": _unit(1)})
    monkeypatch.setattr(settings, "voice_match_voiceprints_path", prints)
    monkeypatch.setattr(settings, "voice_match_model_path", str(tmp_path / "missing.onnx"))
    _clip(prints, "check", _tone(0.1, 3.0))
    assert vm.main(["score", "check"]) == 1
    err = capsys.readouterr().err
    assert "voice match unavailable: model_missing" in err and "Traceback" not in err


def test_cli_reports_a_bad_wav_without_a_traceback(cli, capsys):
    vm.save_voiceprints(cli, {"owner": _unit(1)})
    with open(os.path.join(vm.clips_dir(cli), "junk.wav"), "wb") as f:
        f.write(b"not a wav")
    assert vm.main(["score", "junk"]) == 1
    assert capsys.readouterr().err.startswith("error:")


def test_cli_record_needs_a_configured_device_and_a_new_clip_name(cli, monkeypatch, capsys):
    from orchestrator.config import SatelliteDevice

    monkeypatch.setattr(
        type(settings),
        "voice_satellites",
        lambda self: [SatelliteDevice("atom-echo-kitchen", "10.0.0.50", "media_player.k")],
    )
    assert vm.main(["record", "atom-echo-nope", "me1"]) == 2
    _clip(cli, "me1", _tone(0.1, 3.0))
    assert vm.main(["record", "atom-echo-kitchen", "me1"]) == 2  # exists
    assert vm.main(["record", "atom-echo-kitchen", "../me2"]) == 1  # not a bare name
    calls = []

    async def fake_record(host, port, key, seconds, expected_name):
        calls.append((host, expected_name, seconds))
        return bytes(4 * RATE * 2)

    monkeypatch.setattr(vm, "record_clip", fake_record)
    assert vm.main(["record", "atom-echo-kitchen", "me2", "--seconds", "999"]) == 0
    assert calls == [("10.0.0.50", "atom-echo-kitchen", 40.0)]
    path = os.path.join(vm.clips_dir(cli), "me2.wav")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_cli_refuses_to_record_or_enroll_outside_a_voice_match_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "voice_match_voiceprints_path", str(tmp_path / "data" / "prints.json"))
    assert vm.main(["record", "atom-echo-kitchen", "me1"]) == 2
    assert vm.main(["enroll", "owner", "me1", "me2"]) == 2
    assert "directory named voice_match" in capsys.readouterr().err
    assert not os.path.exists(tmp_path / "data")


# -- hardening -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "values",
    [
        pytest.param([[0.1]] * vm.EMBED_DIM, id="nested"),
        pytest.param(["0.1"] * vm.EMBED_DIM, id="numeric-strings"),
        pytest.param([True] * vm.EMBED_DIM, id="bools"),
    ],
)
def test_voiceprint_values_must_be_plain_numbers(tmp_path, values):
    path = str(tmp_path / "voiceprints.json")
    with open(path, "w") as f:
        json.dump({"version": 1, "model_sha256": vm.MODEL_SHA256, "voiceprints": {"owner": values}}, f)
    with pytest.raises(vm.VoiceMatchUnavailable) as e:
        vm.load_voiceprints(path)
    assert e.value.reason == "voiceprints_invalid"


def test_deeply_nested_voiceprint_file_is_invalid_not_a_crash(tmp_path):
    path = tmp_path / "voiceprints.json"
    path.write_text("[" * 50_000 + "]" * 50_000)
    with pytest.raises(vm.VoiceMatchUnavailable) as e:
        vm.load_voiceprints(str(path))
    assert e.value.reason == "voiceprints_invalid"


def test_nan_model_output_is_its_own_reason(tmp_path, monkeypatch):
    m = vm.VoiceMatcher("/nonexistent.onnx", "/unused.json")

    class Session:
        def run(self, names, feeds):
            return [np.full((1, vm.EMBED_DIM), np.nan, dtype=np.float32)]

    monkeypatch.setattr(vm, "fbank", lambda samples: np.zeros((50, 80), dtype=np.float32))
    monkeypatch.setattr(m, "_load_session", lambda: Session())
    with pytest.raises(vm.VoiceMatchUnavailable) as e:
        m.embed(_tone(0.1, 1.0))
    assert e.value.reason == "model_output"


async def test_score_async_runs_on_its_own_pool_and_refuses_when_busy(tmp_path, monkeypatch):
    import threading

    m = vm.VoiceMatcher("/nonexistent.onnx", _prints_file(tmp_path, {"owner": _unit(1)}))
    release = threading.Event()
    names = []

    def slow_score(pcm):
        names.append(threading.current_thread().name)
        release.wait(5)
        return 0.5

    monkeypatch.setattr(m, "score", slow_score)
    first = [asyncio.create_task(m.score_async(b"", timeout=0.05)) for _ in range(vm.MAX_IN_FLIGHT)]
    for t in first:
        with pytest.raises(TimeoutError):
            await t
    # both workers are still stuck past their timeout: fail fast, don't queue
    with pytest.raises(vm.VoiceMatchUnavailable) as e:
        await m.score_async(b"", timeout=1)
    assert e.value.reason == "busy"
    release.set()
    for _ in range(100):
        if m._in_flight == 0:
            break
        await asyncio.sleep(0.01)
    assert m._in_flight == 0
    assert await m.score_async(b"", timeout=1) == 0.5
    assert all(n.startswith("voice-match") for n in names)


async def test_score_async_never_blocks_the_event_loop_on_a_model_load(tmp_path):
    """A worker holding the model lock (first load: 26 MB read + hash + session)
    must not hold up the event loop: the busy check takes its own lock."""
    import time

    m = vm.VoiceMatcher("/nonexistent.onnx", _prints_file(tmp_path, {"owner": _unit(1)}))
    m._lock.acquire()  # a model load in progress on another thread
    lags: list[float] = []

    async def ticker() -> None:
        last = time.monotonic()
        for _ in range(30):
            await asyncio.sleep(0.01)
            now = time.monotonic()
            lags.append(now - last - 0.01)
            last = now

    try:
        tick = asyncio.create_task(ticker())
        for _ in range(3):  # third call: both slots held by the stuck ones -> busy at once
            with pytest.raises((TimeoutError, vm.VoiceMatchUnavailable)):
                await m.score_async(_pcm(1500), timeout=0.05)
        await tick
        assert max(lags) < 0.05
    finally:
        m._lock.release()


async def test_a_call_cancelled_while_queued_gives_its_slot_back(tmp_path, monkeypatch):
    import threading

    m = vm.VoiceMatcher("/nonexistent.onnx", _prints_file(tmp_path, {"owner": _unit(1)}))
    gate = threading.Event()
    monkeypatch.setattr(m, "score", lambda pcm: gate.wait(5) and 0.5)
    m._executor._max_workers = 1  # one worker: the second call waits in the queue
    running = asyncio.create_task(m.score_async(b"", timeout=5))
    await asyncio.sleep(0.05)
    with pytest.raises(TimeoutError):
        await m.score_async(b"", timeout=0.05)  # cancelled while still queued
    gate.set()
    assert await running == 0.5
    for _ in range(100):
        if m._in_flight == 0:
            break
        await asyncio.sleep(0.01)
    assert m._in_flight == 0


def test_reasons_are_fixed_strings():
    """Reasons go into logs and must stay free of user content."""
    assert all(r.replace("_", "").isalpha() for r in vm.REASONS)


# -- small pieces ------------------------------------------------------------------------------


def test_pcm_to_samples_keeps_the_int16_scale_and_drops_a_half_sample():
    """WeSpeaker expects int16-scale samples: normalising to -1..1 would wreck every score."""
    pcm = (32767).to_bytes(2, "little", signed=True) + (-32768).to_bytes(2, "little", signed=True) + b"\x01"
    samples = vm.pcm_to_samples(pcm)
    assert samples.dtype == np.float32 and samples.tolist() == [32767.0, -32768.0]


@pytest.mark.parametrize(
    ("speech_at", "quiet", "start_ms", "end_ms"),
    [
        pytest.param(500, 100, 300, 3000, id="quiet-shorter-than-the-tail"),
        pytest.param(50, 1200, 0, 2100, id="speech-before-the-lead"),
    ],
)
def test_speech_span_margins_stay_inside_the_capture(speech_at, quiet, start_ms, end_ms):
    pcm = (bytes(range(256)) * 400)[: 3000 * 32]
    span = vm.speech_span(pcm, speech_at_ms=speech_at, quiet_ms=quiet, audio_ms=3000)
    assert span == pcm[start_ms * 32 : end_ms * 32]


def test_voiceprints_that_are_not_utf8_are_invalid(tmp_path):
    path = tmp_path / "voiceprints.json"
    path.write_bytes(b'{"version": 1, "x": "\xff\xfe"}')
    with pytest.raises(vm.VoiceMatchUnavailable) as e:
        vm.load_voiceprints(str(path))
    assert e.value.reason == "voiceprints_invalid"


def test_voiceprint_with_a_huge_integer_is_invalid_not_a_crash(tmp_path):
    """A hand-edited file with an integer past float range must read as invalid,
    like 1e400 (which json makes inf) already does. Today the bridge labels it
    `failed (OverflowError)` instead of `unavailable`, and `list` / `remove` /
    `enroll` end in a traceback."""
    path = tmp_path / "voiceprints.json"
    payload = {"version": 1, "model_sha256": vm.MODEL_SHA256, "voiceprints": {"owner": [10**400] * vm.EMBED_DIM}}
    path.write_text(json.dumps(payload))
    with pytest.raises(vm.VoiceMatchUnavailable) as e:
        vm.load_voiceprints(str(path))
    assert e.value.reason == "voiceprints_invalid"


def test_read_wav_refuses_a_long_clip_naming_only_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(vm, "MAX_WAV_SECONDS", 1)
    path = _wav_at(str(tmp_path), "long.wav", _tone(0.1, 2.0))
    with pytest.raises(ValueError, match="longer than 1 s") as e:
        vm.read_wav(path)
    assert str(e.value).startswith("long.wav:") and str(tmp_path) not in str(e.value)


def test_write_wav_never_overwrites_a_clip(tmp_path):
    path = str(tmp_path / "me1.wav")
    vm._write_wav(path, bytes(RATE * 2))
    with pytest.raises(FileExistsError):
        vm._write_wav(path, bytes(RATE * 2))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert vm.read_wav(path).shape == (RATE,)


# -- voiceprint cache --------------------------------------------------------------------------


def test_an_empty_voiceprint_file_is_no_voiceprints(tmp_path, monkeypatch):
    """What `remove` leaves after the last person: fail open, never np.stack([])."""
    path = str(tmp_path / "voice_match" / "voiceprints.json")
    vm.save_voiceprints(path, {})
    m = vm.VoiceMatcher("/nonexistent.onnx", path)
    called = []
    monkeypatch.setattr(m, "embed", lambda samples: called.append(1))
    with pytest.raises(vm.VoiceMatchUnavailable) as e:
        m.score(_pcm(1500))
    assert e.value.reason == "no_voiceprints" and not called


def test_a_voiceprint_path_that_cannot_be_stat_is_invalid(tmp_path):
    blocker = tmp_path / "voice_match"
    blocker.write_text("a file where the directory should be")
    m = vm.VoiceMatcher("/nonexistent.onnx", str(blocker / "voiceprints.json"))
    with pytest.raises(vm.VoiceMatchUnavailable) as e:
        m.score(_pcm(1500))
    assert e.value.reason == "voiceprints_invalid"


def test_voiceprints_are_read_once_until_the_file_changes(tmp_path, monkeypatch):
    path = _prints_file(tmp_path, {"owner": _unit(1)})
    m = vm.VoiceMatcher("/nonexistent.onnx", path)
    monkeypatch.setattr(m, "embed", lambda samples: _unit(1))
    reads = []
    real_load = vm.load_voiceprints
    monkeypatch.setattr(vm, "load_voiceprints", lambda p: reads.append(p) or real_load(p))
    for _ in range(5):
        m.score(_pcm(1500))
    assert len(reads) == 1
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    m.score(_pcm(1500))
    assert len(reads) == 2


def test_voiceprint_reload_is_safe_while_two_threads_score(tmp_path, monkeypatch):
    """Two rooms score on the pool's two threads while `enroll` rewrites the
    file: no error, and every score comes from one whole file."""
    path = str(tmp_path / "voice_match" / "voiceprints.json")
    first, second = {"owner": _unit(1)}, {"owner": _unit(2), "guest": _unit(3)}
    vm.save_voiceprints(path, first)
    m = vm.VoiceMatcher("/nonexistent.onnx", path)
    probe = _unit(2)
    monkeypatch.setattr(m, "embed", lambda samples: probe)
    expected = (float(_unit(1) @ probe), 1.0)  # score against `first`, against `second`
    errors: list[BaseException] = []
    scores: list[float] = []
    writing = threading.Event()

    def reader() -> None:
        while writing.is_set():
            try:
                scores.append(m.score(_pcm(200)))
            except BaseException as e:  # noqa: BLE001 — collected, asserted below
                errors.append(e)

    writing.set()
    threads = [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    for i in range(60):
        vm.save_voiceprints(path, second if i % 2 == 0 else first)
    writing.clear()
    for t in threads:
        t.join(10)
    assert not errors and scores
    assert all(min(abs(s - e) for e in expected) < 1e-4 for s in scores)
    vm.save_voiceprints(path, second)
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert m.score(_pcm(200)) == pytest.approx(1.0, abs=1e-5)


# -- model session (onnxruntime stubbed; the pinned checksum swapped for the stub's) ------------


MODEL_BLOB = b"stand-in for the pinned speaker model " * 64


class FakeOrt:
    """A stand-in onnxruntime module recording how the session is built and run."""

    def __init__(self) -> None:
        self.built: list[dict] = []
        self.runs: list = []
        self.output = _unit(1)
        self.fail: Exception | None = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.model_path = ""
        fake = self

        class SessionOptions:
            def __init__(self) -> None:
                self.intra_op_num_threads = 0
                self.inter_op_num_threads = 0
                self.entries: dict[str, str] = {}

            def add_session_config_entry(self, key: str, value: str) -> None:
                self.entries[key] = value

        class InferenceSession:
            def __init__(self, model, sess_options=None, providers=None) -> None:
                fake.entered.set()
                fake.release.wait(5)
                if fake.fail is not None:
                    raise fake.fail
                fake.built.append({"model": model, "opts": sess_options, "providers": providers})

            def run(self, names, feeds):
                fake.runs.append((names, {k: v.shape for k, v in feeds.items()}))
                return [fake.output[None]]

        self.module = types.ModuleType("onnxruntime")
        self.module.SessionOptions = SessionOptions
        self.module.InferenceSession = InferenceSession


@pytest.fixture
def fake_ort(tmp_path, monkeypatch):
    ort = FakeOrt()
    monkeypatch.setitem(sys.modules, "onnxruntime", ort.module)
    monkeypatch.setattr(vm, "MODEL_SHA256", hashlib.sha256(MODEL_BLOB).hexdigest())
    model = tmp_path / "model.onnx"
    model.write_bytes(MODEL_BLOB)
    ort.model_path = str(model)
    return ort


def test_session_is_built_from_the_hashed_bytes_with_quiet_cpu_threads(fake_ort, tmp_path):
    m = vm.VoiceMatcher(fake_ort.model_path, str(tmp_path / "p.json"), threads=3)
    session = m._load_session()
    (built,) = fake_ort.built
    assert built["model"] == MODEL_BLOB  # the bytes that were hashed, not a path re-opened later
    assert built["providers"] == ["CPUExecutionProvider"]
    opts = built["opts"]
    assert (opts.intra_op_num_threads, opts.inter_op_num_threads) == (3, 1)
    assert opts.entries == {"session.intra_op.allow_spinning": "0"}
    os.unlink(fake_ort.model_path)  # loaded once: a later delete doesn't matter to this process
    assert m._load_session() is session and len(fake_ort.built) == 1


def test_threads_below_one_mean_one(fake_ort, tmp_path):
    vm.VoiceMatcher(fake_ort.model_path, str(tmp_path / "p.json"), threads=0)._load_session()
    assert fake_ort.built[0]["opts"].intra_op_num_threads == 1


def test_session_is_built_once_across_threads(fake_ort, tmp_path):
    m = vm.VoiceMatcher(fake_ort.model_path, str(tmp_path / "p.json"))
    fake_ort.release.clear()
    sessions: list = []
    errors: list[BaseException] = []

    def load() -> None:
        try:
            sessions.append(m._load_session())
        except BaseException as e:  # noqa: BLE001 — collected, asserted below
            errors.append(e)

    first = threading.Thread(target=load)
    first.start()
    assert fake_ort.entered.wait(5)  # the first load is inside InferenceSession(), holding the lock
    rest = [threading.Thread(target=load) for _ in range(3)]
    for t in rest:
        t.start()
    fake_ort.release.set()
    for t in [first, *rest]:
        t.join(10)
    assert not errors and len(sessions) == 4
    assert len(fake_ort.built) == 1 and all(s is sessions[0] for s in sessions)


def test_a_failed_session_build_is_model_load_and_is_retried(fake_ort, tmp_path):
    """Not cached: once the operator fixes the model, the next question loads it."""
    m = vm.VoiceMatcher(fake_ort.model_path, str(tmp_path / "p.json"))
    fake_ort.fail = RuntimeError("onnx says no")
    with pytest.raises(vm.VoiceMatchUnavailable) as e:
        m._load_session()
    assert e.value.reason == "model_load"
    fake_ort.fail = None
    assert m._load_session() is not None and len(fake_ort.built) == 1


def test_a_model_past_the_size_cap_is_refused(fake_ort, tmp_path, monkeypatch):
    monkeypatch.setattr(vm, "MODEL_MAX_BYTES", len(MODEL_BLOB) - 1)
    m = vm.VoiceMatcher(fake_ort.model_path, str(tmp_path / "p.json"))
    with pytest.raises(vm.VoiceMatchUnavailable) as e:
        m._load_session()
    assert e.value.reason == "model_checksum" and not fake_ort.built


def test_preload_is_ready_with_a_model_and_voiceprints(fake_ort, tmp_path):
    prints = str(tmp_path / "voice_match" / "voiceprints.json")
    m = vm.VoiceMatcher(fake_ort.model_path, prints)
    assert m.preload() == "no_voiceprints"
    vm.save_voiceprints(prints, {"owner": _unit(1)})
    assert m.preload() is None


async def test_score_async_runs_features_and_model_end_to_end(fake_ort, tmp_path):
    """Real fbank into the (stub) session: the feed/output names and the
    feature shape the pinned model needs, then the best cosine."""
    pytest.importorskip("kaldi_native_fbank")
    prints = str(tmp_path / "voice_match" / "voiceprints.json")
    vm.save_voiceprints(prints, {"owner": _unit(2), "guest": _unit(1)})
    m = vm.VoiceMatcher(fake_ort.model_path, prints)
    pcm = _tone(0.1, 1.5).astype("<i2").tobytes()
    assert await m.score_async(pcm, timeout=10) == pytest.approx(1.0, abs=1e-5)
    ((names, shapes),) = fake_ort.runs
    assert names == ["embs"] and list(shapes) == ["feats"]
    batch, frames, bins = shapes["feats"]
    assert (batch, bins) == (1, 80) and 140 <= frames <= 150


@pytest.mark.skipif(not os.path.isfile(os.environ.get("VOICE_MATCH_TEST_MODEL", "")), reason="no model file")
async def test_real_model_scores_its_own_enrollment_audio_as_the_same_voice(tmp_path):
    """The pinned model through the matcher's own pool: the real session options
    load, and audio scores ~1 against a voiceprint made from it."""
    pytest.importorskip("kaldi_native_fbank")
    prints = str(tmp_path / "voice_match" / "voiceprints.json")
    m = vm.VoiceMatcher(os.environ["VOICE_MATCH_TEST_MODEL"], prints)
    noise = np.round(np.random.default_rng(7).standard_normal(int(RATE * 2.0)) * 3000).astype(np.float32)
    vm.save_voiceprints(prints, {"owner": m.embed(noise)})
    assert await m.preload_async(timeout=30) is None
    assert await m.score_async(noise.astype("<i2").tobytes(), timeout=30) == pytest.approx(1.0, abs=1e-3)


# -- fetch_model (no network: respx) -----------------------------------------------------------


@pytest.fixture
def pinned_blob(monkeypatch):
    """Pretend MODEL_BLOB is the pinned model."""
    monkeypatch.setattr(vm, "MODEL_SHA256", hashlib.sha256(MODEL_BLOB).hexdigest())
    return MODEL_BLOB


def _leftovers(directory) -> list[str]:
    return sorted(n for n in os.listdir(directory) if n.endswith(".tmp"))


def test_fetch_model_downloads_verifies_and_installs(tmp_path, pinned_blob):
    path = tmp_path / "voice_match" / "model.onnx"
    with respx.mock() as router:
        route = router.get(vm.MODEL_URL).mock(return_value=httpx.Response(200, content=pinned_blob))
        msg = vm.fetch_model(str(path))
    assert route.call_count == 1
    assert msg == f"downloaded {len(pinned_blob)} bytes, checksum ok"
    assert path.read_bytes() == pinned_blob
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert _leftovers(path.parent) == []


def test_fetch_model_keeps_a_good_copy_without_a_request(tmp_path, pinned_blob):
    path = tmp_path / "model.onnx"
    path.write_bytes(pinned_blob)
    with respx.mock():  # no routes: any request would fail the test
        assert vm.fetch_model(str(path)) == "already there, checksum ok"


def test_fetch_model_replaces_a_bad_copy(tmp_path, pinned_blob):
    path = tmp_path / "model.onnx"
    path.write_bytes(b"truncated download")
    with respx.mock() as router:
        router.get(vm.MODEL_URL).mock(return_value=httpx.Response(200, content=pinned_blob))
        vm.fetch_model(str(path))
    assert path.read_bytes() == pinned_blob


def _fetch_fails(tmp_path, response, exc, match=None) -> None:
    path = tmp_path / "model.onnx"
    path.write_bytes(b"old copy")  # whatever was there stays as it was
    with respx.mock() as router:
        router.get(vm.MODEL_URL).mock(return_value=response)
        with pytest.raises(exc, match=match):
            vm.fetch_model(str(path))
    assert path.read_bytes() == b"old copy"
    assert _leftovers(tmp_path) == []


def test_fetch_model_checksum_mismatch_installs_nothing(tmp_path, pinned_blob):
    _fetch_fails(tmp_path, httpx.Response(200, content=b"tampered weights"), ValueError, "checksum mismatch")


def test_fetch_model_stops_past_the_size_cap(tmp_path, pinned_blob, monkeypatch):
    monkeypatch.setattr(vm, "MODEL_MAX_BYTES", 100)
    _fetch_fails(tmp_path, httpx.Response(200, content=bytes(1000)), ValueError, "larger than expected")


def test_fetch_model_stops_past_the_deadline(tmp_path, pinned_blob, monkeypatch):
    ticks = iter([0.0])  # the deadline is taken at 0; every later reading is past it
    monkeypatch.setattr(vm, "time", SimpleNamespace(monotonic=lambda: next(ticks, vm.FETCH_DEADLINE_S + 1.0)))
    _fetch_fails(tmp_path, httpx.Response(200, content=pinned_blob), TimeoutError, "longer than")


def test_fetch_model_http_error_leaves_no_temp_file(tmp_path, pinned_blob):
    _fetch_fails(tmp_path, httpx.Response(404), httpx.HTTPStatusError)


def test_cli_fetch_prints_the_outcome(cli, tmp_path, pinned_blob, capsys):
    with respx.mock() as router:
        router.get(vm.MODEL_URL).mock(return_value=httpx.Response(200, content=pinned_blob))
        assert vm.main(["fetch"]) == 0
    assert "checksum ok" in capsys.readouterr().out
    assert (tmp_path / "model.onnx").read_bytes() == pinned_blob


def test_cli_fetch_network_failure_is_reported_without_a_traceback(cli, capsys):
    with respx.mock() as router:
        router.get(vm.MODEL_URL).mock(side_effect=httpx.ConnectError("no route"))
        assert vm.main(["fetch"]) == 1
    assert capsys.readouterr().err.startswith("error:")


# -- record_clip (aioesphomeapi.APIClient stubbed) ---------------------------------------------


class FakeDevice:
    """Stands in for aioesphomeapi.APIClient: records what record_clip sends."""

    def __init__(self, address, port, password, **kwargs) -> None:
        self.args = (address, port, password)
        self.kwargs = kwargs
        self.login = None
        self.handlers: dict | None = None
        self.events: list[str] = []
        self.unsubscribed = 0
        self.disconnected = 0

    async def connect(self, login=False):
        self.login = login

    def subscribe_voice_assistant(self, *, handle_start, handle_stop, handle_audio):
        self.handlers = {"start": handle_start, "stop": handle_stop, "audio": handle_audio}
        return self._unsub

    def _unsub(self) -> None:
        self.unsubscribed += 1

    def send_voice_assistant_event(self, event_type, data) -> None:
        self.events.append(event_type.name)

    async def disconnect(self):
        self.disconnected += 1


class _FastAsyncio:
    """vm.asyncio stand-in: no real 0.5 s settle sleep; waits capped at `wait_cap`."""

    def __init__(self, wait_cap: float | None = None) -> None:
        self.sleeps: list[float] = []
        self.waits: list[float] = []
        self._cap = wait_cap

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, delay, *args):
        self.sleeps.append(delay)
        await asyncio.sleep(0)

    async def wait_for(self, aw, timeout):
        self.waits.append(timeout)
        return await asyncio.wait_for(aw, timeout if self._cap is None else min(timeout, self._cap))


@pytest.fixture
def device(monkeypatch):
    import aioesphomeapi

    made: list[FakeDevice] = []

    def factory(*args, **kwargs):
        made.append(FakeDevice(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(aioesphomeapi, "APIClient", factory)
    monkeypatch.setattr(vm, "asyncio", _FastAsyncio())
    return made


async def _subscribed(made: list) -> FakeDevice:
    for _ in range(100):
        if made and made[0].handlers is not None:
            return made[0]
        await asyncio.sleep(0)
    raise AssertionError("record_clip never subscribed")


RUN_START, STT_START, RUN_END = "VOICE_ASSISTANT_RUN_START", "VOICE_ASSISTANT_STT_START", "VOICE_ASSISTANT_RUN_END"


async def test_record_clip_records_then_ends_the_run_with_run_end_alone(device, capsys):
    task = asyncio.create_task(vm.record_clip("10.0.0.50", 6053, "noise-key", 1.0, "atom-echo-kitchen"))
    dev = await _subscribed(device)
    await dev.handlers["audio"](b"\x09\x00" * 512)  # before the device starts a run: not recorded
    assert await dev.handlers["start"]("conv", 0, None, "Hey Jess") == 0  # 0 = audio over the API
    await dev.handlers["start"]("conv", 0, None, "Hey Jess")  # a repeat start changes nothing
    chunk = b"\x02\x00" * 512
    for _ in range(40):  # 40 KiB; 1 s is 32000 bytes
        await dev.handlers["audio"](chunk)
    pcm = await task

    assert pcm == b"\x02\x00" * RATE
    assert dev.events == [RUN_START, STT_START, RUN_END]  # never STT_VAD_END (wedges the device)
    assert dev.args == ("10.0.0.50", 6053, None)
    assert dev.kwargs == {
        "client_info": "jess-voice-match",
        "noise_psk": "noise-key",
        "expected_name": "atom-echo-kitchen",
    }
    assert dev.login is True
    assert (dev.unsubscribed, dev.disconnected) == (1, 1)
    assert capsys.readouterr().out.count("recording...") == 1


async def test_record_clip_device_stop_returns_what_was_heard(device):
    task = asyncio.create_task(vm.record_clip("10.0.0.50", 6053, "noise-key", 10.0, "atom-echo-kitchen"))
    dev = await _subscribed(device)
    await dev.handlers["stop"](False)  # no run yet: ignored, still waiting
    await asyncio.sleep(0)
    assert not task.done()
    await dev.handlers["start"]("conv", 0, None, None)  # the button
    for _ in range(3):
        await dev.handlers["audio"](b"\x03\x00" * 512)
    await dev.handlers["stop"](False)
    assert await task == b"\x03\x00" * 1536
    assert dev.events[:2] == [RUN_START, STT_START] and "VOICE_ASSISTANT_STT_VAD_END" not in dev.events
    assert (dev.unsubscribed, dev.disconnected) == (1, 1)


async def test_record_clip_times_out_and_still_lets_go_of_the_device(device, monkeypatch):
    """Nobody pressed the button (or the bridge still holds the device)."""
    shim = _FastAsyncio(wait_cap=0.01)
    monkeypatch.setattr(vm, "asyncio", shim)
    with pytest.raises(TimeoutError):
        await vm.record_clip("10.0.0.50", 6053, "noise-key", 5.0, "atom-echo-kitchen")
    (dev,) = device
    assert shim.waits == [15, 305.0]
    assert (dev.unsubscribed, dev.disconnected) == (1, 1) and dev.events == []


# -- CLI gaps ----------------------------------------------------------------------------------


def test_cli_enroll_keeps_the_other_voiceprints_and_replaces_its_own(cli, monkeypatch):
    for i in range(4):
        _clip(cli, f"c{i}", _tone(0.1, 9.0))
    assert vm.main(["enroll", "owner", "c0", "c1"]) == 0
    owner = vm.load_voiceprints(cli)["owner"]
    other_voice = StubEmbed(base=_unit(5))
    monkeypatch.setattr(vm.VoiceMatcher, "embed", lambda self, samples: other_voice(samples))
    assert vm.main(["enroll", "guest", "c2", "c3"]) == 0
    prints = vm.load_voiceprints(cli)
    assert sorted(prints) == ["guest", "owner"] and np.allclose(prints["owner"], owner, atol=1e-5)
    for i in range(2):
        _clip(cli, f"r{i}", _tone(0.1, 9.0))
    assert vm.main(["enroll", "owner", "r0", "r1"]) == 0  # re-enroll: replaced, still two
    prints = vm.load_voiceprints(cli)
    assert sorted(prints) == ["guest", "owner"] and not np.allclose(prints["owner"], owner, atol=1e-2)


def test_cli_enroll_at_the_voiceprint_limit(cli, capsys):
    vm.save_voiceprints(cli, {f"p{i}": _unit(i + 10) for i in range(vm.MAX_VOICEPRINTS)})
    for i in range(4):
        _clip(cli, f"c{i}", _tone(0.1, 9.0))
    assert vm.main(["enroll", "newcomer", "c0", "c1"]) == 1
    assert f"already {vm.MAX_VOICEPRINTS} voiceprints" in capsys.readouterr().err
    assert vm.main(["enroll", "p3", "c2", "c3"]) == 0  # replacing one is fine
    assert len(vm.load_voiceprints(cli)) == vm.MAX_VOICEPRINTS


def test_cli_list_without_a_file(cli, capsys):
    assert vm.main(["list"]) == 0
    assert capsys.readouterr().out.strip() == "(no voiceprints)"


@pytest.mark.parametrize("cmd", [["list"], ["remove", "owner"]])
def test_cli_refuses_to_touch_a_stale_file_except_enroll(cli, capsys, cmd):
    """Only `enroll` may start over on prints from another model."""
    os.makedirs(os.path.dirname(cli), exist_ok=True)
    stale = json.dumps({"version": 1, "model_sha256": "0" * 64, "voiceprints": {"owner": [0.1] * vm.EMBED_DIM}})
    with open(cli, "w") as f:
        f.write(stale)
    assert vm.main(cmd) == 1
    assert "voiceprints_stale" in capsys.readouterr().err
    with open(cli) as f:
        assert f.read() == stale


def test_cli_score_reports_an_unknown_voice(cli, monkeypatch, capsys):
    vm.save_voiceprints(cli, {"owner": _unit(1)})
    monkeypatch.setattr(vm.VoiceMatcher, "embed", lambda self, samples: _unit(2))
    _clip(cli, "tv", _tone(0.1, 3.0))
    assert vm.main(["score", "tv"]) == 0
    assert capsys.readouterr().out.strip().endswith("not recognized")


def _one_device(monkeypatch) -> None:
    from orchestrator.config import SatelliteDevice

    monkeypatch.setattr(
        type(settings),
        "voice_satellites",
        lambda self: [SatelliteDevice("atom-echo-kitchen", "10.0.0.50", "media_player.k")],
    )


def test_cli_record_saves_nothing_under_three_seconds(cli, monkeypatch, capsys):
    _one_device(monkeypatch)

    async def short(*args):
        return bytes(2 * RATE * 2)

    monkeypatch.setattr(vm, "record_clip", short)
    assert vm.main(["record", "atom-echo-kitchen", "me1"]) == 1
    assert "nothing saved" in capsys.readouterr().err
    assert not os.path.exists(os.path.join(vm.clips_dir(cli), "me1.wav"))


def test_cli_record_timeout_hints_at_the_bridge(cli, monkeypatch, capsys):
    _one_device(monkeypatch)

    async def nobody(*args):
        raise TimeoutError

    monkeypatch.setattr(vm, "record_clip", nobody)
    assert vm.main(["record", "atom-echo-kitchen", "me1"]) == 1
    assert "is the bridge still holding that device" in capsys.readouterr().err


def test_cli_logs_carry_no_voiceprint_id_or_clip_name(cli, caplog):
    """The id and clip names are the operator's own labels (often a name):
    printed to the operator's terminal, never to a log record (Loki)."""
    caplog.set_level(logging.DEBUG)
    for i in range(2):
        _clip(cli, f"zqxclip{i}", _tone(0.1, 9.0))
    _clip(cli, "zqxcheck", _tone(0.1, 3.0))
    assert vm.main(["enroll", "zqx-person", "zqxclip0", "zqxclip1"]) == 0
    assert vm.main(["score", "zqxcheck"]) == 0
    assert vm.main(["list"]) == 0
    assert vm.main(["remove", "zqx-person"]) == 0
    assert "zqx" not in caplog.text
