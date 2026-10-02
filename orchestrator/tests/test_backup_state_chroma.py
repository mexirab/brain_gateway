"""
Tests for scripts/backup_state.py chroma resolution (_chroma_dir) and staging.

The live mempalace is mounted from CHROMA_HOST_PATH (e.g.
~/.local/share/chroma), outside data/ — before this fix the nightly backup
only carried a stale data/chroma copy. Resolution order:
JESS_CHROMA_DIR env → CHROMA_HOST_PATH in <REPO_ROOT>/.env → <REPO_ROOT>/data/chroma.

The script lives outside the orchestrator package, so it's imported by file
path (importlib) relative to this test file: <repo>/scripts/backup_state.py.
In the container, mount the repo so that layout holds (see report).
"""

from __future__ import annotations

import importlib.util
import tarfile
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "backup_state.py"

pytestmark = pytest.mark.skipif(not _SCRIPT.exists(), reason=f"{_SCRIPT} not present (mount scripts/)")


@pytest.fixture
def bs(monkeypatch, tmp_path):
    """Fresh module instance with REPO_ROOT pointed at an empty temp repo."""
    monkeypatch.delenv("JESS_CHROMA_DIR", raising=False)
    monkeypatch.delenv("JESS_BACKUP_REMOTE", raising=False)
    monkeypatch.delenv("JESS_BACKUP_METRICS_PATH", raising=False)
    spec = importlib.util.spec_from_file_location("backup_state_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(mod, "REPO_ROOT", repo)
    monkeypatch.setattr(mod, "REMOTE", "")
    monkeypatch.setattr(mod, "METRICS_PATH", "")
    return mod


# ---------------------------------------------------------------------------
# _chroma_dir
# ---------------------------------------------------------------------------


def test_env_override_wins(bs, monkeypatch, tmp_path):
    (bs.REPO_ROOT / ".env").write_text("CHROMA_HOST_PATH=/should/not/win\n")
    monkeypatch.setenv("JESS_CHROMA_DIR", str(tmp_path / "explicit"))
    assert bs._chroma_dir() == tmp_path / "explicit"


def test_env_override_expands_tilde(bs, monkeypatch):
    monkeypatch.setenv("JESS_CHROMA_DIR", "~/somewhere")
    assert bs._chroma_dir() == Path("~/somewhere").expanduser()


@pytest.mark.parametrize(
    "line",
    ["CHROMA_HOST_PATH=/srv/chroma", 'CHROMA_HOST_PATH="/srv/chroma"', "CHROMA_HOST_PATH='/srv/chroma'"],
)
def test_dotenv_absolute_quoted_and_unquoted(bs, line):
    (bs.REPO_ROOT / ".env").write_text(f"FOO=bar\n{line}\nOTHER=x\n")
    assert bs._chroma_dir() == Path("/srv/chroma")


def test_dotenv_tilde_expanded(bs):
    (bs.REPO_ROOT / ".env").write_text("CHROMA_HOST_PATH=~/.local/share/chroma\n")
    assert bs._chroma_dir() == Path("~/.local/share/chroma").expanduser()


def test_dotenv_relative_is_under_repo_root(bs):
    (bs.REPO_ROOT / ".env").write_text("CHROMA_HOST_PATH=./vol/chroma\n")
    got = bs._chroma_dir()
    assert got.is_absolute()
    assert got == bs.REPO_ROOT / "vol" / "chroma"


def test_dotenv_empty_value_falls_back(bs):
    (bs.REPO_ROOT / ".env").write_text("CHROMA_HOST_PATH=\n")
    assert bs._chroma_dir() == bs.REPO_ROOT / "data" / "chroma"


def test_dotenv_commented_key_ignored(bs):
    (bs.REPO_ROOT / ".env").write_text("# CHROMA_HOST_PATH=/srv/chroma\n")
    assert bs._chroma_dir() == bs.REPO_ROOT / "data" / "chroma"


def test_missing_dotenv_falls_back(bs):
    assert bs._chroma_dir() == bs.REPO_ROOT / "data" / "chroma"


# ---------------------------------------------------------------------------
# main() staging
# ---------------------------------------------------------------------------


def _layout(bs, monkeypatch, tmp_path, chroma: Path):
    data = tmp_path / "data"
    (data / "app").mkdir(parents=True)
    (data / "app" / "notes.txt").write_text("n")
    creds = tmp_path / "credentials"
    creds.mkdir()
    chroma.mkdir(parents=True, exist_ok=True)
    (chroma / "personal_rag").mkdir(exist_ok=True)
    (chroma / "personal_rag" / "blob.bin").write_bytes(b"vec")
    backups = tmp_path / "backups"
    monkeypatch.setattr(bs, "DATA_DIR", data)
    monkeypatch.setattr(bs, "CREDENTIALS_DIR", creds)
    monkeypatch.setattr(bs, "CHROMA_DIR", chroma)
    monkeypatch.setattr(bs, "BACKUP_DIR", backups)
    return backups


def _names(backups: Path) -> set[str]:
    archives = list(backups.glob("jess-state-*.tar.gz"))
    assert len(archives) == 1, archives
    with tarfile.open(archives[0]) as tar:
        return set(tar.getnames())


def test_main_stages_external_chroma_under_chroma_arcname(bs, monkeypatch, tmp_path):
    backups = _layout(bs, monkeypatch, tmp_path, tmp_path / "elsewhere" / "chroma")
    assert bs.main() == 0
    names = _names(backups)
    assert "chroma/personal_rag/blob.bin" in names
    assert "data/app/notes.txt" in names
    assert not any(n.startswith("data/chroma") for n in names)


def test_main_does_not_double_stage_chroma_inside_data(bs, monkeypatch, tmp_path):
    backups = _layout(bs, monkeypatch, tmp_path, tmp_path / "data" / "chroma")
    assert bs.main() == 0
    names = _names(backups)
    assert "data/chroma/personal_rag/blob.bin" in names
    assert not any(n == "chroma" or n.startswith("chroma/") for n in names)


def test_main_warns_when_chroma_missing(bs, monkeypatch, tmp_path, capsys):
    backups = _layout(bs, monkeypatch, tmp_path, tmp_path / "elsewhere" / "chroma")
    missing = tmp_path / "nope" / "chroma"
    monkeypatch.setattr(bs, "CHROMA_DIR", missing)
    assert bs.main() == 0
    out = capsys.readouterr()
    assert "chroma dir" in (out.out + out.err) and "NOT in this backup" in (out.out + out.err)
    assert not any(n.startswith("chroma/") for n in _names(backups))
