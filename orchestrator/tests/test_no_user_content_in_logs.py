"""Guard: personal content must never reach the logs (Loki keeps 30 days and the
nightly self-audit scrapes warnings). Log lengths, ids or categories instead.

Static check over the orchestrator package: any `logger.<level>(...)` call whose
arguments reference one of the user-content variable names below — other than
as `len(...)` — fails. Medication names, dosages, messages, search queries,
reminder text and calendar titles all flow through these names.
"""

import ast
import pathlib

PKG = pathlib.Path(__file__).resolve().parent.parent

LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical"}

# Variable / attribute names that carry user-written or personal content.
SENSITIVE = {
    "user_text",
    "user_message",
    "question",
    "query",
    "full_query",
    "original_query",
    "title",
    "nudge",
    "med_name",
    "raw_text",
    "desc",
    "transcript",
    "utterance",
    "reply",
    "answer",
}

# Calls that reduce content to something non-identifying.
SAFE_WRAPPERS = {"len", "type", "bool", "id"}


def _sensitive_refs(node: ast.AST) -> list[str]:
    """Sensitive names used in `node`, ignoring anything inside len()/type()/…"""
    found: list[str] = []

    def visit(n: ast.AST) -> None:
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in SAFE_WRAPPERS:
            return
        if isinstance(n, ast.Name) and n.id in SENSITIVE:
            found.append(n.id)
        elif isinstance(n, ast.Attribute) and n.attr in SENSITIVE:
            found.append(n.attr)
        for child in ast.iter_child_nodes(n):
            visit(child)

    visit(node)
    return found


def _offending_log_calls() -> list[str]:
    hits = []
    for path in sorted(PKG.rglob("*.py")):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in LOG_METHODS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"logger", "log", "_logger"}
            ):
                continue
            refs = [r for arg in node.args for r in _sensitive_refs(arg)]
            if refs:
                hits.append(f"{path.relative_to(PKG.parent)}:{node.lineno} logs {sorted(set(refs))}")
    return hits


def test_no_user_content_in_log_calls():
    hits = _offending_log_calls()
    assert not hits, "Log lengths/ids instead of user content:\n" + "\n".join(hits)


def test_guard_catches_a_violation():
    tree = ast.parse('logger.info("q: %s", query[:50])')
    call = tree.body[0].value
    assert _sensitive_refs(call.args[1]) == ["query"]
    ok = ast.parse('logger.info("q: %d", len(query))').body[0].value
    assert _sensitive_refs(ok.args[1]) == []
