"""Guard: personal content must never reach the logs (Loki keeps 30 days and the
nightly self-audit sends warnings to an LLM and a Pushover digest). Log
lengths, ids or categories instead.

Static check over the orchestrator package. A logging call fails when any of
its arguments (positional or keyword) contains, outside len()/type()/bool():

* a name or attribute that usually carries user content (SENSITIVE), or whose
  name ends in one of SENSITIVE_SUFFIXES;
* a dict lookup with such a key, e.g. ``quest["name"]``;
* a slice such as ``x[:80]`` — truncating before logging is the classic sign
  of logging content.

Audited exceptions (e.g. an upstream server's own error body) carry a
``# log-safe: <reason>`` comment on the call or the line above it, so every
exception is a conscious, reviewable decision.
"""

import ast
import pathlib

import pytest

PKG = pathlib.Path(__file__).resolve().parent.parent

LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical"}
LOGGER_NAMES = {"logger", "log", "_logger", "_log", "logging"}

SENSITIVE = {
    # messages / speech
    "user_text",
    "user_message",
    "user_msg",
    "question",
    "transcript",
    "utterance",
    "reply",
    "answer",
    "text",
    "content",
    "message",
    "msg",
    "prompt",
    # searches
    "query",
    "full_query",
    "original_query",
    # personal records
    "title",
    "subject",
    "summary",
    "description",
    "desc",
    "notes",
    "note",
    "detail",
    "name",
    "med_name",
    "nudge",
    "raw_text",
    "task",
    "task_text",
    "reminder_text",
    "tags",
    "destination",
    "ev",
    "preview",
    "arguments",
    "target_text",
    "quest",
    "fact",
    "facts",
    "snippet",
    "caption",
    "filename",
    "data",
    # calculate: the program carries the user's figures and model-chosen names
    # (which can be medication names); med_tokens are words of med names
    "program",
    "expression",
    "med_tokens",
}
SENSITIVE_SUFFIXES = ("_text", "_query", "_title", "_message", "_content", "_desc")

# `<result>.message` is a service/integration status string (HA, Pi-hole),
# not user content; `self.<attr>` is the object's own config (instance names).
ATTR_EXEMPT = {"message"}

# Attribute reads that are identifiers/numbers no matter which object they are on.
SAFE_ATTRS = {"id", "status_code", "colno", "lineno", "pos"}

SAFE_WRAPPERS = {"len", "type", "bool", "id", "sorted", "isinstance", "content_shape"}
PRAGMA = "log-safe:"


def _is_sensitive_name(name: str) -> bool:
    return name in SENSITIVE or name.endswith(SENSITIVE_SUFFIXES)


def _violations(node: ast.AST) -> list[str]:
    """What in `node` would put content in a log line (ignoring len() etc.)."""
    found: list[str] = []

    def visit(n: ast.AST) -> None:
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in SAFE_WRAPPERS:
            return
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "keys":
            return  # dict keys (e.g. sorted(data.keys())) are field names, not content
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "get"
            and n.args
            and isinstance(n.args[0], ast.Constant)
            and isinstance(n.args[0].value, str)
            and _is_sensitive_name(n.args[0].value)
        ):
            found.append(f".get({n.args[0].value!r})")
        if isinstance(n, ast.Attribute) and n.attr in SAFE_ATTRS:
            return  # msg.id, resp.status_code: identifiers, whatever the object
        if isinstance(n, ast.Name) and _is_sensitive_name(n.id):
            found.append(n.id)
        elif (
            isinstance(n, ast.Attribute)
            and _is_sensitive_name(n.attr)
            and n.attr not in ATTR_EXEMPT
            and not (isinstance(n.value, ast.Name) and n.value.id == "self")
        ):
            found.append(n.attr)
        elif isinstance(n, ast.Subscript):
            sl = n.slice
            if isinstance(sl, ast.Slice):
                found.append("slice[:]")
            elif isinstance(sl, ast.Constant) and isinstance(sl.value, str) and _is_sensitive_name(sl.value):
                found.append(f"[{sl.value!r}]")
        for child in ast.iter_child_nodes(n):
            visit(child)

    visit(node)
    return found


def _is_log_call(node: ast.AST) -> bool:
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    if node.func.attr not in LOG_METHODS:
        return False
    recv = node.func.value
    if isinstance(recv, ast.Name):
        return recv.id in LOGGER_NAMES
    if isinstance(recv, ast.Call) and isinstance(recv.func, ast.Attribute) and recv.func.attr == "getLogger":
        return True  # logging.getLogger(__name__).info(...)
    return isinstance(recv, ast.Attribute) and recv.attr in LOGGER_NAMES  # self.logger


def _offending_log_calls() -> list[str]:
    hits = []
    for path in sorted(PKG.rglob("*.py")):
        if "tests" in path.parts:
            continue
        source = path.read_text()
        lines = source.splitlines()
        for node in ast.walk(ast.parse(source, filename=str(path))):
            if not _is_log_call(node):
                continue
            start, end = node.lineno, getattr(node, "end_lineno", node.lineno)
            window = lines[max(start - 2, 0) : end]
            if any(PRAGMA in line for line in window):
                continue
            args = list(node.args) + [kw.value for kw in node.keywords if kw.arg not in ("exc_info", "stack_info")]
            refs = [r for a in args for r in _violations(a)]
            if refs:
                hits.append(f"{path.relative_to(PKG.parent)}:{start} logs {sorted(set(refs))}")
    return hits


