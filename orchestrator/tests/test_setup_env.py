"""
Tests for orchestrator/setup_env.py — the first-boot env-overrides writer.

Covers:
- .env-style line parser and value escaping
- read/write round-trip + the file is `0600` after write
- allow-list enforcement (non-allow-listed keys raise ValueError)
- delete_key (present / absent / not-allow-listed)
- key_status_map redaction (secrets don't echo back)
- redact_for_log
- apply_to_environ
- dirty-since-boot flag
- validators (HA, Pushover, ntfy, Paperless) via httpx.MockTransport
- validate_service for known/unknown services and the no-validator path

All filesystem state is redirected into tmp_path via monkeypatching the
module-level `_OVERRIDES_PATH` constant. Requires full orchestrator deps
(httpx) — runs inside the brain-orchestrator container.
"""

from __future__ import annotations

import os
import stat

import pytest


def _can_import():
    try:
        import httpx  # noqa: F401

        from orchestrator import setup_env  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


_skip_no_deps = pytest.mark.skipif(
    not _can_import(),
    reason="setup_env requires full orchestrator deps (httpx)",
)


@pytest.fixture
def setup_env_mod(tmp_path, monkeypatch):
    if not _can_import():
        pytest.skip("deps unavailable")
    from orchestrator import setup_env as mod

    monkeypatch.setattr(mod, "_OVERRIDES_PATH", str(tmp_path / "setup_overrides.env"), raising=True)
    # Reset dirty flag — previous tests may have flipped it.
    monkeypatch.setattr(mod, "_dirty_since_boot", False, raising=True)
    return mod


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestParseEnvLine:
    def test_simple_key_value(self, setup_env_mod):
        assert setup_env_mod._parse_env_line("KEY=value") == ("KEY", "value")

    def test_strips_whitespace(self, setup_env_mod):
        assert setup_env_mod._parse_env_line("  KEY = value  \n") == ("KEY", "value")

    def test_strips_double_quotes(self, setup_env_mod):
        assert setup_env_mod._parse_env_line('KEY="hello world"') == ("KEY", "hello world")

    def test_strips_single_quotes(self, setup_env_mod):
        assert setup_env_mod._parse_env_line("KEY='hello world'") == ("KEY", "hello world")

    def test_mismatched_quotes_kept(self, setup_env_mod):
        # Mismatched quotes are not stripped — value passes through unchanged.
        assert setup_env_mod._parse_env_line("KEY=\"hello'") == ("KEY", "\"hello'")

    def test_blank_line_returns_none(self, setup_env_mod):
        assert setup_env_mod._parse_env_line("") is None
        assert setup_env_mod._parse_env_line("   ") is None

    def test_comment_returns_none(self, setup_env_mod):
        assert setup_env_mod._parse_env_line("# nothing here") is None

    def test_missing_equals_returns_none(self, setup_env_mod):
        assert setup_env_mod._parse_env_line("not a kv pair") is None

    def test_empty_key_returns_none(self, setup_env_mod):
        assert setup_env_mod._parse_env_line("=value") is None

    def test_empty_value_allowed(self, setup_env_mod):
        assert setup_env_mod._parse_env_line("KEY=") == ("KEY", "")

    def test_value_with_equals(self, setup_env_mod):
        # `partition` only splits on the FIRST `=`.
        assert setup_env_mod._parse_env_line("KEY=a=b") == ("KEY", "a=b")


# ---------------------------------------------------------------------------
# Escape
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestEscapeValue:
    def test_plain_value_unchanged(self, setup_env_mod):
        assert setup_env_mod._escape_value("hello") == "hello"

    def test_empty_stays_empty(self, setup_env_mod):
        assert setup_env_mod._escape_value("") == ""

    def test_value_with_space_quoted(self, setup_env_mod):
        assert setup_env_mod._escape_value("hello world") == '"hello world"'

    def test_value_with_hash_quoted(self, setup_env_mod):
        # `#` could start a comment if unquoted.
        assert setup_env_mod._escape_value("a#b") == '"a#b"'

    def test_double_quote_escaped(self, setup_env_mod):
        assert setup_env_mod._escape_value('a"b') == '"a\\"b"'

    def test_newline_escaped(self, setup_env_mod):
        assert setup_env_mod._escape_value("a\nb") == '"a\\nb"'


