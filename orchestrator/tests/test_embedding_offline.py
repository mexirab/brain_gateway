"""Offline embedding-model load, the remote-code pin, and the memory-off path.

Covers orchestrator/embedding_pin.py (load_kwargs + _KeepCodeRevision,
remote_code_revisions, the fetch/check CLI), shared._load_embedding_model and
_CountingEmbedder(None), the auto_learn early drop, the typed
EmbeddingModelUnavailableError surfaces and the image's HF_HUB_OFFLINE=1.

The real model is never loaded and nothing touches the network: the CLI's
load and child process are faked; the sentence-transformers wiring tests run
the installed library against a local stub directory with transformers'
Auto*.from_pretrained replaced by recorders.
"""

import contextlib
import json
import logging
import os
import shlex
import subprocess
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from prometheus_client import REGISTRY

from orchestrator import auto_learn, embedding_pin, prompt_builder, shared, tool_registry
from orchestrator.embedding_pin import (
    DEFAULT_EMBEDDING_MODEL,
    EMBEDDING_CODE_REVISION,
    EMBEDDING_MODEL_REVISION,
    OFFLINE_ENV,
    _KeepCodeRevision,
    load_kwargs,
    remote_code_revisions,
)
from orchestrator.exceptions import (
    BrainGatewayError,
    EmbeddingModelUnavailableError,
    ExternalServiceError,
    TransientError,
)
from orchestrator.metrics import EMBEDDING_ERRORS
from orchestrator.shared import _CountingEmbedder

PIN = EMBEDDING_CODE_REVISION
OTHER_COMMIT = "0123456789abcdef0123456789abcdef01234567"
CUSTOM_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
REPO_ROOT = Path(__file__).resolve().parents[2]
# How transformers names the default model's remote code (repo parts sanitized,
# commit as-is): transformers_modules.<org>.<repo>.<commit>.<file>
NOMIC_CODE_PKG = "transformers_modules.nomic_hyphen_ai.nomic_hyphen_bert_hyphen_2048"

_CONV = [
    {"role": "user", "content": "I just moved to a new apartment and I really love cooking on weekends."},
    {"role": "assistant", "content": "That sounds lovely, congrats on the new place!"},
]


def _val(name, **labels):
    return REGISTRY.get_sample_value(name, labels or None) or 0.0


def _embedding_errors_all_callers():
    return sum(s.value for m in EMBEDDING_ERRORS.collect() for s in m.samples if s.name.endswith("_total"))


class _FakeModel:
    max_seq_length = 512

    def __init__(self, result=None):
        self.result = [0.0] * 4 if result is None else result
        self.encode_calls = []

    def encode(self, *args, **kwargs):
        self.encode_calls.append((args, kwargs))
        return self.result


# ---------------------------------------------------------------------------
# embedding_pin.load_kwargs + _KeepCodeRevision
# ---------------------------------------------------------------------------


def test_load_kwargs_default_model_pins_config_model_and_processor():
    kwargs = load_kwargs(DEFAULT_EMBEDDING_MODEL)

    assert kwargs == {
        "trust_remote_code": True,
        "revision": EMBEDDING_MODEL_REVISION,
        "config_kwargs": {"code_revision": PIN},
        "model_kwargs": {"code_revision": PIN},
        "processor_kwargs": {"code_revision": PIN},
    }
    assert type(kwargs["model_kwargs"]) is _KeepCodeRevision
    # shared.py adds this itself; a duplicate would be a TypeError at load.
    assert "local_files_only" not in kwargs


@pytest.mark.parametrize("name", [CUSTOM_MODEL, "nomic-ai/nomic-embed-text-v1.5", ""])
def test_load_kwargs_other_models_are_unpinned(name):
    assert load_kwargs(name) == {"trust_remote_code": True}


def test_load_kwargs_returns_fresh_dicts_each_call():
    first = load_kwargs(DEFAULT_EMBEDDING_MODEL)
    first["model_kwargs"]["device_map"] = "cpu"
    first["config_kwargs"].clear()
    first["processor_kwargs"]["model_max_length"] = 8

    assert load_kwargs(DEFAULT_EMBEDDING_MODEL) == {
        "trust_remote_code": True,
        "revision": EMBEDDING_MODEL_REVISION,
        "config_kwargs": {"code_revision": PIN},
        "model_kwargs": {"code_revision": PIN},
        "processor_kwargs": {"code_revision": PIN},
    }


