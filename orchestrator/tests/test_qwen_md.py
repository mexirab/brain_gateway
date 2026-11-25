"""Keeps QWEN.md, the compact Qwen Code briefing at the repo root, honest.

Qwen Code loads QWEN.md into every request it sends the brain, whose window is
64K tokens (CLAUDE.md alone is ~37K, which is why QWEN.md exists). So:

- it has a size budget (MAX_CHARS, ~3K tokens);
- every repo path it names (inline code, or a word in a fenced command) must
  exist: a stale path sends the model hunting for a file that's gone. Paths
  are repo-relative; fenced commands are assumed to run from the repo root;
- every code name it puts in backticks (snake_case or UPPER_SNAKE with an
  underscore, e.g. a function or constant) must still appear in orchestrator/,
  and a quoted `NAME = settings.field` alias must still be a line of shared.py;
- the numbers it quotes (test-id limit, tool-result cap) match the code;
- the CLAUDE.md sections it points to still exist under that name, and the
  index grep it gives for CLAUDE.md prints a small index, not most of
  CLAUDE.md (its topic notes are single lines of up to ~8K chars);
- it has no `@path` import directive in prose: Qwen Code inlines the file an
  `@path` names into every request (a committed symlink could pull in a file
  from outside the repo);
- its safety lines (never commit/push, no installs, no network tools, no
  secrets, the log and prompt rules) are still there after a trim for size.

The path extractor skips globs, braces, placeholders (`test_<feature>.py`),
absolute or home paths (box paths, URLs), flags and anything with spaces.
The extractors pair single backticks within a line, so QWEN.md must keep each
inline code span on one line, use single backticks and close its fences (a
check enforces this; otherwise that line's paths, names and @imports would go
unchecked).

Each check also has a negative case: the real QWEN.md (or CLAUDE.md) plus one
mutation must make it fail (test_qwen_md_check_fails_on_broken_doc).

Needs a full checkout. GitHub CI runs it from one, and there a missing
CLAUDE.md fails the checks instead of skipping them. The documented local
runner mounts only orchestrator/, scripts/ and data/, so there the QWEN.md
checks skip; the extractor's own tests still run.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_QWEN = _REPO / "QWEN.md"
_CLAUDE = _REPO / "CLAUDE.md"
_ORCH = _REPO / "orchestrator"
_SHARED = _ORCH / "shared.py"

MAX_CHARS = 10_000

# The command QWEN.md gives for listing CLAUDE.md's headings and note titles,
# and a Python twin of it. `-o` matters: without it the grep prints whole
# notes (~98K chars on 2026-11-21) and Qwen Code truncates tool output at 25K.
_INDEX_GREP = r"grep -no '^## .*\|^- \*\*[^*]*\*\*' CLAUDE.md"
_INDEX_PATTERN = re.compile(r"^## .*|^- \*\*[^*]*\*\*")
MAX_INDEX_CHARS = 6_000

# CLAUDE.md sections QWEN.md may point to by name, besides any it names as
# `CLAUDE.md's "X"`. A pointer QWEN.md uses must still be a `## ` heading in
# CLAUDE.md (prefix match: headings carry suffixes like "(MANDATORY)").
_CLAUDE_SECTIONS = (
    "Common Commands",
    "Detailed Docs (router)",
    "Post-change review workflow",
)
_CLAUDE_POINTER = re.compile(r"CLAUDE\.md's \"([^\"]+)\"")

# (source file, constant name, how QWEN.md states its value)
_FACTS = (
    ("orchestrator/tests/conftest.py", "MAX_NODEID_CHARS", "≤{} chars"),
    ("orchestrator/unified_loop.py", "MAX_TOOL_RESULT_CHARS", "capped at {} chars"),
)

# Safety lines a trim for size must not drop. Matched case-insensitively after
# collapsing whitespace. Reword one only on purpose, and update it here.
_SAFETY_PHRASES = (
    "never run `git commit`, `git push`",
    "never run `pip install`",
    "no curl, no `docker`, no ssh",
    "don't read secrets",
    "`# log-safe: <reason>`",
    "never add a second system message",
    "no personal data anywhere",
)

_full_checkout = pytest.mark.skipif(
    not _CLAUDE.is_file() and not os.environ.get("GITHUB_ACTIONS"),
    reason="needs a full repo checkout (CLAUDE.md / QWEN.md not mounted)",
)

_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*\n(.*?)^[ \t]*\1[ \t]*$", re.M | re.S)
_SPAN = re.compile(r"`([^`\n]+)`")
_PATH_CHARS = re.compile(r"^[A-Za-z0-9_./-]+$")
_LINE_SUFFIX = re.compile(r":\d+(-\d+)?$")
_FILE_EXT = re.compile(r"\.(py|md|yml|yaml|toml|json|ts|tsx|js|sh|service|txt|html|css)$")
_CODE_NAME = re.compile(r"^@?(_?[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*)(?:\(\))?$")
_ALIAS_SPAN = re.compile(r"^[A-Z][A-Z0-9_]* = settings\.[a-z][a-z0-9_]*$")
_IMPORT_DIRECTIVE = re.compile(r"(?:^|\s)(@[A-Za-z0-9./~_-]+)")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _split_fences(text: str) -> tuple[str, list[str]]:
    """Return (text with fenced blocks removed, the fenced blocks' bodies)."""
    return _FENCE.sub("", text), [m.group(2) for m in _FENCE.finditer(text)]


def _prose_spans(text: str) -> list[str]:
    prose, _ = _split_fences(text)
    return [span.strip() for span in _SPAN.findall(prose)]


def _as_repo_path(token: str) -> str | None:
    token = _LINE_SUFFIX.sub("", token.strip().strip("'\""))
    if not token or not _PATH_CHARS.match(token):
        return None  # commands, globs, braces, placeholders, URLs
    if token.startswith(("/", "-")):
        return None  # absolute (box/container) paths, flags
    if "/" in token or _FILE_EXT.search(token):
        return token
    return None


def repo_paths(text: str) -> list[str]:
    """Repo-relative paths QWEN.md names: inline code spans plus fenced-command words."""
    _, fences = _split_fences(text)
    candidates = _prose_spans(text)
    for body in fences:
        candidates.extend(body.split())
    found = []
    for candidate in candidates:
        path = _as_repo_path(candidate)
        if path and path not in found:
            found.append(path)
    return found


def code_names(text: str) -> list[str]:
    """Backticked snake_case / UPPER_SNAKE names (with an underscore) in QWEN.md prose."""
    names = []
    for span in _prose_spans(text):
        match = _CODE_NAME.match(span)
        if match and "_" in match.group(1) and match.group(1) not in names:
            names.append(match.group(1))
    return names


def alias_spans(text: str) -> list[str]:
    """Backticked `NAME = settings.field` examples in QWEN.md prose."""
    return [span for span in _prose_spans(text) if _ALIAS_SPAN.match(span)]


def import_directives(text: str) -> list[str]:
    """`@path` tokens in prose (outside fenced blocks and inline code), which Qwen Code would import."""
    prose, _ = _split_fences(text)
    return _IMPORT_DIRECTIVE.findall(_SPAN.sub("", prose))


def claude_index(claude_text: str) -> list[str]:
    """What `_INDEX_GREP` prints: `N:match` for each heading / note title."""
    out = []
    for number, line in enumerate(claude_text.splitlines(), 1):
        match = _INDEX_PATTERN.match(line)
        if match:
            out.append(f"{number}:{match.group(0)}")
    return out


def inline_code_problems(text: str) -> list[str]:
    """Prose lines the backtick extractors would misread: they pair single backticks within a line."""
    prose = _FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), text)  # keeps line numbers
    problems = []
    for number, line in enumerate(prose.splitlines(), 1):
        if re.match(r"[ \t]*(`{3,}|~{3,})", line):
            problems.append(f"line {number}: unclosed code fence")
        elif "``" in line or "\\`" in line:
            problems.append(f"line {number}: double or escaped backtick")
        elif line.count("`") % 2:
            problems.append(f"line {number}: odd number of backticks")
    return problems


