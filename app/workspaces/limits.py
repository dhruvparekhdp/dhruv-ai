"""Caps for a task workspace. These are independent of the model."""

from __future__ import annotations

import os
from pathlib import Path

from app.workspaces.models import WorkspaceError

MAX_CHECKOUT_FILES = 5_000
MAX_CHECKOUT_BYTES = 200_000_000
MAX_PATCH_BYTES = 200_000
MAX_DIFF_CHARS = 100_000
MAX_CHECK_OUTPUT = 80_000


def measure_tree(root: Path) -> tuple[int, int]:
    """Count regular files and bytes, without following symlinks."""
    files = 0
    total = 0
    for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        dirs[:] = [name for name in dirs if not (current_path / name).is_symlink()]
        for name in names:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                continue
            files += 1
            try:
                total += path.stat().st_size
            except OSError:
                continue
            if files > MAX_CHECKOUT_FILES or total > MAX_CHECKOUT_BYTES:
                raise WorkspaceError("workspace exceeds the checkout size limit")
    return files, total