def test_keep_code_revision_pop_returns_value_without_removing_it():
    kw = _KeepCodeRevision(code_revision=PIN)

    assert kw.pop("code_revision") == PIN
    assert kw.pop("code_revision", None) == PIN  # ST 5.7's exact call
    assert kw == {"code_revision": PIN}


def test_keep_code_revision_other_keys_pop_normally():
    kw = _KeepCodeRevision(code_revision=PIN, device_map="cpu")

    assert kw.pop("device_map") == "cpu"
    assert "device_map" not in kw
    assert kw.pop("missing", "fallback") == "fallback"
    with pytest.raises(KeyError):
        kw.pop("missing")
    assert kw == {"code_revision": PIN}


def test_keep_code_revision_absent_key_pops_like_a_dict():
    kw = _KeepCodeRevision(device_map="cpu")

    assert kw.pop("code_revision", None) is None
    with pytest.raises(KeyError):
        kw.pop("code_revision")


def test_keep_code_revision_survives_pop_then_merge_and_unpack():
    """What ST does to model_kwargs: pop code_revision, then merge/unpack the same dict."""
    kw = load_kwargs(DEFAULT_EMBEDDING_MODEL)["model_kwargs"]
    popped = kw.pop("code_revision", None) if kw else None
    merged = {"revision": EMBEDDING_MODEL_REVISION}
    merged.update(kw)

    def from_pretrained(path, **kwargs):
        return kwargs

    assert popped == PIN
    assert merged["code_revision"] == PIN
    assert {**kw}["code_revision"] == PIN
    assert from_pretrained("x", **kw)["code_revision"] == PIN


# ---------------------------------------------------------------------------
# The installed sentence-transformers really hands the pin to every load
# ---------------------------------------------------------------------------
# A local stub directory in ST format (modules.json naming the Transformer
# module, as the default model's repo does); transformers' AutoConfig /
# AutoModel / AutoProcessor .from_pretrained record their kwargs. The stub
# processor load aborts the construction once all three were seen.


class _StopLoad(Exception):
    """Raised by the stub processor load: the three loads were recorded by then."""


@pytest.fixture
def st_model_dir(tmp_path):
    modules = [{"idx": 0, "name": "0", "path": "", "type": "sentence_transformers.models.Transformer"}]
    (tmp_path / "modules.json").write_text(json.dumps(modules))
    return tmp_path


@contextlib.contextmanager
def _recorded_transformers_loads():
    import transformers

    calls = {}

    def recorder(name, result=None, stop=False):
        def from_pretrained(*args, **kwargs):
            calls[name] = kwargs
            if stop:
                raise _StopLoad
            return result

        return from_pretrained

    with (
        patch.object(
            transformers.AutoConfig, "from_pretrained", side_effect=recorder("config", transformers.BertConfig())
        ),
        patch.object(transformers.AutoModel, "from_pretrained", side_effect=recorder("model", MagicMock(name="model"))),
        patch.object(transformers.AutoProcessor, "from_pretrained", side_effect=recorder("processor", stop=True)),
    ):
        yield calls


def _load_via_pin_kwargs(real_st, model_dir, monkeypatch):
    with contextlib.suppress(_StopLoad):
        real_st(str(model_dir), local_files_only=True, **load_kwargs(DEFAULT_EMBEDDING_MODEL))


def _load_via_shared(real_st, model_dir, monkeypatch):
    monkeypatch.setattr(shared, "SentenceTransformer", lambda _id, **kw: real_st(str(model_dir), **kw))
    # The stub processor aborts the load, so the loader reports memory off.
    assert shared._load_embedding_model(DEFAULT_EMBEDDING_MODEL) is None


def _load_via_cli(real_st, model_dir, monkeypatch):
    import sentence_transformers

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", lambda _id, **kw: real_st(str(model_dir), **kw))
    with contextlib.suppress(_StopLoad):
        embedding_pin._load_and_encode(DEFAULT_EMBEDDING_MODEL)