# ---------------------------------------------------------------------------
# read/write round-trip
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestReadWriteRoundTrip:
    def test_missing_file_returns_empty(self, setup_env_mod):
        assert setup_env_mod.read_overrides() == {}

    def test_write_then_read(self, setup_env_mod):
        setup_env_mod.set_keys({"VLLM_MODEL": "Qwen/Qwen3-8B-AWQ", "TTS_VOICE": "default"})
        loaded = setup_env_mod.read_overrides()
        assert loaded == {"VLLM_MODEL": "Qwen/Qwen3-8B-AWQ", "TTS_VOICE": "default"}

    def test_write_perms_0600(self, setup_env_mod):
        setup_env_mod.set_keys({"VLLM_MODEL": "test"})
        mode = stat.S_IMODE(os.stat(setup_env_mod._OVERRIDES_PATH).st_mode)
        assert mode == 0o600

    def test_merge_preserves_existing(self, setup_env_mod):
        setup_env_mod.set_keys({"VLLM_MODEL": "a"})
        setup_env_mod.set_keys({"TTS_VOICE": "b"})
        loaded = setup_env_mod.read_overrides()
        assert loaded == {"VLLM_MODEL": "a", "TTS_VOICE": "b"}

    def test_write_overwrites_same_key(self, setup_env_mod):
        setup_env_mod.set_keys({"VLLM_MODEL": "a"})
        setup_env_mod.set_keys({"VLLM_MODEL": "b"})
        assert setup_env_mod.read_overrides() == {"VLLM_MODEL": "b"}

    def test_non_allow_listed_in_file_silently_dropped_on_read(self, setup_env_mod, tmp_path):
        # If someone hand-edits a non-allow-listed key into the file, read
        # drops it — defence-in-depth so non-allow-listed values can never
        # be silently applied to os.environ.
        path = tmp_path / "setup_overrides.env"
        path.write_text("VLLM_MODEL=x\nDB_PASSWORD=secret\n")
        loaded = setup_env_mod.read_overrides()
        assert loaded == {"VLLM_MODEL": "x"}

    def test_quoted_value_round_trip(self, setup_env_mod):
        setup_env_mod.set_keys({"VLLM_MODEL": "has spaces"})
        # Read it back through our own reader.
        assert setup_env_mod.read_overrides() == {"VLLM_MODEL": "has spaces"}

    def test_corrupt_file_returns_empty(self, setup_env_mod, tmp_path):
        # Binary garbage → UnicodeDecodeError → warns + returns {}.
        path = tmp_path / "setup_overrides.env"
        path.write_bytes(b"\xff\xfe\xfd not utf-8")
        assert setup_env_mod.read_overrides() == {}


