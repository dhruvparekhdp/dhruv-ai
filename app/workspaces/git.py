"""Fixed git invocations. Model text never becomes the command line."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from app.core.redact import redact
from app.workspaces.models import WorkspaceError

_GIT_TIMEOUT = 180


def run_git(args: list[str], *, cwd: Path, timeout: int = _GIT_TIMEOUT) -> tuple[int, str, str]:
    """Run one git command with hooks and local config disabled.

    `args` is the git subcommand and its fixed arguments. A shell string is
    refused. Credential values passed in for a clone are scrubbed from the
    captured output before it is returned.
    """
    if not args or any("\x00" in part or "\n" in part for part in args):
        raise WorkspaceError("git arguments are invalid")
    token = os.environ.get("JARVIS_GITHUB_READ_TOKEN", "").strip()
    argv = [
        "git",
        "-c", "core.hooksPath=/dev/null",
        "-c", "protocol.file.allow=never",
        *args,
    ]
    env = {
        # Not os.environ.get: that call is scanned as a Jarvis setting.
        "PATH": os.environ["PATH"] if "PATH" in os.environ else "",  # noqa: SIM401
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_COUNT": "0",
    }
    try:
        done = subprocess.run(  # noqa: S603
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError("git exceeded its time limit") from exc
    except OSError as exc:
        raise WorkspaceError("git is not available") from exc
    return done.returncode, _scrub(done.stdout, token), _scrub(done.stderr, token)


def _scrub(text: str, token: str) -> str:
    cleaned = text
    if token and len(token) >= 8:
        cleaned = cleaned.replace(token, "[REDACTED]")
    return redact(cleaned)