@pytest.mark.parametrize(
    "load",
    [
        pytest.param(_load_via_pin_kwargs, id="load_kwargs"),
        pytest.param(_load_via_shared, id="shared_loader"),
        pytest.param(_load_via_cli, id="cli_load_and_encode"),
    ],
)
def test_pinned_code_revision_reaches_every_transformers_load(load, st_model_dir, monkeypatch):
    from sentence_transformers import SentenceTransformer as real_st

    with _recorded_transformers_loads() as calls:
        load(real_st, st_model_dir, monkeypatch)

    assert sorted(calls) == ["config", "model", "processor"], f"loads seen: {sorted(calls)}"
    assert {name: kw.get("code_revision") for name, kw in calls.items()} == {
        "config": PIN,
        "model": PIN,
        "processor": PIN,
    }
    assert {name: kw.get("revision") for name, kw in calls.items()} == dict.fromkeys(calls, EMBEDDING_MODEL_REVISION)
    assert all(kw.get("trust_remote_code") is True for kw in calls.values())


def test_installed_st_drops_code_revision_from_a_plain_dict(st_model_dir):
    """Sensitivity check for the test above: a plain model_kwargs dict (the pre-fix
    load_kwargs) loses the pin before AutoModel.from_pretrained."""
    from sentence_transformers import SentenceTransformer as real_st

    kwargs = load_kwargs(DEFAULT_EMBEDDING_MODEL)
    kwargs["model_kwargs"] = dict(kwargs["model_kwargs"])
    with _recorded_transformers_loads() as calls, contextlib.suppress(_StopLoad):
        real_st(str(st_model_dir), local_files_only=True, **kwargs)

    if calls["model"].get("code_revision") == PIN:
        pytest.skip("this sentence-transformers keeps model_kwargs['code_revision']; _KeepCodeRevision is now spare")
    assert calls["model"].get("code_revision") is None


# ---------------------------------------------------------------------------
# embedding_pin.remote_code_revisions
# ---------------------------------------------------------------------------


@pytest.fixture
def remote_modules(monkeypatch):
    """Start from no transformers_modules.* in sys.modules; add fakes by name."""
    for name in [n for n in sys.modules if n.startswith("transformers_modules")]:
        monkeypatch.delitem(sys.modules, name)

    def add(*names):
        for name in names:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))

    return add


def test_remote_code_revisions_empty_without_remote_code(remote_modules):
    assert remote_code_revisions() == set()


def test_remote_code_revisions_collects_each_commit_once(remote_modules):
    remote_modules(
        "transformers_modules",
        f"{NOMIC_CODE_PKG}.{PIN}",
        f"{NOMIC_CODE_PKG}.{PIN}.modeling_hf_nomic_bert",
        f"{NOMIC_CODE_PKG}.{PIN}.configuration_hf_nomic_bert",
        # a future transformers sanitizing a leading digit
        f"transformers_modules.org.repo._{OTHER_COMMIT}.modeling_x",
        f"{NOMIC_CODE_PKG}.main.modeling_hf_nomic_bert",
    )

    assert remote_code_revisions() == {PIN, OTHER_COMMIT}


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(f"{NOMIC_CODE_PKG}.main.modeling_x", id="branch-name"),
        pytest.param(f"{NOMIC_CODE_PKG}.{PIN[:39]}.modeling_x", id="39-hex"),
        pytest.param(f"{NOMIC_CODE_PKG}.{PIN}0.modeling_x", id="41-hex"),
        pytest.param(f"{NOMIC_CODE_PKG}.{PIN}{PIN[:24]}.modeling_x", id="64-hex-local-dir-hash"),
        pytest.param(f"{NOMIC_CODE_PKG}.{PIN.upper()}.modeling_x", id="uppercase-hex"),
        pytest.param(f"{NOMIC_CODE_PKG}.__{PIN}.modeling_x", id="double-underscore"),
        pytest.param(f"{NOMIC_CODE_PKG}.{PIN}_v2.modeling_x", id="hex-with-suffix"),
        pytest.param(f"some_package.{PIN}.module", id="not-remote-code"),
        pytest.param(f"transformers_modules_extra.{PIN}.module", id="lookalike-prefix"),
        pytest.param("transformers_modules", id="bare-package"),
    ],
)
def test_remote_code_revisions_ignores_non_commit_parts(remote_modules, name):
    remote_modules(name)
    assert remote_code_revisions() == set()


