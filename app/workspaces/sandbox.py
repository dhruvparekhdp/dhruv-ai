"""Container command builder. Project code is not started on the host."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.redact import redact
from app.workspaces.limits import MAX_CHECK_OUTPUT
from app.workspaces.models import WorkspaceError

_SETUP = (
    "Code execution is refused because no rootless Podman or Docker runtime is available. "
    "Install rootless Podman, set JARVIS_SANDBOX_RUNTIME=podman and JARVIS_SANDBOX_IMAGE "
    "to an image that is already present, and grant build_test for the task. "
    "Inspection and workspace edits stay available. Containers reduce risk; they are not a proof of isolation."
)


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int
    output: str
    duration_ms: int
    runtime: str
    capped: bool


def _captured(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return ""


def setup_instructions() -> str:
    return _SETUP


def detect_runtime() -> str | None:
    forced = os.environ.get("JARVIS_SANDBOX_RUNTIME", "").strip().lower()
    if forced in {"none", "off"}:
        return None
    if forced in {"podman", "docker"}:
        return forced if shutil.which(forced) else None
    for name in ("podman", "docker"):
        if shutil.which(name):
            return name
    return None


def container_argv(
    *,
    workspace: Path,
    argv: list[str],
    network: bool,
    preview: bool,
) -> list[str]:
    """Build a container command. The project argv is appended, not a shell string."""
    runtime = detect_runtime()
    image = os.environ.get("JARVIS_SANDBOX_IMAGE", "").strip()
    if runtime is None or not image:
        raise WorkspaceError(setup_instructions())
    if not argv or any("\x00" in part or "\n" in part for part in argv):
        raise WorkspaceError("check command is invalid")
    network_mode = "bridge" if network or preview else "none"
    command = [
        runtime, "run", "--rm",
        "--network", network_mode,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", os.environ.get("JARVIS_SANDBOX_PIDS", "256"),
        "--memory", os.environ.get("JARVIS_SANDBOX_MEMORY", "4g"),
        "--cpus", os.environ.get("JARVIS_SANDBOX_CPUS", "2"),
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",  # noqa: S108 - container path, not a host temp file
        "--mount", f"type=bind,src={workspace},dst=/work",
        "--workdir", "/work",
        "--user", "65534:65534",
    ]
    if preview:
        command.extend(["--publish", "127.0.0.1::8000"])
    command.extend([image, *argv])
    _refuse_unsafe(command, workspace)
    return command


def _refuse_unsafe(command: list[str], workspace: Path) -> None:
    joined = " ".join(command)
    if "--privileged" in command or "--network host" in joined or "--net=host" in command:
        raise WorkspaceError("sandbox command was refused")
    if "docker.sock" in joined or str(Path.home()) in joined:
        if str(workspace) not in str(Path.home()):
            raise WorkspaceError("sandbox command was refused")
        if "docker.sock" in joined:
            raise WorkspaceError("sandbox command was refused")
    if any(part == "/var/run/docker.sock" for part in command):
        raise WorkspaceError("sandbox command was refused")


def run_container(
    *,
    workspace: Path,
    argv: list[str],
    network: bool,
    preview: bool,
    timeout: int,
) -> SandboxResult:
    command = container_argv(workspace=workspace, argv=argv, network=network, preview=preview)
    started = time.monotonic()
    try:
        done = subprocess.run(  # noqa: S603
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = redact(_captured(exc.stdout) + _captured(exc.stderr))[:MAX_CHECK_OUTPUT]
        return SandboxResult(
            exit_code=124,
            output=output + "\nThe sandbox hit its wall-clock limit.",
            duration_ms=int((time.monotonic() - started) * 1000),
            runtime=command[0],
            capped=True,
        )
    except OSError as exc:
        raise WorkspaceError(setup_instructions()) from exc
    text = redact(_captured(done.stdout) + _captured(done.stderr))
    capped = len(text) > MAX_CHECK_OUTPUT
    return SandboxResult(
        exit_code=done.returncode,
        output=text[:MAX_CHECK_OUTPUT],
        duration_ms=int((time.monotonic() - started) * 1000),
        runtime=command[0],
        capped=capped,
    )