# --- extractor self-tests (run everywhere) ------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("orchestrator/config.py", "orchestrator/config.py"),
        ("frontend/", "frontend/"),
        ("CLAUDE.md", "CLAUDE.md"),
        ("orchestrator/orchestrator.py:136", "orchestrator/orchestrator.py"),
        ("orchestrator/{config.py, shared.py}", None),
        ("orchestrator/*.py", None),
        ("test_<feature>.py", None),
        ("/app/data", None),
        ("~/.qwen/QWEN.md", None),
        ("https://example.com/x.md", None),
        ("-p", None),
        (".env", None),
        (".github/workflows/ci.yml", ".github/workflows/ci.yml"),
        ("DYNAMIC_CONTEXT_MARKER", None),
        ("git commit", None),
        ("os.environ", None),
    ],
    ids=lambda v: str(v)[:40],
)
def test_as_repo_path(token, expected):
    assert _as_repo_path(token) == expected


def test_repo_paths_reads_spans_and_fenced_words():
    text = (
        "See `orchestrator/a.py` and `b/*.py`.\n"
        "```bash\n"
        "python -m pytest -p no:cacheprovider orchestrator/tests/test_x.py -k 'y'\n"
        "```\n"
        "1. Step:\n"
        "   ~~~\n"
        "   cat docs/z.md\n"
        "   ~~~\n"
        "Then `CLAUDE.md`.\n"
    )
    assert repo_paths(text) == [
        "orchestrator/a.py",
        "CLAUDE.md",
        "orchestrator/tests/test_x.py",
        "docs/z.md",
    ]