# ---------------------------------------------------------------------------
# embedding_pin._load_and_encode
# ---------------------------------------------------------------------------


def test_load_and_encode_returns_dimension_and_loaded_commits(monkeypatch, remote_modules):
    import sentence_transformers

    fake = _FakeModel(result=[0.1] * 768)
    constructed = []

    def fake_st(name, **kwargs):
        constructed.append((name, kwargs))
        remote_modules(f"{NOMIC_CODE_PKG}.{PIN}.modeling_hf_nomic_bert")  # as the real load would
        return fake

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", fake_st)

    assert embedding_pin._load_and_encode(DEFAULT_EMBEDDING_MODEL) == (768, {PIN})
    assert constructed == [(DEFAULT_EMBEDDING_MODEL, load_kwargs(DEFAULT_EMBEDDING_MODEL))]
    assert fake.encode_calls == [(("self-test",), {"normalize_embeddings": True})]


# ---------------------------------------------------------------------------
# embedding_pin.main (fetch | check)
# ---------------------------------------------------------------------------


@pytest.fixture
def cli(monkeypatch):
    """main() with the model load and the child `check` process faked.

    loads records (model, HF_HUB_OFFLINE at load time); runs records the
    subprocess.run calls. set_hub(None|True|False) fakes whether/how
    huggingface_hub.constants is already imported.
    """
    monkeypatch.setenv(OFFLINE_ENV, "untouched")
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    state = SimpleNamespace(result=(768, {PIN}), exc=None, child_rc=0, loads=[], runs=[])

    def fake_load(model):
        state.loads.append((model, os.environ.get(OFFLINE_ENV)))
        if state.exc is not None:
            raise state.exc
        return state.result

    def fake_run(cmd, **kwargs):
        state.runs.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, state.child_rc)

    def set_hub(offline):
        if offline is None:
            monkeypatch.delitem(sys.modules, "huggingface_hub.constants", raising=False)
        else:
            mod = types.ModuleType("huggingface_hub.constants")
            mod.HF_HUB_OFFLINE = offline
            monkeypatch.setitem(sys.modules, "huggingface_hub.constants", mod)

    monkeypatch.setattr(embedding_pin, "_load_and_encode", fake_load)
    monkeypatch.setattr(embedding_pin, "subprocess", SimpleNamespace(run=fake_run))
    state.set_hub = set_hub
    return state


def test_check_ok_loads_offline_and_reports(cli, capsys):
    cli.set_hub(True)

    assert embedding_pin.main(["check"]) == 0

    assert cli.loads == [(DEFAULT_EMBEDDING_MODEL, "1")]  # offline flag set BEFORE the load
    assert cli.runs == []
    out = capsys.readouterr().out
    assert out.startswith("check: OK")
    assert "768-dim" in out


@pytest.mark.parametrize(
    ("loaded", "shown"),
    [
        pytest.param({OTHER_COMMIT}, OTHER_COMMIT, id="other-commit"),
        pytest.param(set(), "no commit", id="no-commit"),
        pytest.param({PIN, OTHER_COMMIT}, OTHER_COMMIT, id="pin-plus-other"),
    ],
)
def test_check_refuses_default_model_from_unpinned_code(cli, capsys, loaded, shown):
    cli.set_hub(True)
    cli.result = (768, loaded)

    assert embedding_pin.main(["check"]) == 1

    captured = capsys.readouterr()
    assert "check: OK" not in captured.out
    assert "check: FAILED" in captured.err
    assert shown in captured.err
    assert PIN in captured.err


@pytest.mark.parametrize("loaded", [set(), {OTHER_COMMIT}], ids=["no-commit", "other-commit"])
def test_check_custom_model_skips_the_commit_test(cli, capsys, loaded):
    cli.set_hub(True)
    cli.result = (384, loaded)

    assert embedding_pin.main(["check", "--model", CUSTOM_MODEL]) == 0
    assert capsys.readouterr().out.startswith("check: OK")


def test_check_load_failure_returns_1_with_fetch_hint(cli, capsys):
    cli.set_hub(True)
    cli.exc = OSError("not in the cache")

    assert embedding_pin.main(["check"]) == 1

    err = capsys.readouterr().err
    assert "check: FAILED to load" in err
    assert "OSError" in err
    assert "embedding_pin fetch" in err
    assert cli.runs == []


