"""Locate the dev tools installed next to the interpreter that launched us.

`make setup` puts ruff, mypy, and pip-audit in `.venv/bin`. CI installs those
same packages onto PATH. Checking the interpreter's directory first means
`make lint` uses the venv even when it is not activated.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def which_tool(name: str) -> str:
    """Return an executable path for a dev tool, or raise if it is not installed."""
    override = os.environ.get(name.upper())
    if override:
        return override
    # sys.executable in a venv is often a symlink to the system interpreter.
    # Resolving it first would look in /usr/bin and miss the venv's ruff.
    candidates = (
        Path(sys.executable).parent / name,
        Path(sys.executable).resolve().parent / name,
    )
    for sibling in candidates:
        if sibling.is_file() and os.access(sibling, os.X_OK):
            return str(sibling)
    found = shutil.which(name)
    if found:
        return found
    raise RuntimeError(
        f"cannot find {name}. Run `make setup` or pip install -r requirements-dev.txt."
    )


def run_tool(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a dev tool whose path we already resolved.

    The executable is not a shell string, so this is not `shell=True`.
    """
    return subprocess.run(  # noqa: S603
        argv,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