def test_code_names_picks_identifiers_only():
    text = "`run_loop`, `@register_tool`, `get_all_tools()`, `MAX_X`, `_cap_it`, `Settings`, `bgw_*`, `a b_c`\n"
    assert code_names(text) == ["run_loop", "register_tool", "get_all_tools", "MAX_X", "_cap_it"]


def test_alias_spans():
    text = 'e.g. `FOO_ENABLED = settings.foo_enabled`, not `x = 1` or `asyncio_mode = "auto"`\n'
    assert alias_spans(text) == ["FOO_ENABLED = settings.foo_enabled"]


def test_import_directives_found_only_in_prose():
    text = (
        "Read @docs/x.md first. Mail a@b.c is fine. `@register_tool` is code.\n"
        "@../../.env\n"
        "```bash\n"
        "echo @not-prose\n"
        "```\n"
    )
    assert import_directives(text) == ["@docs/x.md", "@../../.env"]


def test_claude_index_prints_titles_not_notes():
    text = "# Title\n## Setup (short)\n- **Topic one (2026):** a very long note body\n- plain bullet\n"
    assert claude_index(text) == ["2:## Setup (short)", "3:- **Topic one (2026):**"]


_INDEX_SAMPLE = (
    "# Title\n"
    "## Setup (short)\n"
    "- **Topic one (2026):** body with **bold** and `code`\n"
    "- plain bullet with **bold**\n"
    "  - **indented** sub-bullet\n"
    "text ## not a heading\n"
    "##no space\n"
    "- **unclosed bold\n"
    "## Ünïcode ✓ heading\n"
)