@pytest.mark.parametrize(
    ("action", "hub_offline"),
    [pytest.param("check", False, id="check-hub-online"), pytest.param("fetch", True, id="fetch-hub-offline")],
)
def test_refuses_when_hub_already_imported_in_the_other_mode(cli, capsys, action, hub_offline):
    cli.set_hub(hub_offline)

    assert embedding_pin.main([action]) == 2

    assert cli.loads == []
    assert cli.runs == []
    assert os.environ[OFFLINE_ENV] == "untouched"
    assert "run it on its own" in capsys.readouterr().err


@pytest.mark.parametrize(("action", "flag"), [("check", "1"), ("fetch", "0")])
def test_sets_offline_flag_when_hub_not_imported_yet(cli, action, flag):
    cli.set_hub(None)

    embedding_pin.main([action])

    assert cli.loads == [(DEFAULT_EMBEDDING_MODEL, flag)]
    assert os.environ[OFFLINE_ENV] == flag


@pytest.mark.parametrize("child_rc", [0, 1, 3])
def test_fetch_downloads_online_then_checks_offline_in_a_fresh_process(cli, capsys, monkeypatch, child_rc):
    cli.set_hub(False)
    cli.child_rc = child_rc
    monkeypatch.setenv("BGW_TEST_PASSTHROUGH", "kept")

    assert embedding_pin.main(["fetch"]) == child_rc

    assert cli.loads == [(DEFAULT_EMBEDDING_MODEL, "0")]
    assert len(cli.runs) == 1
    cmd, kwargs = cli.runs[0]
    assert cmd == [sys.executable, "-m", "orchestrator.embedding_pin", "check", "--model", DEFAULT_EMBEDDING_MODEL]
    assert kwargs["check"] is False
    assert kwargs["env"][OFFLINE_ENV] == "1"
    assert kwargs["env"]["BGW_TEST_PASSTHROUGH"] == "kept"  # HF_HOME etc. reach the child
    assert os.environ[OFFLINE_ENV] == "0"  # the parent's own flag is not flipped back
    assert capsys.readouterr().out.startswith("fetch:")


def test_fetch_load_failure_returns_1_without_running_check(cli, capsys):
    cli.set_hub(False)
    cli.exc = ConnectionError("hub unreachable")

    assert embedding_pin.main(["fetch"]) == 1

    assert cli.runs == []
    err = capsys.readouterr().err
    assert "fetch: FAILED to load" in err
    assert "ConnectionError" in err
    assert "Not in the HF cache" not in err


@pytest.mark.parametrize(
    ("argv_model", "env_model", "expected"),
    [
        pytest.param("org/flag-model", "org/env-model", "org/flag-model", id="flag-wins"),
        pytest.param(None, "org/env-model", "org/env-model", id="env"),
        pytest.param(None, "", DEFAULT_EMBEDDING_MODEL, id="empty-env-is-default"),
        pytest.param(None, None, DEFAULT_EMBEDDING_MODEL, id="unset-is-default"),
    ],
)
@pytest.mark.parametrize("action", ["check", "fetch"])
def test_model_resolution(cli, monkeypatch, action, argv_model, env_model, expected):
    """--model, else $EMBEDDING_MODEL (ci.yml passes -e EMBEDDING_MODEL="" when unset), else the default."""
    cli.set_hub(action == "check")
    if env_model is not None:
        monkeypatch.setenv("EMBEDDING_MODEL", env_model)
    argv = [action] + (["--model", argv_model] if argv_model else [])

    assert embedding_pin.main(argv) == 0

    assert [m for m, _ in cli.loads] == [expected]
    if action == "fetch":
        assert cli.runs[0][0][-2:] == ["--model", expected]  # the child checks the same model


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param([], id="no-action"),
        pytest.param(["nope"], id="bad-action"),
        pytest.param(["check", "--model"], id="model-no-value"),
    ],
)
def test_cli_rejects_bad_arguments(cli, argv):
    with pytest.raises(SystemExit) as info:
        embedding_pin.main(argv)
    assert info.value.code == 2
    assert cli.loads == []


def _run_python(*args):
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    return subprocess.run(
        [sys.executable, *args], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60, check=False
    )


