"""Run pip-audit against the pinned requirement files.

Advisories listed in security/pip-audit-ignore.txt are the baseline: they
were already present when this check was added, and dependency upgrades are
a separate change. Any advisory that is not listed fails the run.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.which_tool import run_tool, which_tool

ROOT = Path(__file__).resolve().parent.parent
IGNORE_FILE = ROOT / "security" / "pip-audit-ignore.txt"
REQUIREMENTS = ("requirements.txt", "requirements-dev.txt", "requirements-mcp.txt")

# pip-audit ids look like PYSEC-2026-1845, CVE-2026-112390, or GHSA-....
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def load_ignore_ids(path: Path) -> list[str]:
    """Return vulnerability ids, skipping blanks and comments. Order is preserved."""
    if not path.is_file():
        raise ValueError(f"missing pip-audit ignore file {path}")
    ids: list[str] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not _ID.fullmatch(line):
            raise ValueError(f"{path}:{lineno}: not a vulnerability id")
        if line not in ids:
            ids.append(line)
    return ids


def main(argv: list[str] | None = None) -> int:
    del argv  # the check takes no arguments; the ignore file is the configuration
    try:
        ids = load_ignore_ids(IGNORE_FILE)
    except ValueError as exc:
        print(f"pip-audit: {exc}", file=sys.stderr)
        return 2
    command = [
        which_tool("pip-audit"),
        "--progress-spinner",
        "off",
        "--desc",
        "on",
    ]
    for name in REQUIREMENTS:
        command.extend(["-r", name])
    for vuln_id in ids:
        command.extend(["--ignore-vuln", vuln_id])
    try:
        proc = run_tool(command)
    except RuntimeError as exc:
        print(f"pip-audit: {exc}", file=sys.stderr)
        return 2
    if proc.returncode != 0:
        # pip-audit writes the report to stdout. Surface it without adding anything else.
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        return proc.returncode
    print(f"pip-audit: no unignored advisories ({len(ids)} baselined)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
