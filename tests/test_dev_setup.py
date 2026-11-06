"""Guards for the dev-setup contract.

These read the repo rather than booting the app. They fail when a new
environment variable has no blank entry in .env.example, when CI drops a
required check, or when the lint baseline drifts.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

from scripts.baseline import diff_counts, load_counts, main
from scripts.pip_audit import load_ignore_ids

ROOT = Path(__file__).resolve().parent.parent

# Desktop session variables the node agent inspects. They are part of the
# machine, not Jarvis configuration, so they stay out of .env.example.
_OS_ENV = frozenset({"DISPLAY", "WAYLAND_DISPLAY"})

_ENV_CALL = re.compile(
    r"""(?:os\.getenv|os\.environ\.get|_env_bool|_env)\(\s*["']([A-Z][A-Z0-9_]*)["']"""
)
_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
_COMMENTED_ASSIGNMENT = re.compile(r"^\s*#\s*[A-Z][A-Z0-9_]*=")
_SKIP_DIRS = frozenset({
    ".git", ".venv", ".tools", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache",
})


def _python_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        files.append(path)
    return files


def _env_names_in_source() -> set[str]:
    names: set[str] = set()
    for path in _python_files():
        names.update(_ENV_CALL.findall(path.read_text(encoding="utf-8")))
    return names - _OS_ENV


def _example_text() -> str:
    return (ROOT / ".env.example").read_text(encoding="utf-8")


def _example_assignments(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        match = _ASSIGNMENT.fullmatch(raw.strip())
        assert match is not None, f".env.example:{lineno} is not a NAME= assignment"
        name, value = match.group(1), match.group(2)
        assert value == "", f".env.example:{lineno} {name} must be blank"
        found[name] = value
    return found


def test_env_example_lists_every_variable_without_a_value() -> None:
    text = _example_text()
    assignments = _example_assignments(text)
    assert set(assignments) == _env_names_in_source()
    assert "change-me" not in text
    assert "password@" not in text
    for raw in text.splitlines():
        assert _COMMENTED_ASSIGNMENT.match(raw) is None


def test_each_env_variable_has_a_description() -> None:
    lines = _example_text().splitlines()
    for index, raw in enumerate(lines):
        if raw.lstrip().startswith("#") or _ASSIGNMENT.fullmatch(raw.strip()) is None:
            continue
        previous = index - 1
        while previous >= 0 and not lines[previous].strip():
            previous -= 1
        assert previous >= 0
        assert lines[previous].lstrip().startswith("#"), raw


def test_make_targets_match_ci() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for target in ("setup", "lint", "typecheck", "test", "security"):
        assert f"\n{target}:" in f"\n{makefile}"
    for snippet in (
        "push:",
        "pull_request:",
        "make lint",
        "make typecheck",
        "make test",
        "make security",
        "fetch-depth: 0",
    ):
        assert snippet in workflow
    assert "pip_audit" in makefile
    assert "gitleaks detect" in makefile
    assert "npm audit" in makefile
    assert not (ROOT / "package.json").is_file()
    assert not (ROOT / "app" / "static" / "package.json").is_file()


def test_gitleaks_pin_is_checksummed() -> None:
    text = (ROOT / "scripts" / "install-gitleaks.sh").read_text(encoding="utf-8")
    assert "8.30.1" in text
    assert "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb" in text


def test_pip_audit_ignore_file_lists_each_id_once() -> None:
    path = ROOT / "security" / "pip-audit-ignore.txt"
    ids = load_ignore_ids(path)
    raw = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert raw == ids


def test_diff_counts_flags_new_and_resolved_findings() -> None:
    added, removed = diff_counts(Counter({"a": 2, "b": 1}), Counter({"a": 1, "c": 1}))
    assert added == ["+1 a", "+1 b"]
    assert removed == ["-1 c"]


def test_dev_tooling_is_not_baseline_debt() -> None:
    """New files in this change are held to the strict config, not the baseline."""
    for name in ("ruff.txt", "mypy.txt"):
        text = (ROOT / "scripts" / "baselines" / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            path = line.split("\t", 2)[1]
            assert not path.startswith("scripts/")
            assert path != "tests/test_dev_setup.py"


def test_load_counts_rejects_a_bad_line(tmp_path: Path) -> None:
    path = tmp_path / "base.txt"
    path.write_text("# comment\n\nnot-a-baseline\n", encoding="utf-8")
    with pytest.raises(ValueError, match="expected COUNT, PATH, CODE, MESSAGE"):
        load_counts(path)


def test_committed_baselines_match_the_tree() -> None:
    assert main(["check-ruff"]) == 0
    assert main(["check-mypy"]) == 0
