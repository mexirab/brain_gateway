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
- _validate_url, the URL check every validator runs first

validate_service dispatch is covered through the route in test_routes_setup.py
(TestValidateEnv). All filesystem state is redirected into tmp_path via
monkeypatching the module-level `_OVERRIDES_PATH` constant.
"""

from __future__ import annotations

import os
import stat

import httpx
import pytest


@pytest.fixture
def setup_env_mod(tmp_path, monkeypatch):
    from orchestrator import setup_env as mod

    monkeypatch.setattr(mod, "_OVERRIDES_PATH", str(tmp_path / "setup_overrides.env"), raising=True)
    # Reset dirty flag — previous tests may have flipped it.
    monkeypatch.setattr(mod, "_dirty_since_boot", False, raising=True)
    return mod


# ---------------------------------------------------------------------------
# Parser + escaping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line,expected",
    [
        pytest.param("KEY=value", ("KEY", "value"), id="simple"),
        pytest.param("  KEY = value  \n", ("KEY", "value"), id="whitespace-stripped"),
        pytest.param('KEY="hello world"', ("KEY", "hello world"), id="double-quotes"),
        pytest.param("KEY='hello world'", ("KEY", "hello world"), id="single-quotes"),
        # Mismatched quotes are not stripped — value passes through unchanged.
        pytest.param("KEY=\"hello'", ("KEY", "\"hello'"), id="mismatched-quotes-kept"),
        pytest.param("", None, id="blank"),
        pytest.param("   ", None, id="blank-spaces"),
        pytest.param("# nothing here", None, id="comment"),
        pytest.param("not a kv pair", None, id="no-equals"),
        pytest.param("=value", None, id="empty-key"),
        pytest.param("KEY=", ("KEY", ""), id="empty-value"),
        # `partition` only splits on the FIRST `=`.
        pytest.param("KEY=a=b", ("KEY", "a=b"), id="value-with-equals"),
    ],
)
def test_parse_env_line(setup_env_mod, line, expected):
    assert setup_env_mod._parse_env_line(line) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        pytest.param("hello", "hello", id="plain"),
        pytest.param("", "", id="empty"),
        pytest.param("hello world", '"hello world"', id="space-quoted"),
        # `#` could start a comment if unquoted.
        pytest.param("a#b", '"a#b"', id="hash-quoted"),
        pytest.param('a"b', '"a\\"b"', id="double-quote-escaped"),
        pytest.param("a\nb", '"a\\nb"', id="newline-escaped"),
    ],
)
def test_escape_value(setup_env_mod, value, expected):
    assert setup_env_mod._escape_value(value) == expected


# ---------------------------------------------------------------------------
# read/write round-trip
# ---------------------------------------------------------------------------


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

    # Stripped to empty → rejected (use DELETE to unset).
    @pytest.mark.parametrize("value", [pytest.param("", id="empty"), pytest.param("   ", id="whitespace-only")])
    def test_set_rejects_empty_value(self, setup_env_mod, value):
        with pytest.raises(ValueError, match="empty value"):
            setup_env_mod.set_keys({"VLLM_MODEL": value})

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


@pytest.mark.parametrize(
    "values,expected",
    [
        pytest.param({"HA_TOKEN": "actual-token-value"}, {"HA_TOKEN": "***"}, id="secret-masked"),
        pytest.param({"VLLM_MODEL": "Qwen/Qwen3-8B-AWQ"}, {"VLLM_MODEL": "Qwen/Qwen3-8B-AWQ"}, id="non-secret"),
        pytest.param({"HA_TOKEN": "shh", "VLLM_MODEL": "x"}, {"HA_TOKEN": "***", "VLLM_MODEL": "x"}, id="mixed"),
        # An attacker can't smuggle ANSI escapes through a non-secret URL into
        # operator-viewed logs: \x1b and \x07 both become "?".
        pytest.param({"HA_URL": "http://x\x1b]0;PWND\x07"}, {"HA_URL": "http://x?]0;PWND?"}, id="control-chars"),
    ],
)
def test_redact_for_log(setup_env_mod, values, expected):
    assert setup_env_mod.redact_for_log(values) == expected


# ---------------------------------------------------------------------------
# apply_to_environ
# ---------------------------------------------------------------------------


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


class TestDirtyFlag:
    def test_starts_false(self, setup_env_mod):
        assert setup_env_mod.is_dirty_since_boot() is False

    def test_set_marks_dirty(self, setup_env_mod):
        setup_env_mod.set_keys({"VLLM_MODEL": "x"})
        assert setup_env_mod.is_dirty_since_boot() is True

    def test_delete_marks_dirty(self, setup_env_mod):
        setup_env_mod.set_keys({"VLLM_MODEL": "x"})
        setup_env_mod._dirty_since_boot = False  # isolate the delete effect
        setup_env_mod.delete_key("VLLM_MODEL")
        assert setup_env_mod.is_dirty_since_boot() is True


# ---------------------------------------------------------------------------
# Validators — using httpx.MockTransport
# ---------------------------------------------------------------------------


class TestValidators:
    def _patch_transport(self, monkeypatch, setup_env_mod, handler):
        """Replace setup_env._http_client with one that uses MockTransport."""

        def _client_factory(timeout: float = 8.0):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

        monkeypatch.setattr(setup_env_mod, "_http_client", _client_factory)

    async def test_ha_success(self, setup_env_mod, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.path == "/api/"
            assert req.headers["Authorization"] == "Bearer my-token"
            return httpx.Response(200, json={"message": "API running"})

        self._patch_transport(monkeypatch, setup_env_mod, handler)
        ok, detail = await setup_env_mod._validate_ha({"HA_URL": "http://ha.local:8123", "HA_TOKEN": "my-token"})
        assert ok is True
        assert "Home Assistant" in detail

    async def test_ha_unauthorized(self, setup_env_mod, monkeypatch):
        self._patch_transport(monkeypatch, setup_env_mod, lambda req: httpx.Response(401))
        ok, detail = await setup_env_mod._validate_ha({"HA_URL": "http://ha.local", "HA_TOKEN": "bad"})
        assert ok is False
        assert "rejected" in detail.lower()

    async def test_ha_missing_inputs(self, setup_env_mod):
        ok, detail = await setup_env_mod._validate_ha({"HA_URL": "http://x"})
        assert ok is False
        assert "required" in detail.lower()

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(httpx.ConnectError("boom"), id="network-error"),
            # httpx.InvalidURL (CRLF in URL etc.) must NOT escape the validator
            # as a 500. Regression for the hacker review's finding #3.
            pytest.param(httpx.InvalidURL("CRLF in URL"), id="invalid-url"),
        ],
    )
    async def test_ha_unreachable(self, setup_env_mod, monkeypatch, exc):
        def handler(req: httpx.Request) -> httpx.Response:
            raise exc

        self._patch_transport(monkeypatch, setup_env_mod, handler)
        ok, detail = await setup_env_mod._validate_ha({"HA_URL": "http://x", "HA_TOKEN": "y"})
        assert ok is False
        assert "Could not reach" in detail
        # Regression: the httpx exception class name must NOT appear in the
        # detail (hacker review finding — was a port-scan oracle).
        assert "ConnectError" not in detail
        assert "ReadError" not in detail
        assert "RemoteProtocolError" not in detail

    # URL fragment → path appended inside the fragment → request goes to / with
    # no auth path → any 200-on-/ server validates with a junk token.
    # Regression for the hacker review's finding #2 (HIGH), on each validator.
    @pytest.mark.parametrize(
        "validator,values,needle",
        [
            pytest.param(
                "_validate_ha", {"HA_URL": "http://10.0.0.248:3000#x", "HA_TOKEN": "x"}, "fragment", id="ha-fragment"
            ),
            pytest.param(
                "_validate_ha", {"HA_URL": "http://ha.local?foo=bar", "HA_TOKEN": "x"}, "query", id="ha-query"
            ),
            pytest.param("_validate_ha", {"HA_URL": "file:///etc/shadow", "HA_TOKEN": "x"}, "http", id="ha-scheme"),
            pytest.param(
                "_validate_paperless",
                {"PAPERLESS_URL": "http://10.0.0.106:8123#x", "PAPERLESS_API_TOKEN": "x"},
                "fragment",
                id="paperless-fragment",
            ),
            pytest.param(
                "_validate_ntfy",
                {"NTFY_URL": "http://ntfy.local#x", "NTFY_TOPIC": "topic"},
                "fragment",
                id="ntfy-fragment",
            ),
        ],
    )
    async def test_bad_url_rejected_before_any_request(self, setup_env_mod, validator, values, needle):
        ok, detail = await getattr(setup_env_mod, validator)(values)
        assert ok is False
        assert needle in detail.lower()

    async def test_pushover_success(self, setup_env_mod, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            assert "pushover.net" in str(req.url)
            return httpx.Response(200, json={"status": 1})

        self._patch_transport(monkeypatch, setup_env_mod, handler)
        ok, detail = await setup_env_mod._validate_pushover({"PUSHOVER_USER_KEY": "u", "PUSHOVER_APP_TOKEN": "t"})
        assert ok is True

    async def test_pushover_rejected(self, setup_env_mod, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": 0, "errors": ["user identifier is invalid"]})

        self._patch_transport(monkeypatch, setup_env_mod, handler)
        ok, detail = await setup_env_mod._validate_pushover({"PUSHOVER_USER_KEY": "bad", "PUSHOVER_APP_TOKEN": "bad"})
        assert ok is False
        assert "invalid" in detail.lower()

    async def test_ntfy_success(self, setup_env_mod, monkeypatch):
        self._patch_transport(monkeypatch, setup_env_mod, lambda req: httpx.Response(200))
        ok, _ = await setup_env_mod._validate_ntfy({"NTFY_URL": "http://ntfy.local", "NTFY_TOPIC": "jess"})
        assert ok is True

    async def test_paperless_success(self, setup_env_mod, monkeypatch):
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.headers["Authorization"].startswith("Token ")
            return httpx.Response(200)

        self._patch_transport(monkeypatch, setup_env_mod, handler)
        ok, _ = await setup_env_mod._validate_paperless({"PAPERLESS_URL": "http://p.local", "PAPERLESS_API_TOKEN": "t"})
        assert ok is True


# ---------------------------------------------------------------------------
# _validate_url helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://ha.local:8123", id="http"),
        pytest.param("https://api.example.com", id="https"),
        # rstrip("/") on the caller side — the helper itself accepts trailing.
        pytest.param("http://ha.local/", id="trailing-slash"),
        # Boundary: 65535 is the highest valid port.
        pytest.param("http://ha.local:65535", id="port-65535"),
        # Port 0 is technically valid in a URL (means "any port" in some APIs);
        # urlparse accepts it and there's no reason to reject.
        pytest.param("http://ha.local:0", id="port-0"),
    ],
)
def test_validate_url_accepts(setup_env_mod, url):
    ok, _ = setup_env_mod._validate_url(url)
    assert ok is True


@pytest.mark.parametrize(
    "url,needles",
    [
        pytest.param("http://ha.local#frag", ("fragment",), id="fragment"),
        pytest.param("http://ha.local?x=1", ("query",), id="query"),
        # The `;params` segment in a URL path is rare but parseable.
        pytest.param("http://ha.local/path;p=1", (), id="params"),
        pytest.param("file:///etc/shadow", ("http",), id="file-scheme"),
        pytest.param("gopher://x/", (), id="gopher-scheme"),
        pytest.param("ha.local:8123", (), id="missing-scheme"),
        pytest.param("http://", ("host",), id="missing-host"),
        # user:pass@ in URL leaks creds into access logs + double-auths the
        # request (one via the wizard's Authorization header).
        pytest.param("http://user:pass@ha.local", ("credentials",), id="userinfo"),
        pytest.param("http://user@ha.local", (), id="username-only"),
        pytest.param("http://ha.local  ", ("whitespace",), id="trailing-whitespace"),
        pytest.param("  http://ha.local", (), id="leading-whitespace"),
        pytest.param("http://ha.local\nX-Evil: 1", ("control",), id="control-chars"),
        # Empty-query bypass — `urlparse("http://x?").query == ""` would slide
        # through the `p.query` check, then the appended `/api/` lands inside
        # the query string. Same false-positive class as the fragment trick.
        # (Hacker re-attack PARTIAL finding on PR #20.)
        pytest.param("http://example.com?", ("fragment", "query"), id="trailing-question-mark"),
        pytest.param("http://example.com/path?", (), id="trailing-question-mark-path"),
        # `http://x:99999` — urlparse raises ValueError when `.port` is
        # accessed. Without the explicit check, this leaks as an
        # ExceptionGroup at connect-time → uncaught 500 in the route.
        # (Hacker re-attack NEW-1 finding on PR #20.)
        pytest.param("http://example.com:99999", ("port",), id="port-above-range"),
        pytest.param("http://example.com:1234567", ("port",), id="port-far-above-range"),
    ],
)
def test_validate_url_rejects(setup_env_mod, url, needles):
    ok, why = setup_env_mod._validate_url(url)
    assert ok is False
    if needles:  # the reason names the problem (any one of these words)
        assert any(n in why.lower() for n in needles), why
