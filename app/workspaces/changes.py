"""Review and apply a patch inside one task workspace."""

from __future__ import annotations

from pathlib import Path

from app.core.redact import redact
from app.workspaces.access import record_event, require_grant
from app.workspaces.git import run_git
from app.workspaces.limits import MAX_DIFF_CHARS, MAX_PATCH_BYTES
from app.workspaces.manager import validate_workspace_path
from app.workspaces.models import Workspace, WorkspaceError
from app.workspaces.repo import is_excluded_path


def workspace_diff(workspace: Workspace) -> dict[str, object]:
    require_grant(workspace, "repository_read")
    root = validate_workspace_path(workspace, ".")
    git_dir = root / ".git"
    if not git_dir.is_dir() or git_dir.is_symlink():
        names = _names(root)
        record_event(workspace_id=workspace.workspace_id, actor="owner", tool="repo.diff")
        return {"kind": "unversioned", "diff_untrusted": "", "files": names}
    code, out, err = run_git(
        ["diff", "--no-ext-diff", "--stat", "--patch", "--", "."], cwd=root,
    )
    _status, porcelain, _err = run_git(["status", "--porcelain", "--", "."], cwd=root)
    record_event(
        workspace_id=workspace.workspace_id, actor="owner", tool="repo.diff", exit_code=code,
    )
    text = redact(out or err)[:MAX_DIFF_CHARS]
    return {
        "kind": "git",
        "exit_code": code,
        "diff_untrusted": text,
        "status_untrusted": redact(porcelain)[:20_000],
    }


def _names(root: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if is_excluded_path(relative):
            continue
        found.append(relative)
        if len(found) >= 200:
            break
    return found


def apply_patch(workspace: Workspace, diff_text: str, *, actor: str = "owner") -> dict[str, object]:
    """Apply a unified diff. Paths that leave the workspace are refused."""
    require_grant(workspace, "workspace_write")
    if len(diff_text.encode("utf-8")) > MAX_PATCH_BYTES:
        raise WorkspaceError("patch exceeds the size limit")
    files = _parse(diff_text)
    if not files:
        raise WorkspaceError("patch did not contain a file")
    changed: list[str] = []
    for relative, deleted, hunks in files:
        if is_excluded_path(relative):
            raise WorkspaceError("patch touches an excluded path")
        target = validate_workspace_path(workspace, relative)
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise WorkspaceError("patch target is not a regular file")
        if deleted:
            if target.is_file() and not target.is_symlink():
                target.unlink()
            changed.append(relative)
            continue
        original = [] if not target.exists() else target.read_text(encoding="utf-8").splitlines()
        updated = _apply(original, hunks)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(updated) + ("\n" if updated else ""), encoding="utf-8")
        changed.append(relative)
    record_event(
        workspace_id=workspace.workspace_id, actor=actor, tool="workspace.write_patch", paths=changed,
    )
    return {"changed": changed}


def _parse(diff_text: str) -> list[tuple[str, bool, list[list[tuple[str, str]]]]]:
    if "diff --git " not in diff_text:
        raise WorkspaceError("patch must be a unified git diff")
    chunks = diff_text.split("diff --git ")[1:]
    parsed: list[tuple[str, bool, list[list[tuple[str, str]]]]] = []
    for chunk in chunks:
        lines = chunk.splitlines()
        header = lines[0] if lines else ""
        parts = header.split()
        if len(parts) != 2 or not parts[0].startswith("a/") or not parts[1].startswith("b/"):
            raise WorkspaceError("patch file header is invalid")
        relative = parts[1][2:]
        if parts[0][2:] not in {relative, "dev/null"} and parts[0] != "a/dev/null":
            raise WorkspaceError("patch renames are not allowed")
        deleted = any(line.startswith("+++ /dev/null") for line in lines)
        new_file = any(line.startswith("--- /dev/null") for line in lines)
        if ".." in Path(relative).parts or relative.startswith("/") or not relative:
            raise WorkspaceError("patch path is outside the workspace")
        hunks: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] | None = None
        for line in lines[1:]:
            if line.startswith("@@"):
                current = []
                hunks.append(current)
                continue
            if current is None or line.startswith(("diff ", "index ", "new file", "deleted file", "---", "+++")):
                continue
            if line.startswith("\\"):
                continue
            if not line:
                marker, text = " ", ""
            else:
                marker, text = line[0], line[1:]
            if marker not in " +-":
                raise WorkspaceError("patch hunk is invalid")
            current.append((marker, text))
        if not hunks and not deleted and not new_file:
            raise WorkspaceError("patch file has no hunks")
        parsed.append((relative, deleted, hunks))
    return parsed


def _apply(lines: list[str], hunks: list[list[tuple[str, str]]]) -> list[str]:
    result: list[str] = []
    cursor = 0
    for hunk in hunks:
        old = [text for marker, text in hunk if marker in " -"]
        index = _find(lines, old, cursor)
        if index is None:
            raise WorkspaceError("patch does not apply")
        result.extend(lines[cursor:index])
        for marker, text in hunk:
            if marker in " +":
                result.append(text)
        cursor = index + len(old)
    result.extend(lines[cursor:])
    return result


def _find(lines: list[str], old: list[str], start: int) -> int | None:
    if not old:
        return start
    limit = len(lines) - len(old)
    for index in range(start, limit + 1):
        if lines[index:index + len(old)] == old:
            return index
    return None