def test_cli_runs_as_a_module():
    proc = _run_python("-m", "orchestrator.embedding_pin", "--help")

    assert proc.returncode == 0, proc.stderr
    assert "fetch" in proc.stdout
    assert "check" in proc.stdout


def test_embedding_pin_import_is_stdlib_only():
    """fetch/check must set HF_HUB_OFFLINE before huggingface_hub is imported."""
    code = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import orchestrator.embedding_pin\n"
        "new = {m.split('.')[0] for m in set(sys.modules) - before}\n"
        "print(','.join(sorted(new - set(sys.stdlib_module_names) - {'orchestrator'})))\n"
    )
    proc = _run_python("-c", code)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


# ---------------------------------------------------------------------------
# shared._CountingEmbedder without a model (memory off)
# ---------------------------------------------------------------------------


def test_unloaded_embedder_is_unavailable():
    emb = _CountingEmbedder(None)
    assert emb.available is False
    assert getattr(emb, "available", True) is False  # how auto_learn reads it


def test_loaded_embedder_is_available():
    assert _CountingEmbedder(_FakeModel()).available is True


def test_unloaded_embedder_encode_raises_typed_error_without_counting():
    emb = _CountingEmbedder(None)
    before_here = _val("bgw_embedding_errors_total", caller="test_embedding_offline")
    before_all = _embedding_errors_all_callers()

    with pytest.raises(EmbeddingModelUnavailableError):
        emb.encode("anything", normalize_embeddings=True)

    assert _val("bgw_embedding_errors_total", caller="test_embedding_offline") == before_here
    assert _embedding_errors_all_callers() == before_all


@pytest.mark.parametrize("name", ["max_seq_length", "tokenizer", "get_sentence_embedding_dimension"])
def test_unloaded_embedder_attribute_error_names_the_attribute(name):
    with pytest.raises(AttributeError, match=name):
        getattr(_CountingEmbedder(None), name)


# ---------------------------------------------------------------------------
# shared._load_embedding_model
# ---------------------------------------------------------------------------


@pytest.fixture
def loader(monkeypatch):
    """SentenceTransformer, the commit scan and the offline flag faked in shared."""
    model = object()
    st = MagicMock(name="SentenceTransformer", return_value=model)
    revisions = MagicMock(name="remote_code_revisions", return_value={PIN})
    hub = SimpleNamespace(HF_HUB_OFFLINE=True)
    monkeypatch.setattr(shared, "SentenceTransformer", st)
    monkeypatch.setattr(shared, "remote_code_revisions", revisions)
    monkeypatch.setattr(shared, "hf_constants", hub)
    return SimpleNamespace(model=model, st=st, revisions=revisions, hub=hub)


def _shared_records(caplog, level):
    return [r for r in caplog.records if r.name == "orchestrator.shared" and r.levelno >= level]


def test_loader_returns_default_model_loaded_offline_from_the_pin(loader, caplog):
    with caplog.at_level(logging.WARNING, logger="orchestrator.shared"):
        assert shared._load_embedding_model(DEFAULT_EMBEDDING_MODEL) is loader.model

    loader.st.assert_called_once_with(
        DEFAULT_EMBEDDING_MODEL, local_files_only=True, **load_kwargs(DEFAULT_EMBEDDING_MODEL)
    )
    assert type(loader.st.call_args.kwargs["model_kwargs"]) is _KeepCodeRevision
    loader.revisions.assert_called_once_with()
    assert _shared_records(caplog, logging.WARNING) == []


@pytest.mark.parametrize(
    ("loaded", "shown"),
    [
        pytest.param({OTHER_COMMIT}, OTHER_COMMIT, id="other-commit"),
        pytest.param(set(), "no commit", id="no-commit"),
        pytest.param({PIN, OTHER_COMMIT}, OTHER_COMMIT, id="pin-plus-other"),
    ],
)
def test_loader_refuses_default_model_from_unpinned_code(loader, caplog, loaded, shown):
    loader.revisions.return_value = loaded

    with caplog.at_level(logging.ERROR, logger="orchestrator.shared"):
        assert shared._load_embedding_model(DEFAULT_EMBEDDING_MODEL) is None

    (record,) = _shared_records(caplog, logging.ERROR)
    assert record.msg.startswith("[EMBEDDING] Model REFUSED")
    assert shown in record.getMessage()
    assert PIN in record.getMessage()


