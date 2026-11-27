"""Bounded read and exact-text search for prepared task workspaces."""

from __future__ import annotations

import fnmatch
import os
import stat
from pathlib import Path
from typing import Any

from app.core.redact import redact
from app.workspaces.manager import validate_workspace_path
from app.workspaces.models import Workspace, WorkspaceError

MAX_READ_BYTES = 256_000
MAX_SEARCH_FILES = 1_000
MAX_SEARCH_DIRECTORIES = 2_000
MAX_SEARCH_BYTES = 8_000_000
MAX_SEARCH_RESULTS = 100
MAX_LINE_CHARS = 800
MAX_DEPTH = 8

_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".ssh", ".aws", ".secrets", ".venv", "venv", "node_modules", "__pycache__",
    ".tox", ".mypy_cache", ".pytest_cache", "target", "dist", "build", ".jarvis",
}
_SAFE_ENV_FILES = {".env.example", ".env.sample", ".env.template", ".env.dist"}
_SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}
_SENSITIVE_NAMES = {
    "id_rsa", "id_ed25519", "credentials", "credentials.json", "secrets.json",
    ".envrc", ".npmrc", ".pypirc", ".netrc", ".git-credentials",
}


def _is_sensitive_path(relative_path: str) -> bool:
    path = Path(relative_path)
    name = path.name.lower()
    if any(part.lower() in _SKIP_DIRS for part in path.parts):
        return True
    if name.startswith(".env") and name not in _SAFE_ENV_FILES:
        return True
    return name in _SENSITIVE_NAMES or path.suffix.lower() in _SENSITIVE_SUFFIXES


def _read_bytes(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise WorkspaceError("workspace file is unavailable")
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise WorkspaceError("workspace file is not a regular file")
        content = os.fdopen(descriptor, "rb")
        descriptor = -1
        with content:
            data = content.read(limit + 1)
        if len(data) > limit:
            raise WorkspaceError("workspace file exceeds the read limit")
    except OSError as exc:
        raise WorkspaceError("workspace file could not be read") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return data


def _read_text(path: Path, limit: int) -> str:
    content = _read_bytes(path, limit)
    if b"\0" in content:
        raise WorkspaceError("binary files cannot be read as text")
    return content.decode("utf-8", errors="replace")


def read_file(
    workspace: Workspace,
    relative_path: str,
    *,
    start_line: int = 1,
    line_count: int = 200,
) -> dict[str, Any]:
    """Read a bounded line range from a file inside the task workspace."""
    if start_line < 1 or not 1 <= line_count <= 500:
        raise WorkspaceError("line range is outside the allowed bounds")
    if _is_sensitive_path(relative_path):
        raise WorkspaceError("sensitive paths are excluded from repository reading")
    path = validate_workspace_path(workspace, relative_path)
    content = _read_text(path, MAX_READ_BYTES)
    lines = content.splitlines()
    start = min(start_line, len(lines) + 1)
    selected = lines[start - 1 : start - 1 + line_count]
    return {
        "path": path.relative_to(Path(workspace.root_path).resolve(strict=True)).as_posix(),
        "start_line": start,
        "end_line": start + len(selected) - 1 if selected else start - 1,
        "total_lines": len(lines),
        "content_untrusted": redact("\n".join(selected)),
    }


def is_excluded_path(relative_path: str) -> bool:
    """True for secret files, VCS metadata, and dependency trees."""
    return _is_sensitive_path(relative_path)


def search_text(
    workspace: Workspace,
    query: str,
    *,
    file_glob: str = "",
    max_results: int = 50,
    mode: str = "text",
) -> dict[str, Any]:
    """Search exact text with strict file, byte, and result budgets."""
    if not query or len(query) > 300:
        raise WorkspaceError("search query must contain 1 to 300 characters")
    if not 1 <= max_results <= MAX_SEARCH_RESULTS:
        raise WorkspaceError("max_results is outside the allowed bounds")

    if mode not in {"text", "path", "symbol"}:
        raise WorkspaceError("search mode must be text, path, or symbol")

    root = validate_workspace_path(workspace, ".")
    results: list[dict[str, Any]] = []
    files_seen = 0
    directories_seen = 0
    bytes_seen = 0
    truncated = False
    needle = query.casefold()

    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        directories_seen += 1
        if directories_seen > MAX_SEARCH_DIRECTORIES:
            truncated = True
            break
        current_path = Path(current)
        depth = len(current_path.relative_to(root).parts)
        dirs[:] = [
            name for name in dirs
            if name not in _SKIP_DIRS and not (current_path / name).is_symlink()
        ]
        if depth >= MAX_DEPTH:
            dirs.clear()
        for name in files:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if _is_sensitive_path(relative):
                continue
            if file_glob and not fnmatch.fnmatchcase(relative, file_glob):
                continue
            files_seen += 1
            if files_seen > MAX_SEARCH_FILES:
                truncated = True
                break
            if mode == "path":
                if needle in relative.casefold():
                    results.append({
                        "path": relative,
                        "line": 0,
                        "text_untrusted": relative,
                    })
                if len(results) >= max_results:
                    truncated = True
                    break
                continue
            try:
                raw = _read_bytes(path, MAX_READ_BYTES)
            except WorkspaceError:
                continue
            if bytes_seen + len(raw) > MAX_SEARCH_BYTES:
                truncated = True
                break
            bytes_seen += len(raw)
            if b"\0" in raw:
                continue
            for line_number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                folded = line.casefold()
                if mode == "symbol":
                    if needle not in folded:
                        continue
                    stripped = line.lstrip()
                    if not stripped.startswith((
                        "def ", "class ", "function ", "func ", "fn ", "type ", "const ", "let ", "var ",
                    )):
                        continue
                elif needle not in folded:
                    continue
                results.append({
                    "path": relative,
                    "line": line_number,
                    "text_untrusted": redact(line[:MAX_LINE_CHARS]),
                })
                if len(results) >= max_results:
                    truncated = True
                    break
            if len(results) >= max_results:
                break
        if truncated or len(results) >= max_results:
            break

    return {
        "workspace_id": workspace.workspace_id,
        "query": query,
        "mode": mode,
        "count": len(results),
        "truncated": truncated,
        "results_untrusted": results,
    }