# ---------------------------------------------------------------------------
# Allow-list enforcement
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestAllowList:
    def test_set_rejects_non_allow_listed(self, setup_env_mod):
        with pytest.raises(ValueError, match="not in allow-list"):
            setup_env_mod.set_keys({"DB_PASSWORD": "shh"})

    def test_set_partial_reject_atomic(self, setup_env_mod):
        # If ANY key is invalid, no writes happen.
        with pytest.raises(ValueError):
            setup_env_mod.set_keys({"VLLM_MODEL": "ok", "DB_PASSWORD": "bad"})
        assert setup_env_mod.read_overrides() == {}

    def test_delete_rejects_non_allow_listed(self, setup_env_mod):
        with pytest.raises(ValueError, match="not in allow-list"):
            setup_env_mod.delete_key("DB_PASSWORD")

    def test_is_allowed_known_key(self, setup_env_mod):
        assert setup_env_mod.is_allowed("HA_TOKEN")
        assert not setup_env_mod.is_allowed("DB_PASSWORD")

    def test_is_secret(self, setup_env_mod):
        assert setup_env_mod.is_secret("HA_TOKEN")
        assert not setup_env_mod.is_secret("VLLM_MODEL")

    def test_set_rejects_empty_string_value(self, setup_env_mod):
        with pytest.raises(ValueError, match="empty value"):
            setup_env_mod.set_keys({"VLLM_MODEL": ""})

    def test_set_rejects_whitespace_only_value(self, setup_env_mod):
        # Stripped to empty → rejected (use DELETE to unset).
        with pytest.raises(ValueError, match="empty value"):
            setup_env_mod.set_keys({"VLLM_MODEL": "   "})

    def test_set_strips_surrounding_whitespace(self, setup_env_mod):
        # Pasted tokens often carry a trailing newline; we strip and accept.
        setup_env_mod.set_keys({"HA_TOKEN": "  my-token  "})
        assert setup_env_mod.read_overrides() == {"HA_TOKEN": "my-token"}

    def test_set_rejects_control_chars(self, setup_env_mod):
        # Embedded newline / NUL / DEL — would break the .env parser and let
        # log-injection through.
        for bad in ("a\nb", "a\x00b", "a\x7fb", "a\tb", "a\rb"):
            with pytest.raises(ValueError, match="control characters"):
                setup_env_mod.set_keys({"HA_TOKEN": bad})

    def test_set_rejects_batch_atomically(self, setup_env_mod):
        # If any one value is bad, NO write happens.
        with pytest.raises(ValueError):
            setup_env_mod.set_keys({"VLLM_MODEL": "ok", "HA_TOKEN": "bad\nvalue"})
        assert setup_env_mod.read_overrides() == {}


# ---------------------------------------------------------------------------
# delete_key
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestDeleteKey:
    def test_delete_present(self, setup_env_mod):
        setup_env_mod.set_keys({"VLLM_MODEL": "x"})
        assert setup_env_mod.delete_key("VLLM_MODEL") is True
        assert setup_env_mod.read_overrides() == {}

    def test_delete_absent(self, setup_env_mod):
        assert setup_env_mod.delete_key("VLLM_MODEL") is False


# ---------------------------------------------------------------------------
# key_status_map — the GET response shape
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestKeyStatusMap:
    def test_secret_present_no_value(self, setup_env_mod):
        setup_env_mod.set_keys({"HA_TOKEN": "secret-token"})
        m = setup_env_mod.key_status_map()
        assert m["HA_TOKEN"]["present"] is True
        assert m["HA_TOKEN"]["secret"] is True
        assert "value" not in m["HA_TOKEN"]  # NEVER echoed

    def test_non_secret_present_has_value(self, setup_env_mod):
        setup_env_mod.set_keys({"VLLM_MODEL": "Qwen/Qwen3-8B-AWQ"})
        m = setup_env_mod.key_status_map()
        assert m["VLLM_MODEL"]["present"] is True
        assert m["VLLM_MODEL"]["secret"] is False
        assert m["VLLM_MODEL"]["value"] == "Qwen/Qwen3-8B-AWQ"

    def test_absent_key_no_value(self, setup_env_mod):
        m = setup_env_mod.key_status_map()
        assert m["HA_TOKEN"]["present"] is False
        assert "value" not in m["HA_TOKEN"]

    def test_includes_every_allow_listed_key(self, setup_env_mod):
        m = setup_env_mod.key_status_map()
        assert set(m.keys()) == set(setup_env_mod.ALLOWED_KEYS.keys())


# ---------------------------------------------------------------------------
# redact_for_log
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestRedactForLog:
    def test_secret_masked(self, setup_env_mod):
        out = setup_env_mod.redact_for_log({"HA_TOKEN": "actual-token-value"})
        assert out == {"HA_TOKEN": "***"}

    def test_non_secret_passes_through(self, setup_env_mod):
        out = setup_env_mod.redact_for_log({"VLLM_MODEL": "Qwen/Qwen3-8B-AWQ"})
        assert out == {"VLLM_MODEL": "Qwen/Qwen3-8B-AWQ"}

    def test_mixed(self, setup_env_mod):
        out = setup_env_mod.redact_for_log({"HA_TOKEN": "shh", "VLLM_MODEL": "x"})
        assert out == {"HA_TOKEN": "***", "VLLM_MODEL": "x"}

    def test_non_secret_control_chars_stripped(self, setup_env_mod):
        # An attacker can't smuggle ANSI escapes through a non-secret URL into
        # operator-viewed logs.
        out = setup_env_mod.redact_for_log({"HA_URL": "http://x\x1b]0;PWND\x07"})
        # \x1b and \x07 both replaced with ?
        assert out["HA_URL"] == "http://x?]0;PWND?"


