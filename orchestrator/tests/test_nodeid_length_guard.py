"""Tests for the test-id length guard in conftest.py.

CI runs ``pytest -v``, which prints every node id. Before pytest 9.2, a test
parametrized with a ~1 MB string and no ``ids=`` got the whole string as its
id, and two such tests stalled the GitHub test job for 25+ minutes. pytest 9.2
replaces an auto-generated str/bytes part over 100 chars with ``argname+index``
by default, but explicit ids, ``ids=`` callables and multi-argument ids joined
with ``-`` still have no length limit. conftest.py's
``pytest_collection_modifyitems`` now raises ``pytest.UsageError`` with a short
report (``long_nodeid_report``) when any id is over ``MAX_NODEID_CHARS``.

The property that matters most: the report and pytest's output never contain
the oversized id itself, only the test function, a count and a length.

The end-to-end tests run a real (in-process) pytest session on a temp project
via ``pytester``. ``pytest_plugins`` in a non-root conftest is an error on
pytest 8+, but in a test module it is still allowed, so the plugin is enabled
here only.

These tests must not leak a big id into the CI log themselves if the guard
ever regresses: checks against big strings assert plain booleans (pytest's
explanation of a failed ``x in text`` quotes ``text`` and is not truncated when
``CI`` is set), and pytester's echo of the inner session is drained from this
test's capture (a failing test prints its captured output in full).
"""

from types import SimpleNamespace

import pytest

from orchestrator.tests.conftest import MAX_NODEID_CHARS, long_nodeid_report, pytest_collection_modifyitems

pytest_plugins = ["pytester"]

_FILE = "orchestrator/tests/test_example.py"


def _nodeid_of_length(length: int, func: str = "test_case") -> str:
    """A parametrized node id of exactly ``length`` chars."""
    prefix = f"{_FILE}::{func}["
    filler = length - len(prefix) - 1
    assert filler > 0
    return prefix + "a" * filler + "]"


# ---------------------------------------------------------------------------
# long_nodeid_report
# ---------------------------------------------------------------------------


def test_no_report_for_short_or_no_ids():
    ids = [f"{_FILE}::test_a", f"{_FILE}::test_b[1-2]", f"{_FILE}::TestK::test_c[short-name]"]
    assert long_nodeid_report(ids) is None
    assert long_nodeid_report([]) is None


def test_limit_boundary():
    assert long_nodeid_report([_nodeid_of_length(MAX_NODEID_CHARS)]) is None
    assert long_nodeid_report([_nodeid_of_length(MAX_NODEID_CHARS + 1)]) is not None


def test_custom_limit_replaces_the_default():
    nodeid = "a.py::t[123456]"  # 15 chars
    assert long_nodeid_report([nodeid], limit=15) is None
    assert long_nodeid_report([nodeid], limit=14) is not None
    assert long_nodeid_report([_nodeid_of_length(MAX_NODEID_CHARS + 50)], limit=1_000) is None