def test_loader_custom_model_skips_the_commit_check(loader):
    loader.revisions.return_value = {OTHER_COMMIT}

    assert shared._load_embedding_model(CUSTOM_MODEL) is loader.model

    loader.st.assert_called_once_with(CUSTOM_MODEL, local_files_only=True, trust_remote_code=True)
    loader.revisions.assert_not_called()


@pytest.mark.parametrize("exc_type", [OSError, ValueError, RuntimeError, ImportError])
def test_loader_failure_returns_none_and_logs_only_the_type(loader, caplog, exc_type):
    loader.st.side_effect = exc_type("detail-marker /some/cache/path")

    with caplog.at_level(logging.ERROR, logger="orchestrator.shared"):
        assert shared._load_embedding_model(DEFAULT_EMBEDDING_MODEL) is None

    loader.revisions.assert_not_called()
    (record,) = _shared_records(caplog, logging.ERROR)
    assert record.msg.startswith("[EMBEDDING] Model load FAILED (%s)")
    assert record.args == (exc_type.__name__,)
    assert "detail-marker" not in record.getMessage()
    assert record.exc_info is not None


@pytest.mark.parametrize(("offline", "warned"), [(True, False), (False, True), (0, True)])
def test_loader_warns_when_hub_is_online(loader, caplog, offline, warned):
    loader.hub.HF_HUB_OFFLINE = offline

    with caplog.at_level(logging.WARNING, logger="orchestrator.shared"):
        assert shared._load_embedding_model(DEFAULT_EMBEDDING_MODEL) is loader.model

    warnings = [r for r in _shared_records(caplog, logging.WARNING) if "HF_HUB_OFFLINE is not set" in r.getMessage()]
    assert bool(warnings) is warned


def test_loader_real_library_uncached_model_fails_fast(caplog):
    """The installed sentence-transformers, offline, with a model that is not in the cache."""
    if not shared.hf_constants.HF_HUB_OFFLINE:
        pytest.skip("needs HF_HUB_OFFLINE=1 (set by the image and CI); online this would query the Hub")
    t0 = time.monotonic()

    with caplog.at_level(logging.ERROR, logger="orchestrator.shared"):
        assert shared._load_embedding_model("bgw-tests/no-such-embedding-model") is None

    assert time.monotonic() - t0 < 30
    assert any(r.msg.startswith("[EMBEDDING] Model load FAILED") for r in _shared_records(caplog, logging.ERROR))


# ---------------------------------------------------------------------------
# auto_learn.run_auto_learn with memory off
# ---------------------------------------------------------------------------

_RUN_OUTCOMES = ("stored", "duplicates_only", "no_facts", "opt_out", "error")


def _auto_learn_counts():
    return {
        "dropped": _val("bgw_auto_learn_dropped_total", reason="embedding_unavailable"),
        "extractions": _val("bgw_auto_learn_extractions_total"),
        **{f"run:{o}": _val("bgw_auto_learn_runs_total", outcome=o) for o in _RUN_OUTCOMES},
    }


def test_embedding_unavailable_drop_reason_is_exported_at_zero():
    # Pre-created label: rate()/increase() can't see a series' first sample.
    assert REGISTRY.get_sample_value("bgw_auto_learn_dropped_total", {"reason": "embedding_unavailable"}) is not None


async def test_run_auto_learn_drops_conversation_when_memory_off(monkeypatch, caplog):
    monkeypatch.setattr(shared, "embedding_model", _CountingEmbedder(None))
    extract = AsyncMock(return_value=[])
    monkeypatch.setattr(auto_learn, "extract_facts", extract)
    before = _auto_learn_counts()

    with caplog.at_level(logging.WARNING, logger="orchestrator.auto_learn"):
        assert await auto_learn.run_auto_learn(_CONV) is None

    extract.assert_not_awaited()
    after = _auto_learn_counts()
    assert {k: after[k] - before[k] for k in after} == {k: (1.0 if k == "dropped" else 0.0) for k in after}
    assert any(
        r.levelno == logging.WARNING and "embedding model is not loaded" in r.getMessage()
        for r in caplog.records
        if r.name == "orchestrator.auto_learn"
    )