@pytest.mark.skipif(shutil.which("grep") is None, reason="needs grep")
@pytest.mark.parametrize("source", ["sample", pytest.param("claude-md", marks=_full_checkout)])
def test_claude_index_matches_grep(source, tmp_path):
    """claude_index() prints exactly what the real `_INDEX_GREP` prints (the size budget relies on it)."""
    if source == "sample":
        (tmp_path / "CLAUDE.md").write_text(_INDEX_SAMPLE, encoding="utf-8")
        where = tmp_path
    else:
        where = _REPO
    run = subprocess.run(["sh", "-c", _INDEX_GREP], cwd=where, capture_output=True, check=False)
    assert run.returncode == 0, run.stderr.decode("utf-8", errors="replace")
    printed = run.stdout.decode("utf-8").rstrip("\n").split("\n")  # split("\n"), not splitlines(): keep any \r
    assert printed == claude_index((where / "CLAUDE.md").read_text(encoding="utf-8"))


def test_inline_code_problems():
    text = (
        "Fine: `a` and `b_c`.\n"
        "```bash\n"
        "x ``y`` `z\n"
        "```\n"
        "A `wrapped\n"
        "span` here.\n"
        "Double ``x`` span.\n"
        "Escaped \\` tick \\`.\n"
        "~~~\n"
        "cat docs/z.md\n"
    )
    assert inline_code_problems(text) == [
        "line 5: odd number of backticks",
        "line 6: odd number of backticks",
        "line 7: double or escaped backtick",
        "line 8: double or escaped backtick",
        "line 9: unclosed code fence",
    ]


# --- QWEN.md checks (full checkout only) ----------------------------------------


def _qwen_text() -> str:
    assert _QWEN.is_file(), "QWEN.md is missing from the repo root (Qwen Code's briefing)"
    return _QWEN.read_text(encoding="utf-8")


def _claude_text() -> str:
    assert _CLAUDE.is_file(), "CLAUDE.md is missing from the repo root"
    return _CLAUDE.read_text(encoding="utf-8")


@_full_checkout
def test_qwen_md_within_size_budget():
    size = len(_qwen_text())
    assert size <= MAX_CHARS, (
        f"QWEN.md is {size} chars, over its {MAX_CHARS}-char budget. It goes into every "
        "Qwen Code request on a 64K brain: move detail into CLAUDE.md or docs/ and point to it."
    )


@_full_checkout
def test_qwen_md_paths_exist():
    paths = repo_paths(_qwen_text())
    # Guard the extractor: QWEN.md lists ~15 load-bearing files.
    assert len(paths) >= 15, f"only {len(paths)} repo paths found in QWEN.md; extractor broken?"
    bad = []
    for path in paths:
        if ".." in path.split("/"):
            bad.append(f"{path} (not repo-relative)")
        elif not (_REPO / path).exists() or (path.endswith("/") and not (_REPO / path).is_dir()):
            bad.append(path)
    assert not bad, "QWEN.md names paths that don't exist in the repo:\n  " + "\n  ".join(bad)


@_full_checkout
def test_qwen_md_code_names_exist():
    names = code_names(_qwen_text())
    sources = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(_ORCH.rglob("*.py"))
        if p.name != "test_qwen_md.py"
    )
    missing = [n for n in names if not re.search(rf"(?<![\w]){re.escape(n)}\b", sources)]
    assert not missing, (
        "QWEN.md names code that no longer appears anywhere in orchestrator/: "
        + ", ".join(missing)
        + ". Update the name, or drop the backticks if it isn't code."
    )


@_full_checkout
def test_qwen_md_alias_examples_exist():
    shared_lines = {line.strip() for line in _SHARED.read_text(encoding="utf-8").splitlines()}
    stale = [a for a in alias_spans(_qwen_text()) if a not in shared_lines]
    assert not stale, f"QWEN.md quotes alias line(s) that orchestrator/shared.py no longer has: {stale}"