# ---------------------------------------------------------------------------
# apply_to_environ
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestApplyToEnviron:
    def test_applies_keys(self, setup_env_mod):
        setup_env_mod.set_keys({"VLLM_MODEL": "test-value"})
        env = {}
        applied = setup_env_mod.apply_to_environ(env)
        assert env == {"VLLM_MODEL": "test-value"}
        assert applied == ["VLLM_MODEL"]

    def test_missing_file_no_op(self, setup_env_mod):
        env = {}
        applied = setup_env_mod.apply_to_environ(env)
        assert env == {}
        assert applied == []

    def test_overwrites_existing(self, setup_env_mod):
        setup_env_mod.set_keys({"VLLM_MODEL": "new"})
        env = {"VLLM_MODEL": "old", "OTHER": "untouched"}
        setup_env_mod.apply_to_environ(env)
        assert env == {"VLLM_MODEL": "new", "OTHER": "untouched"}


# ---------------------------------------------------------------------------
# dirty-since-boot flag
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestDirtyFlag:
    def test_starts_false(self, setup_env_mod):
        assert setup_env_mod.is_dirty_since_boot() is False

    def test_set_marks_dirty(self, setup_env_mod):
        setup_env_mod.set_keys({"VLLM_MODEL": "x"})
        assert setup_env_mod.is_dirty_since_boot() is True

    def test_delete_marks_dirty(self, setup_env_mod):
        setup_env_mod.set_keys({"VLLM_MODEL": "x"})
        # Reset flag to isolate the delete effect.
        import orchestrator.setup_env as mod

        mod._dirty_since_boot = False
        setup_env_mod.delete_key("VLLM_MODEL")
        assert setup_env_mod.is_dirty_since_boot() is True


