"""Coder box entrypoint (inside the container). scripts/jess-coder picks the mode.

Modes:
  selftest                 isolation self-test only (selftest.py)
  session [--continue] [--ask] [--headless]
                           self-test, then prepare /work and start Qwen Code
                           (interactive, or --headless: the instruction comes on
                           stdin). Refuses to start if the self-test fails.
  patch <base-sha>         print the workspace's changes against <base-sha> as a
                           text diff on stdout (run with /work read-only, no network)
  clean                    delete everything under /work (run with no network)

Layout of /work (the session folder on the host, mounted read-write):
  /work/repo          git clone of the bundle, on branch coder/<name>
  /work/.qwen-state   Qwen Code's chat history, so `--continue` works across boxes

The host never runs git in /work: `patch` hands the diff over as text, and the
box's files belong to the box uid, so host git refuses them as dubious ownership.
Stdlib only; runs with python3 -I.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

WORK = Path("/work")
REPO = WORK / "repo"
STATE = WORK / ".qwen-state"
BUNDLE = Path("/bundle/repo.bundle")
HOME = Path(os.environ.get("HOME", "/home/coder"))
SYSTEM_PROMPT = Path("/opt/jess-coder/system-prompt.md")
SELFTEST = "/opt/jess-coder/selftest.py"
SELFTEST_FAILED = 86  # scripts/jess-coder reads this exit code

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

# Qwen Code tools the box can't or shouldn't use (CODER_SPIKE.md section 2).
# Excluding only tool_search makes Qwen Code load every deferred tool up front,
# so the deferred ones are named too. Keeps read_file, write_file, edit, glob,
# grep_search, run_shell_command.
EXCLUDE_TOOLS = (
    "notebook_edit,skill,tool_search,tool_call,cron_create,cron_delete,cron_list,enter_worktree,"
    "exit_worktree,get_goal,update_goal,list_agents,loop_wakeup,monitor,read_mcp_resource,"
    "record_artifact,report_findings,send_message,task_stop,zoom_image,web_fetch,web_search,"
    "todo_write,save_memory,agent,task,"
    # Interactive-only extras: propose_goal's schema (maxLength 1500) is over the
    # gate's bound and the gate refuses the whole request; artifact and
    # display_image are useless offline. ask_user_question + plan mode stay.
    "propose_goal,artifact,display_image"
)

# Git must not run anything from the workspace's own config or attributes when
# the box hands its diff over (the model could have written them).
HEADLESS_WALL_TIME = "45m"

SAFE_GIT = [
    "git",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.pager=cat",
    "-c",
    "core.quotePath=true",
    "-c",
    "diff.noprefix=false",
    "-c",
    "diff.mnemonicPrefix=false",
    "-c",
    "diff.relative=false",
    "-c",
    "color.ui=false",
]


def die(msg: str, code: int = 2) -> NoReturn:
    print(f"jess-coder box: {msg}", file=sys.stderr)
    sys.exit(code)


def git(*args: str, cwd: Path = REPO, env: dict | None = None, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*SAFE_GIT, *args],
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )


def run_selftest() -> int:
    return subprocess.run([sys.executable, "-I", SELFTEST]).returncode


def prepare_workspace(name: str, base: str) -> None:
    if (REPO / ".git").is_dir():
        print(f"jess-coder box: resuming the workspace in {REPO}")
        return
    if not BUNDLE.is_file():
        die("no repo bundle mounted and no workspace yet")
    print("jess-coder box: unpacking the repo bundle (first start of this session)")
    branch = f"coder/{name}"
    git("init", "--quiet", "--initial-branch", branch, str(REPO), cwd=WORK)
    git("fetch", "--quiet", "--no-tags", str(BUNDLE), "main")
    git("cat-file", "-e", f"{base}^{{commit}}")
    git("reset", "--quiet", "--hard", base)
    git("config", "user.name", "Jess coder box")
    git("config", "user.email", "coder-box@jess.invalid")


def prepare_home() -> None:
    qwen = HOME / ".qwen"
    qwen.mkdir(mode=0o700, parents=True, exist_ok=True)
    (STATE / "projects").mkdir(mode=0o700, parents=True, exist_ok=True)
    link = qwen / "projects"
    if not link.exists():
        link.symlink_to(STATE / "projects")


def session(args: list[str]) -> None:
    name = os.environ.get("JESS_CODER_NAME", "")
    base = os.environ.get("JESS_CODER_BASE", "")
    if not NAME_RE.match(name) or not SHA_RE.match(base):
        die("JESS_CODER_NAME / JESS_CODER_BASE missing or malformed")
    if not os.environ.get("JESS_BRAIN_KEY"):
        die("no coder token (JESS_BRAIN_KEY)")
    unknown = [a for a in args if a not in ("--continue", "--ask", "--headless")]
    if unknown:
        die(f"unknown session option(s): {' '.join(unknown)}")
    print("jess-coder box: isolation self-test")
    if run_selftest() != 0:
        die("the isolation self-test failed; the box stays closed", SELFTEST_FAILED)
    prepare_workspace(name, base)
    prepare_home()
    cmd = [
        "qwen",
        "--approval-mode",
        "default" if "--ask" in args else "yolo",
        "--exclude-tools",
        EXCLUDE_TOOLS,
        "--system-prompt",
        SYSTEM_PROMPT.read_text(),
    ]
    if "--continue" in args:
        cmd.append("--continue")
    if "--headless" in args:
        # One instruction from stdin; keep retrying on 429 (chat goes first).
        cmd += ["--output-format", "text", "--max-wall-time", HEADLESS_WALL_TIME]
        os.environ["QWEN_CODE_UNATTENDED_RETRY"] = "1"
    os.chdir(REPO)
    os.execvp(cmd[0], cmd)


def patch(args: list[str]) -> None:
    if len(args) != 1 or not SHA_RE.match(args[0]):
        die("usage: patch <base-sha>")
    base = args[0]
    if not (REPO / ".git").is_dir():
        die("this session has no workspace yet")
    # /work is read-only here: the scratch index and any new blob objects go to
    # /tmp, reading the repo's own objects through an alternate.
    env = dict(os.environ)
    objects = "/tmp/patch-objects"
    os.makedirs(objects, exist_ok=True)
    env.update(
        GIT_INDEX_FILE="/tmp/patch-index",
        GIT_OBJECT_DIRECTORY=objects,
        GIT_ALTERNATE_OBJECT_DIRECTORIES=str(REPO / ".git" / "objects"),
        GIT_OPTIONAL_LOCKS="0",
    )
    git("read-tree", base, env=env)
    git("add", "--all", "--", ".", env=env)
    diff = subprocess.run(
        [
            *SAFE_GIT,
            "diff",
            "--cached",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--no-renames",
            "--full-index",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            base,
            "--",
        ],
        cwd=REPO,
        env=env,
        check=True,
        stdout=subprocess.PIPE,
    )
    sys.stdout.buffer.write(diff.stdout)


def clean(args: list[str]) -> None:
    if args:
        die("usage: clean")
    for child in WORK.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    print("jess-coder box: workspace emptied")


def main(argv: list[str]) -> None:
    mode, args = (argv[0], argv[1:]) if argv else ("selftest", [])
    if mode == "selftest":
        if args:
            die("usage: selftest")
        sys.exit(run_selftest())
    if mode == "session":
        session(args)
    elif mode == "patch":
        patch(args)
    elif mode == "clean":
        clean(args)
    else:
        die(f"unknown mode {mode!r}")


if __name__ == "__main__":
    main(sys.argv[1:])