def test_no_user_content_in_log_calls():
    hits = _offending_log_calls()
    assert not hits, (
        "Log lengths/ids/categories instead of user content (or mark an audited, "
        "non-personal exception with '# log-safe: <reason>'):\n" + "\n".join(hits)
    )


def _first_call_violations(src: str) -> list[str]:
    call = ast.parse(src).body[0].value
    return [r for a in list(call.args) + [k.value for k in call.keywords] for r in _violations(a)]


def test_guard_flags_content():
    assert _first_call_violations('logger.info("q: %s", query)') == ["query"]
    assert _first_call_violations('logger.info("q: %s", body[:50])') == ["slice[:]"]
    assert _first_call_violations('logger.info("q: %s", rec["name"])') == ["['name']"]
    assert _first_call_violations('logger.info(f"set {reminder_text}")') == ["reminder_text"]
    assert _first_call_violations('logger.info("x", extra={"t": user_text})') == ["user_text"]
    assert _first_call_violations('logger.info("m %s", med.get("name"))') == [".get('name')"]


def test_guard_allows_lengths_and_keys():
    assert _first_call_violations('logger.info("q: %d", len(query))') == []
    assert _first_call_violations('logger.info("keys %s", sorted(data.keys()))') == []
    assert _first_call_violations('logger.info("id %s", reminder_id)') == []
    assert _first_call_violations('logger.info("msg %s", msg.id)') == []


def test_access_log_query_string_stripped():
    import logging

    from orchestrator.log_config import StripQueryStringFilter

    rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:5000", "GET", "/api/documents?search=private+words", "1.1", 200),
        None,
    )
    assert StripQueryStringFilter().filter(rec) is True
    assert "private" not in rec.getMessage()
    assert "/api/documents?…" in rec.getMessage()


@pytest.mark.parametrize(
    "full_path",
    [
        # uvicorn logs quote(scope["path"]): a "?" the client sent as %3F
        # arrives decoded and is re-quoted, with the text still after it
        "/api/documents%3Fsearch%3Dprivate%20words",
        "/api/documents%3fsearch=private",
        "/api/documents%253Fsearch%253Dprivate",
        "/api/documents%25253fsearch=private",
        "/api/documents%3Fa?search=private",
        "/api/documents?a%3Fsearch=private",
    ],
)
def test_access_log_encoded_query_mark_stripped(full_path):
    import logging

    from orchestrator.log_config import StripQueryStringFilter

    rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:5000", "GET", full_path, "1.1", 404),
        None,
    )
    assert StripQueryStringFilter().filter(rec) is True
    assert "private" not in rec.getMessage()
    assert "/api/documents?…" in rec.getMessage()


@pytest.mark.parametrize(
    "target",
    [
        "/api/documents?search=private",
        "/api/documents%3Fsearch=private",
        "/api/documents%3fsearch%3dprivate",
        "/api/documents%253Fsearch=private",
        "/api/documents%253fsearch=private",
        "/api/documents%25253Fsearch=private",
        "/api/documents%25%33fsearch=private",  # decodes to "%3f", logged "%253f"
    ],
)
def test_access_log_strips_query_from_uvicorns_own_rendering(target):
    """The strings above, but produced by the installed uvicorn: its HTTP
    protocols set scope["path"] = unquote(<target path>) and log
    get_path_with_query_string(scope) (quote(path) + "?" + query)."""
    import logging
    from urllib.parse import unquote

    uvicorn_utils = pytest.importorskip("uvicorn.protocols.utils")
    from orchestrator.log_config import StripQueryStringFilter

    raw_path, _, query = target.partition("?")
    scope = {"type": "http", "path": unquote(raw_path), "query_string": query.encode("ascii"), "root_path": ""}
    args = ("10.0.0.1:5000", "GET", uvicorn_utils.get_path_with_query_string(scope), "1.1", 404)
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', args, None)
    assert StripQueryStringFilter().filter(rec) is True
    assert "private" not in rec.getMessage()
    assert "/api/documents?…" in rec.getMessage()


@pytest.mark.parametrize("full_path", ["/api/documents", "/api/audio/3f9a1c2e.wav", "/api/docs%2F3"])
def test_access_log_path_without_query_untouched(full_path):
    import logging

    from orchestrator.log_config import StripQueryStringFilter

    args = ("10.0.0.1:5000", "GET", full_path, "1.1", 200)
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', args, None)
    assert StripQueryStringFilter().filter(rec) is True
    assert rec.args == args


def test_configure_logging_attaches_access_filter_once():
    import logging

    from orchestrator.log_config import StripQueryStringFilter, configure_logging

    configure_logging("INFO")
    configure_logging("INFO")
    filters = [f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, StripQueryStringFilter)]
    assert len(filters) == 1


def test_content_shape_never_returns_content():
    from orchestrator.log_config import content_shape

    secret = "take two of the blue pills"
    for sample in (secret, "```json\n" + secret, "<think>" + secret, "{" + secret, "  ", None, 5):
        assert secret not in content_shape(sample)
    assert content_shape("```x") == "code-fence"
    assert content_shape("[1,2") == "json-like"