# ---------------------------------------------------------------------------
# Validators — using httpx.MockTransport
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestValidators:
    def _patch_transport(self, monkeypatch, setup_env_mod, handler):
        """Replace setup_env._http_client with one that uses MockTransport."""
        import httpx

        def _client_factory(timeout: float = 8.0):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

        monkeypatch.setattr(setup_env_mod, "_http_client", _client_factory)

    @pytest.mark.asyncio
    async def test_ha_success(self, setup_env_mod, monkeypatch):
        import httpx

        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.path == "/api/"
            assert req.headers["Authorization"] == "Bearer my-token"
            return httpx.Response(200, json={"message": "API running"})

        self._patch_transport(monkeypatch, setup_env_mod, handler)
        ok, detail = await setup_env_mod._validate_ha({"HA_URL": "http://ha.local:8123", "HA_TOKEN": "my-token"})
        assert ok is True
        assert "Home Assistant" in detail

    @pytest.mark.asyncio
    async def test_ha_unauthorized(self, setup_env_mod, monkeypatch):
        import httpx

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(401)

        self._patch_transport(monkeypatch, setup_env_mod, handler)
        ok, detail = await setup_env_mod._validate_ha({"HA_URL": "http://ha.local", "HA_TOKEN": "bad"})
        assert ok is False
        assert "rejected" in detail.lower()

    @pytest.mark.asyncio
    async def test_ha_missing_inputs(self, setup_env_mod):
        ok, detail = await setup_env_mod._validate_ha({"HA_URL": "http://x"})
        assert ok is False
        assert "required" in detail.lower()

    @pytest.mark.asyncio
    async def test_ha_network_error(self, setup_env_mod, monkeypatch):
        import httpx

        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        self._patch_transport(monkeypatch, setup_env_mod, handler)
        ok, detail = await setup_env_mod._validate_ha({"HA_URL": "http://x", "HA_TOKEN": "y"})
        assert ok is False
        assert "Could not reach" in detail
        # Regression: the httpx exception class name must NOT appear in the
        # detail (hacker review finding — was a port-scan oracle).
        assert "ConnectError" not in detail
        assert "ReadError" not in detail
        assert "RemoteProtocolError" not in detail

    @pytest.mark.asyncio
    async def test_ha_invalid_url_caught(self, setup_env_mod, monkeypatch):
        """httpx.InvalidURL (CRLF in URL etc.) must NOT escape the validator
        as a 500. Regression for the hacker review's finding #3."""
        import httpx

        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.InvalidURL("CRLF in URL")

        self._patch_transport(monkeypatch, setup_env_mod, handler)
        ok, detail = await setup_env_mod._validate_ha({"HA_URL": "http://x", "HA_TOKEN": "y"})
        assert ok is False
        assert "Could not reach" in detail

    @pytest.mark.asyncio
    async def test_ha_rejects_url_with_fragment(self, setup_env_mod):
        """URL fragment → path appended inside the fragment → request goes to /
        with no auth path → any 200-on-/ server validates with a junk token.
        Regression for the hacker review's finding #2 (HIGH)."""
        ok, detail = await setup_env_mod._validate_ha({"HA_URL": "http://10.0.0.248:3000#x", "HA_TOKEN": "x"})
        assert ok is False
        assert "fragment" in detail.lower()

    @pytest.mark.asyncio
    async def test_ha_rejects_url_with_query(self, setup_env_mod):
        ok, detail = await setup_env_mod._validate_ha({"HA_URL": "http://ha.local?foo=bar", "HA_TOKEN": "x"})
        assert ok is False
        assert "query" in detail.lower()

    @pytest.mark.asyncio
    async def test_ha_rejects_non_http_scheme(self, setup_env_mod):
        ok, detail = await setup_env_mod._validate_ha({"HA_URL": "file:///etc/shadow", "HA_TOKEN": "x"})
        assert ok is False
        assert "http" in detail.lower()

    @pytest.mark.asyncio
    async def test_paperless_rejects_url_with_fragment(self, setup_env_mod):
        """Same fragment-trick on Paperless. Regression for hacker finding #2."""
        ok, detail = await setup_env_mod._validate_paperless(
            {"PAPERLESS_URL": "http://10.0.0.106:8123#x", "PAPERLESS_API_TOKEN": "x"}
        )
        assert ok is False
        assert "fragment" in detail.lower()

    @pytest.mark.asyncio
    async def test_ntfy_rejects_url_with_fragment(self, setup_env_mod):
        ok, detail = await setup_env_mod._validate_ntfy({"NTFY_URL": "http://ntfy.local#x", "NTFY_TOPIC": "topic"})
        assert ok is False
        assert "fragment" in detail.lower()

    @pytest.mark.asyncio
    async def test_pushover_success(self, setup_env_mod, monkeypatch):
        import httpx

        def handler(req: httpx.Request) -> httpx.Response:
            assert "pushover.net" in str(req.url)
            return httpx.Response(200, json={"status": 1})

        self._patch_transport(monkeypatch, setup_env_mod, handler)
        ok, detail = await setup_env_mod._validate_pushover({"PUSHOVER_USER_KEY": "u", "PUSHOVER_APP_TOKEN": "t"})
        assert ok is True

    @pytest.mark.asyncio
    async def test_pushover_rejected(self, setup_env_mod, monkeypatch):
        import httpx

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": 0, "errors": ["user identifier is invalid"]})

        self._patch_transport(monkeypatch, setup_env_mod, handler)
        ok, detail = await setup_env_mod._validate_pushover({"PUSHOVER_USER_KEY": "bad", "PUSHOVER_APP_TOKEN": "bad"})
        assert ok is False
        assert "invalid" in detail.lower()

    @pytest.mark.asyncio
    async def test_ntfy_success(self, setup_env_mod, monkeypatch):
        import httpx

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        self._patch_transport(monkeypatch, setup_env_mod, handler)
        ok, _ = await setup_env_mod._validate_ntfy({"NTFY_URL": "http://ntfy.local", "NTFY_TOPIC": "jess"})
        assert ok is True

    @pytest.mark.asyncio
    async def test_paperless_success(self, setup_env_mod, monkeypatch):
        import httpx

        def handler(req: httpx.Request) -> httpx.Response:
            assert req.headers["Authorization"].startswith("Token ")
            return httpx.Response(200)

        self._patch_transport(monkeypatch, setup_env_mod, handler)
        ok, _ = await setup_env_mod._validate_paperless({"PAPERLESS_URL": "http://p.local", "PAPERLESS_API_TOKEN": "t"})
        assert ok is True