@pytest.mark.parametrize("size", [5_000, 1_000_000], ids=["5k", "1mb"])
def test_report_never_contains_the_parameter(size):
    marker = "PAYLOADMARKER"
    payload = (marker * (size // len(marker) + 1))[:size]
    ids = [f"{_FILE}::test_payload[{payload}]", f"{_FILE}::test_payload[{payload}-again]"]
    report = long_nodeid_report(ids)
    assert report is not None
    leaked = marker in report
    size = len(report)
    assert not leaked
    # Small and bounded no matter how big the payload is.
    assert size < 600


@pytest.mark.parametrize(
    "nodeid,leak",
    [
        # A parameter that itself contains brackets must not leak into the base.
        pytest.param(f"{_FILE}::test_x[a[b]c" + "q" * 400 + "]", "q" * 10, id="brackets-in-parameter"),
        # No "[" at all (e.g. a huge generated function name): the base is the
        # whole id, so it must be truncated, not printed in full.
        pytest.param(f"{_FILE}::test_" + "y" * 1_000, "y" * 300, id="no-bracket"),
        pytest.param(
            "orchestrator/tests/" + "d" * 300 + "/test_deep.py::test_x[" + "z" * 50 + "]", "z" * 10, id="long-path"
        ),
    ],
)
def test_report_base_is_cut_before_the_parameter_and_at_200_chars(nodeid, leak):
    report = long_nodeid_report([nodeid])
    assert report is not None
    leaked = leak in report
    assert not leaked
    assert len(report.splitlines()[1]) < 260  # base <= 200 chars + count and length


# ---------------------------------------------------------------------------
# pytest_collection_modifyitems
# ---------------------------------------------------------------------------


def test_hook_leaves_short_items_alone():
    items = [SimpleNamespace(nodeid=f"{_FILE}::test_a"), SimpleNamespace(nodeid=f"{_FILE}::test_b[x]")]
    before = list(items)
    assert pytest_collection_modifyitems(config=None, items=items) is None
    assert items == before  # nothing deselected or reordered


def test_hook_raises_usage_error_without_the_payload():
    payload = "HOOKPAYLOAD" * 1_000
    items = [SimpleNamespace(nodeid=f"{_FILE}::test_a"), SimpleNamespace(nodeid=f"{_FILE}::test_big[{payload}]")]
    with pytest.raises(pytest.UsageError) as excinfo:
        pytest_collection_modifyitems(config=None, items=items)
    message = str(excinfo.value)
    leaked = "HOOKPAYLOAD" in message
    assert not leaked
    assert f"{_FILE}::test_big: 1 id(s)" in message
    assert items == []  # emptied, so --collect-only has nothing left to print


def test_guard_hook_is_registered_in_this_session(pytestconfig):
    """The guard is wired in: a registered conftest implements the hook and it fires."""
    impls = [
        impl
        for impl in pytestconfig.hook.pytest_collection_modifyitems.get_hookimpls()
        if getattr(impl.plugin, "long_nodeid_report", None) is not None
    ]
    assert impls, "conftest.py's pytest_collection_modifyitems is not registered"
    with pytest.raises(pytest.UsageError):
        impls[0].function(config=pytestconfig, items=[SimpleNamespace(nodeid=_nodeid_of_length(1_000))])


# ---------------------------------------------------------------------------
# End to end: a real pytest session on a temp project
# ---------------------------------------------------------------------------

# Uses the real hook object from the repo conftest (already imported in this
# process, so no path setup is needed for the in-process run).
_INNER_CONFTEST = "from orchestrator.tests.conftest import pytest_collection_modifyitems  # noqa: F401\n"

_PAYLOAD_SNIPPET = "zq7zq7zq7zq7zq7zq7"

# An ids= callable that returns the value: its result is used as-is on every
# pytest version (9.2+ only shortens auto-generated str ids).
_INNER_TEST_LONG_IDS = """
import pytest

PAYLOAD = "zq7" * 350_000  # ~1 MB

@pytest.mark.parametrize("value", [PAYLOAD, "short"], ids=lambda value: value)
def test_takes_a_payload(value):
    assert value
"""

# No ids= at all: four auto-generated parts, each under pytest 9.2's 100-char
# cutoff, joined with "-" into one id over the limit.
_INNER_TEST_JOINED_IDS = """
import pytest

@pytest.mark.parametrize("a,b,c,d", [("a" * 90, "b" * 90, "c" * 90, "d" * 90)])
def test_four_args(a, b, c, d):
    assert a
"""

_INNER_TEST_WITH_IDS = """
import pytest

PAYLOAD = "zq7" * 350_000  # ~1 MB

@pytest.mark.parametrize("value", [pytest.param(PAYLOAD, id="big-payload"), "short"])
def test_takes_a_payload(value):
    assert value
"""

# Keep the inner session minimal: no cache dir, no asyncio plugin.
_INNER_ARGS = ("-p", "no:cacheprovider", "-p", "no:asyncio")


def _run_inner(pytester, capsys, *args):
    """Run pytest in-process on the temp project; return (result, combined output).

    Drains pytester's echo of the inner output from this test's capture so a
    failure here can't print a 1 MB id.
    """
    result = pytester.runpytest(*args, *_INNER_ARGS)
    capsys.readouterr()
    return result, "\n".join(result.outlines + result.errlines)


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param(("-v",), id="verbose-run"),
        pytest.param((), id="default-run"),
        # pytest prints session.items from a finally under --collect-only, so
        # the hook must empty the list before it raises.
        pytest.param(("--collect-only", "-q"), id="collect-only"),
    ],
)
def test_megabyte_id_fails_collection_with_small_output(pytester, capsys, mode):
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(test_payload_ids=_INNER_TEST_LONG_IDS)

    result, output = _run_inner(pytester, capsys, *mode)

    size = len(output)
    leaked = _PAYLOAD_SNIPPET in output
    # Nothing ran or was listed, not even the short case.
    listed_or_ran = "[short]" in output or "PASSED" in output
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert size < 5_000
    assert not leaked
    assert not listed_or_ran
    assert f"1 test id(s) are longer than {MAX_NODEID_CHARS} chars" in output
    assert "test_payload_ids.py::test_takes_a_payload: 1 id(s), longest" in output


def test_joined_auto_ids_fail_collection(pytester, capsys):
    # Still reachable with pytest 9.2's default id strategy and no ids= at all.
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(test_joined_ids=_INNER_TEST_JOINED_IDS)

    result, output = _run_inner(pytester, capsys, "-v")

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "a" * 90 not in output
    assert "test_joined_ids.py::test_four_args: 1 id(s), longest" in output


def test_short_ids_let_the_session_run(pytester, capsys):
    # The fix the report recommends (pytest.param(..., id=...)) actually works.
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(test_payload_ids=_INNER_TEST_WITH_IDS)

    result, output = _run_inner(pytester, capsys, "-v")

    size = len(output)
    leaked = _PAYLOAD_SNIPPET in output
    assert result.ret == pytest.ExitCode.OK
    assert size < 5_000
    assert not leaked
    result.assert_outcomes(passed=2)
    assert "test_takes_a_payload[big-payload] PASSED" in output
