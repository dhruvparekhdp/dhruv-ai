"""Bounded, read-only repository inventory for a prepared task workspace."""

from __future__ import annotations

import json
import os
import stat
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

from app.core.redact import redact
from app.workspaces.manager import validate_workspace_path
from app.workspaces.models import Workspace, WorkspaceError

MAX_FILES = 2_000
MAX_DIRECTORIES = 2_000
MAX_DEPTH = 8
MAX_MANIFEST_BYTES = 256_000
MAX_TOTAL_MANIFEST_BYTES = 2_000_000
MAX_SUGGESTED_SCRIPTS = 200
MAX_README_CHARS = 4_000

_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".tox", ".mypy_cache", ".pytest_cache", "target", "dist", "build",
}
_MANIFESTS = {
    "pyproject.toml", "requirements.txt", "Pipfile", "Pipfile.lock", "poetry.lock", "uv.lock",
    "package.json", "pnpm-lock.yaml", "yarn.lock", "package-lock.json",
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum", "Makefile", "Dockerfile",
    "compose.yaml", "docker-compose.yml", "pom.xml", "build.gradle", "Gemfile",
}
_PACKAGE_MANAGERS = {
    "requirements.txt": "pip",
    "Pipfile": "pipenv",
    "Pipfile.lock": "pipenv",
    "Cargo.toml": "cargo",
    "go.mod": "go modules",
    "pom.xml": "maven",
    "build.gradle": "gradle",
    "Gemfile": "bundler",
}
_ENTRYPOINT_NAMES = {
    "main.py", "app.py", "__main__.py", "manage.py", "server.py",
    "main.js", "index.js", "server.js", "main.ts", "index.ts", "server.ts",
    "main.go", "main.rs", "Program.cs", "Main.java",
}
_LANGUAGES = {
    ".py": "Python", ".pyi": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
    ".mjs": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".rs": "Rust", ".go": "Go", ".java": "Java", ".kt": "Kotlin",
    ".swift": "Swift", ".rb": "Ruby", ".php": "PHP", ".cs": "C#",
    ".c": "C", ".h": "C", ".cpp": "C++", ".hpp": "C++", ".html": "HTML",
    ".css": "CSS", ".sql": "SQL", ".sh": "Shell",
}