# ---------------------------------------------------------------------------
# _validate_url helper
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestValidateUrl:
    def test_plain_http_ok(self, setup_env_mod):
        ok, _ = setup_env_mod._validate_url("http://ha.local:8123")
        assert ok is True

    def test_plain_https_ok(self, setup_env_mod):
        ok, _ = setup_env_mod._validate_url("https://api.example.com")
        assert ok is True

    def test_trailing_slash_ok(self, setup_env_mod):
        # rstrip("/") on the caller side — the helper itself accepts trailing.
        ok, _ = setup_env_mod._validate_url("http://ha.local/")
        assert ok is True

    def test_rejects_fragment(self, setup_env_mod):
        ok, why = setup_env_mod._validate_url("http://ha.local#frag")
        assert ok is False
        assert "fragment" in why.lower()

    def test_rejects_query(self, setup_env_mod):
        ok, why = setup_env_mod._validate_url("http://ha.local?x=1")
        assert ok is False
        assert "query" in why.lower()

    def test_rejects_params(self, setup_env_mod):
        # The `;params` segment in a URL path is rare but parseable.
        ok, why = setup_env_mod._validate_url("http://ha.local/path;p=1")
        assert ok is False

    def test_rejects_file_scheme(self, setup_env_mod):
        ok, why = setup_env_mod._validate_url("file:///etc/shadow")
        assert ok is False
        assert "http" in why.lower()

    def test_rejects_gopher_scheme(self, setup_env_mod):
        ok, _ = setup_env_mod._validate_url("gopher://x/")
        assert ok is False

    def test_rejects_missing_scheme(self, setup_env_mod):
        ok, _ = setup_env_mod._validate_url("ha.local:8123")
        assert ok is False

    def test_rejects_missing_host(self, setup_env_mod):
        ok, why = setup_env_mod._validate_url("http://")
        assert ok is False
        assert "host" in why.lower()

    def test_rejects_userinfo(self, setup_env_mod):
        # user:pass@ in URL leaks creds into access logs + double-auths the
        # request (one via the wizard's Authorization header).
        ok, why = setup_env_mod._validate_url("http://user:pass@ha.local")
        assert ok is False
        assert "credentials" in why.lower()

    def test_rejects_username_only(self, setup_env_mod):
        ok, why = setup_env_mod._validate_url("http://user@ha.local")
        assert ok is False

    def test_rejects_trailing_whitespace(self, setup_env_mod):
        ok, why = setup_env_mod._validate_url("http://ha.local  ")
        assert ok is False
        assert "whitespace" in why.lower()

    def test_rejects_leading_whitespace(self, setup_env_mod):
        ok, why = setup_env_mod._validate_url("  http://ha.local")
        assert ok is False

    def test_rejects_control_chars(self, setup_env_mod):
        ok, why = setup_env_mod._validate_url("http://ha.local\nX-Evil: 1")
        assert ok is False
        assert "control" in why.lower()


# ---------------------------------------------------------------------------
# validate_service dispatch
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestValidateService:
    @pytest.mark.asyncio
    async def test_unknown_service(self, setup_env_mod):
        ok, detail = await setup_env_mod.validate_service("nonsense", {})
        assert ok is False
        assert "Unknown service" in detail

    @pytest.mark.asyncio
    async def test_no_validator_service_ok(self, setup_env_mod):
        # `model` and `voice` are valid services but have no live validator —
        # the dispatcher returns OK so the caller can persist directly.
        ok, detail = await setup_env_mod.validate_service("model", {})
        assert ok is True
        assert "No live validation" in detail