@pytest.mark.parametrize(
    "embedder",
    [
        pytest.param(lambda: _CountingEmbedder(_FakeModel()), id="loaded-wrapper"),
        pytest.param(lambda: SimpleNamespace(encode=lambda *a, **k: [0.0]), id="bare-model-without-flag"),
    ],
)
async def test_run_auto_learn_extracts_when_memory_on(monkeypatch, embedder):
    monkeypatch.setattr(shared, "embedding_model", embedder())
    extract = AsyncMock(return_value=[])
    monkeypatch.setattr(auto_learn, "extract_facts", extract)
    before = _auto_learn_counts()

    await auto_learn.run_auto_learn(_CONV)

    extract.assert_awaited_once_with(_CONV)
    after = _auto_learn_counts()
    assert after["dropped"] == before["dropped"]
    assert after["extractions"] == before["extractions"] + 1
    assert after["run:no_facts"] == before["run:no_facts"] + 1


# ---------------------------------------------------------------------------
# EmbeddingModelUnavailableError at the edges (tools, API, per-turn RAG)
# ---------------------------------------------------------------------------


def test_embedding_unavailable_error_is_transient():
    assert issubclass(EmbeddingModelUnavailableError, TransientError)
    assert issubclass(EmbeddingModelUnavailableError, BrainGatewayError)
    assert not issubclass(EmbeddingModelUnavailableError, ExternalServiceError)


async def test_tool_that_needs_memory_reports_service_unavailable(monkeypatch):
    monkeypatch.setattr(shared, "embedding_model", _CountingEmbedder(None))

    async def handler(arguments):
        shared.embedding_model.encode(arguments["query"], normalize_embeddings=True)
        return "unreachable"

    monkeypatch.setitem(tool_registry._registry, "embedding_offline_probe", handler)
    before = _val("bgw_tool_call_errors_total", tool="embedding_offline_probe")

    result = await tool_registry.execute_tool("embedding_offline_probe", {"query": "anything"})

    assert result == "Service temporarily unavailable: the embedding model failed to load at startup"
    assert _val("bgw_tool_call_errors_total", tool="embedding_offline_probe") == before + 1


async def test_api_error_handler_maps_memory_off_to_503():
    from orchestrator.orchestrator import brain_gateway_error_handler

    resp = await brain_gateway_error_handler(MagicMock(), EmbeddingModelUnavailableError("memory off"))

    assert resp.status_code == 503
    assert json.loads(resp.body) == {"ok": False, "error": "Service temporarily unavailable"}


def test_per_turn_rag_context_is_empty_when_memory_off(monkeypatch):
    """Chat keeps working with memory off: the per-turn lookup degrades to no context."""
    query = MagicMock(name="collection.query")
    monkeypatch.setattr(prompt_builder, "embedding_model", _CountingEmbedder(None))
    monkeypatch.setattr(prompt_builder, "collection", MagicMock(query=query))
    before = _embedding_errors_all_callers()

    assert prompt_builder._rag_context_sync("what do I like to cook") == ""

    query.assert_not_called()
    assert _embedding_errors_all_callers() == before


# ---------------------------------------------------------------------------
# The image runs offline
# ---------------------------------------------------------------------------


def _dockerfile_instructions(text):
    """(INSTRUCTION, args) per logical line: comments dropped, continuations joined."""
    out, buf = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        word, _, rest = (buf + line).strip().partition(" ")
        out.append((word.upper(), rest.strip()))
        buf = ""
    return out


def _env_pairs(args):
    tokens = shlex.split(args)
    if tokens and "=" not in tokens[0]:  # legacy `ENV key value`
        return {tokens[0]: " ".join(tokens[1:])}
    return dict(tok.split("=", 1) for tok in tokens)


def test_dockerfile_sets_hf_hub_offline_in_the_final_stage():
    instructions = _dockerfile_instructions((REPO_ROOT / "orchestrator" / "Dockerfile").read_text())
    last_from = max(i for i, (word, _) in enumerate(instructions) if word == "FROM")
    env = {}
    for word, args in instructions[last_from:]:
        if word == "ENV":
            env.update(_env_pairs(args))

    assert env.get("HF_HUB_OFFLINE") == "1"
    # The offline load reads $HF_HOME: the path compose and the ci.yml pre-flight mount the cache on.
    assert env.get("HF_HOME") == "/home/jess/.cache/huggingface"