def _bounded_read(path: Path, limit: int) -> str:
    if path.is_symlink() or not path.is_file():
        return ""
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return ""
        stream = os.fdopen(descriptor, "rb")
        descriptor = -1
        with stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit or b"\0" in raw:
            return ""
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return ""
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def inspect_workspace(workspace: Workspace) -> dict[str, Any]:
    """Summarize source layout and manifests; never run scripts or build tools."""
    root = validate_workspace_path(workspace, ".")
    if root.is_symlink() or not root.is_dir():
        raise WorkspaceError("workspace root is unavailable")

    manifest_paths: list[str] = []
    extension_counts: Counter[str] = Counter()
    file_count = 0
    truncated = False
    readme_path: Path | None = None
    license_files: list[str] = []
    entry_points: list[str] = []
    directories_seen = 0

    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        directories_seen += 1
        if directories_seen > MAX_DIRECTORIES:
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
            file_count += 1
            if file_count > MAX_FILES:
                truncated = True
                break
            relative = path.relative_to(root).as_posix()
            if name in _MANIFESTS:
                manifest_paths.append(relative)
            if name.lower().startswith(("license", "licence", "copying", "notice")):
                license_files.append(relative)
            if name in _ENTRYPOINT_NAMES:
                entry_points.append(relative)
            extension_counts[_LANGUAGES.get(path.suffix.lower(), "Other")] += 1
            if readme_path is None and name.lower() == "readme.md":
                readme_path = path
        if truncated:
            break

    manifest_data: dict[str, Any] = {}
    suggested_scripts: dict[str, str] = {}
    manifest_bytes = 0
    for relative in manifest_paths:
        path = validate_workspace_path(workspace, relative)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_MANIFEST_BYTES:
            manifest_data[relative] = {"parse_error": "manifest exceeds the per-file read limit"}
            continue
        if manifest_bytes + size > MAX_TOTAL_MANIFEST_BYTES:
            truncated = True
            break
        manifest_bytes += size
        text = _bounded_read(path, MAX_MANIFEST_BYTES)
        if not text:
            continue
        if path.name == "package.json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                manifest_data[relative] = {"parse_error": "invalid JSON"}
                continue
            if not isinstance(data, dict):
                manifest_data[relative] = {"parse_error": "JSON root must be an object"}
                continue
            package_manager = next(
                (
                    manager
                    for lockfile, manager in (
                        ("pnpm-lock.yaml", "pnpm"),
                        ("yarn.lock", "yarn"),
                        ("package-lock.json", "npm"),
                    )
                    if (path.parent / lockfile).is_file()
                ),
                "npm-compatible",
            )
            manifest_data[relative] = {
                "name": data.get("name", ""),
                "package_manager": package_manager,
                "license": data.get("license", ""),
            }
            scripts = data.get("scripts", {})
            if isinstance(scripts, dict):
                for key, value in scripts.items():
                    if len(suggested_scripts) >= MAX_SUGGESTED_SCRIPTS:
                        truncated = True
                        break
                    if isinstance(value, str):
                        suggested_scripts[f"{relative}::{str(key)[:100]}"] = redact(value[:500])
        elif path.name == "pyproject.toml":
            try:
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                manifest_data[relative] = {"parse_error": "invalid TOML"}
                continue
            project = data.get("project", {})
            build_system = data.get("build-system", {})
            manifest_data[relative] = {
                "name": project.get("name", "") if isinstance(project, dict) else "",
                "license": project.get("license", "") if isinstance(project, dict) else "",
                "package_manager": next(
                    (
                        manager
                        for lockfile, manager in (
                            ("uv.lock", "uv"),
                            ("poetry.lock", "poetry"),
                            ("Pipfile.lock", "pipenv"),
                        )
                        if (path.parent / lockfile).is_file()
                    ),
                    "pip",
                ),
                "build_system": (
                    build_system.get("build-backend", "")
                    if isinstance(build_system, dict)
                    else ""
                ),
            }
        elif path.name == "Makefile":
            manifest_data[relative] = {"present": True, "package_manager": "make"}
            for line in text.splitlines():
                if len(suggested_scripts) >= MAX_SUGGESTED_SCRIPTS:
                    truncated = True
                    break
                if not line or line[0] in " \t#." or ":" not in line:
                    continue
                target = line.split(":", 1)[0].strip()
                if target and " " not in target and "=" not in target:
                    suggested_scripts[f"{relative}::{target[:100]}"] = target[:100]
        else:
            manifest_data[relative] = {
                "present": True,
                "package_manager": _PACKAGE_MANAGERS.get(path.name, ""),
            }

    readme_excerpt = ""
    setup_steps: list[str] = []
    if readme_path is not None:
        readme_excerpt = redact(_bounded_read(readme_path, MAX_README_CHARS + 1)[:MAX_README_CHARS])
        in_fence = False
        for line in readme_excerpt.splitlines():
            marker = line.strip()
            if marker.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence and marker and not marker.startswith("#"):
                setup_steps.append(marker[:300])
            if len(setup_steps) >= 12:
                break

    license_summaries: list[dict[str, str]] = []
    for relative in license_files[:8]:
        license_path = validate_workspace_path(workspace, relative)
        title = next(
            (
                line.strip()
                for line in _bounded_read(license_path, 240).splitlines()
                if line.strip()
            ),
            "",
        )
        license_summaries.append({
            "path": relative,
            "title_untrusted": redact(title)[:160],
        })

    return {
        "workspace_id": workspace.workspace_id,
        "file_count": min(file_count, MAX_FILES),
        "truncated": truncated,
        "languages": dict(extension_counts.most_common()),
        "manifests": manifest_data,
        "license_files": license_files,
        "license_summaries_untrusted": license_summaries,
        "likely_entry_points": entry_points,
        # Script strings are untrusted suggestions; callers must never execute them directly.
        "suggested_scripts_untrusted": suggested_scripts,
        "readme_excerpt_untrusted": readme_excerpt,
        "readme_setup_steps_untrusted": setup_steps,
    }