@_full_checkout
@pytest.mark.parametrize(("source", "name", "stated"), _FACTS, ids=[f[1] for f in _FACTS])
def test_qwen_md_numbers_match_code(source, name, stated):
    pattern = rf"^{name}(?::\s*int)?\s*=\s*([\d_]+)\b"
    match = re.search(pattern, (_REPO / source).read_text(encoding="utf-8"), re.M)
    assert match, f"{name} = <number> not found in {source}; update _FACTS in this test"
    expected = stated.format(int(match.group(1).replace("_", "")))
    assert expected in _flat(_qwen_text()), f"QWEN.md should say {expected!r} (from {source})"


@_full_checkout
def test_qwen_md_claude_pointers_exist():
    text = _flat(_qwen_text())
    headings = [line[3:].strip() for line in _claude_text().splitlines() if line.startswith("## ")]
    pointers = sorted(set(_CLAUDE_SECTIONS) | set(_CLAUDE_POINTER.findall(text)))
    stale = [s for s in pointers if s in text and not any(h.startswith(s) for h in headings)]
    assert not stale, f"QWEN.md points to CLAUDE.md sections that were renamed or removed: {stale}"


@_full_checkout
def test_claude_md_index_grep_stays_small():
    assert _INDEX_GREP in _qwen_text(), (
        "QWEN.md no longer gives the CLAUDE.md index grep this test models. If you changed it on "
        "purpose, update _INDEX_GREP and _INDEX_PATTERN; keep `-o`, CLAUDE.md notes are single long lines."
    )
    index = claude_index(_claude_text())
    notes = [line for line in index if ":- **" in line]
    assert len(notes) >= 10, f"the index found only {len(notes)} '- **Topic' notes in CLAUDE.md"
    size = sum(len(line) + 1 for line in index)
    assert size <= MAX_INDEX_CHARS, (
        f"QWEN.md's CLAUDE.md index grep now prints {size} chars (budget {MAX_INDEX_CHARS}). "
        "Shorten CLAUDE.md note titles (the bold part), or fold old notes into docs/."
    )


@_full_checkout
def test_qwen_md_has_no_import_directives():
    found = import_directives(_qwen_text())
    assert not found, (
        f"QWEN.md has @path import directive(s) {found}: Qwen Code would inline that file into every "
        "request. Put the path in backticks, or drop the @."
    )


@_full_checkout
def test_qwen_md_keeps_safety_lines():
    text = _flat(_qwen_text()).lower()
    missing = [p for p in _SAFETY_PHRASES if p.lower() not in text]
    assert not missing, (
        f"QWEN.md lost safety line(s) {missing}. Put them back; if one was reworded on purpose, "
        "update _SAFETY_PHRASES in this test."
    )


@_full_checkout
def test_qwen_md_inline_code_parses():
    problems = inline_code_problems(_qwen_text())
    assert not problems, (
        "QWEN.md has inline code these checks can't pair, so paths, code names and @imports on those "
        "lines go unchecked:\n  "
        + "\n  ".join(problems)
        + "\nKeep each `span` on one line, use single backticks, and close every code fence."
    )


# --- each QWEN.md check fails on a broken doc (full checkout only) -------------


def _append(extra: str):
    return lambda text: f"{text}\n{extra}\n"


def _sub(pattern: str, repl: str, flags: int = 0):
    """Mutation: replace each regex match with the literal `repl`."""

    def mutate(text: str) -> str:
        new, count = re.subn(pattern, lambda _m: repl, text, flags=flags)
        assert count, f"mutation {pattern!r} no longer matches the doc; update this case"
        return new

    return mutate


def _loose(phrase: str) -> str:
    """Regex for `phrase` that allows any whitespace (a line break) between its words."""
    return r"\s+".join(re.escape(word) for word in phrase.split())


_DROPPED_SAFETY = "never run `pip install`"

# (doc to break, mutation, check that must fail, regex its message must match)
_BROKEN = [
    pytest.param("qwen", _append("x" * MAX_CHARS), test_qwen_md_within_size_budget, "char budget", id="size"),
    pytest.param(
        "qwen", _append("See `orchestrator/nope_qwen.py`."), test_qwen_md_paths_exist, r"nope_qwen\.py", id="path"
    ),
    pytest.param(
        "qwen", _append("```bash\ncat docs/nope_qwen.md\n```"), test_qwen_md_paths_exist, r"docs/nope", id="fenced"
    ),
    pytest.param("qwen", _append("See `../out/x.py`."), test_qwen_md_paths_exist, "not repo-relative", id="dotdot"),
    pytest.param(
        "qwen", _append("See `docker-compose.yml/`."), test_qwen_md_paths_exist, r"compose\.yml/", id="file-as-dir"
    ),
    pytest.param("qwen", _sub("`", ""), test_qwen_md_paths_exist, "extractor broken", id="no-paths"),
    pytest.param(
        "qwen",
        _append("Call `no_such_function_qwen_xyz()`."),
        test_qwen_md_code_names_exist,
        "no_such_function_qwen_xyz",
        id="code-name",
    ),
    pytest.param(
        "qwen", _sub(r"= settings\.", "= settings.renamed_"), test_qwen_md_alias_examples_exist, "renamed_", id="alias"
    ),
    *[
        pytest.param(
            "qwen",
            _sub(_loose(stated).replace(re.escape("{}"), r"\d+"), stated.format(0)),
            functools.partial(test_qwen_md_numbers_match_code, source, name, stated),
            re.escape(source),
            id=f"number-{name}",
        )
        for source, name, stated in _FACTS
    ],
    pytest.param(
        "qwen",
        _append('See CLAUDE.md\'s "No Such Section".'),
        test_qwen_md_claude_pointers_exist,
        "No Such Section",
        id="pointer",
    ),
    pytest.param(
        "claude",
        _sub(r"^## ", "## Renamed ", re.M),
        test_qwen_md_claude_pointers_exist,
        "renamed or removed",
        id="heading-renamed",
    ),
    pytest.param(
        "qwen",
        _sub(re.escape(_INDEX_GREP), _INDEX_GREP.replace("grep -no", "grep -n")),
        test_claude_md_index_grep_stays_small,
        "no longer gives",
        id="grep-no-o",
    ),
    pytest.param(
        "claude",
        _append("".join(f"- **Note title {i:03d} {'t' * 100}:** body\n" for i in range(MAX_INDEX_CHARS // 100))),
        test_claude_md_index_grep_stays_small,
        r"budget \d+",
        id="index-too-big",
    ),
    pytest.param(
        "claude",
        _sub(r"^- \*\*", "- ", re.M),
        test_claude_md_index_grep_stays_small,
        r"found only \d+",
        id="index-no-notes",
    ),
    pytest.param(
        "qwen", _append("Read @CLAUDE.md first."), test_qwen_md_has_no_import_directives, "@CLAUDE", id="import"
    ),
    pytest.param(
        "qwen",
        _sub(_loose(_DROPPED_SAFETY), "", re.I),
        test_qwen_md_keeps_safety_lines,
        re.escape(_DROPPED_SAFETY),
        id="safety",
    ),
    pytest.param(
        "qwen", _append("A wrapped `code\nspan`."), test_qwen_md_inline_code_parses, "odd number", id="backtick"
    ),
]


@_full_checkout
@pytest.mark.parametrize(("doc", "mutate", "check", "match"), _BROKEN)
def test_qwen_md_check_fails_on_broken_doc(doc, mutate, check, match, tmp_path, monkeypatch):
    assert _DROPPED_SAFETY in _SAFETY_PHRASES
    real = _QWEN if doc == "qwen" else _CLAUDE
    broken = tmp_path / real.name
    broken.write_text(mutate(real.read_text(encoding="utf-8")), encoding="utf-8")  # outside raises: a stale case fails
    # The checks read the module globals _QWEN / _CLAUDE at call time.
    monkeypatch.setitem(globals(), "_QWEN" if doc == "qwen" else "_CLAUDE", broken)
    with pytest.raises(AssertionError, match=match):
        check()
