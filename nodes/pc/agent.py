#!/usr/bin/env python3
"""Jarvis PC node agent.

Runs on any machine you want in the fleet -- Linux, Windows or macOS -- and
dials **out** to the orchestrator, holding one WebSocket open (D-014). Nothing
listens on this machine and no port is forwarded.

First start:

    python3 agent.py --server wss://host.ts.net --enrol enrol_XXXX --name bench-01

After that the issued credentials are cached in `node.json` and it reconnects
by itself:

    python3 agent.py --server wss://host.ts.net

Deliberately dependency-light: standard library plus `websockets`. It has to run
on machines that may not have a build toolchain.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import websockets
except ImportError:  # pragma: no cover - the one runtime dependency
    print("This agent needs the 'websockets' package:  pip install websockets", file=sys.stderr)
    raise SystemExit(1)

log = logging.getLogger("jarvis.node")

CONFIG_PATH = Path(__file__).with_name("node.json")
HEARTBEAT_SECONDS = 30
MAX_BACKOFF = 60


# --------------------------------------------------------------------------
# Capability detection
# --------------------------------------------------------------------------


def detect_capabilities() -> list[str]:
    """Advertise what this machine can actually do.

    Roles are not assigned centrally (D-023) -- the node reports its abilities
    and the scheduler places work accordingly.
    """
    caps = ["EXECUTE", "STORAGE"]
    # A desktop session means screenshots and input injection are possible.
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or platform.system() == "Windows":
        caps.append("DESKTOP")
    if shutil.which("ollama"):
        caps.append("INFERENCE")
    return caps


def machine_specs() -> dict[str, int]:
    cores = os.cpu_count() or 1
    ram_mb = 0
    try:
        if hasattr(os, "sysconf") and "SC_PAGE_SIZE" in os.sysconf_names:
            ram_mb = (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) // (1024 * 1024)
    except (ValueError, OSError):
        pass
    return {"cores": cores, "ram_mb": ram_mb}


def collect_metrics() -> dict[str, object]:
    """Cheap, dependency-free health numbers for the heartbeat."""
    metrics: dict[str, object] = {"platform": platform.platform()}
    try:
        usage = shutil.disk_usage(Path.home())
        metrics["disk_free_gb"] = round(usage.free / 1024**3, 1)
        metrics["disk_total_gb"] = round(usage.total / 1024**3, 1)
    except OSError:
        pass
    if hasattr(os, "getloadavg"):
        try:
            metrics["load_1m"] = round(os.getloadavg()[0], 2)
        except OSError:
            pass
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                metrics["ram_available_mb"] = int(line.split()[1]) // 1024
                break
    except (OSError, ValueError, IndexError):
        pass
    return metrics


# --------------------------------------------------------------------------
# Tools this node offers
# --------------------------------------------------------------------------


class ToolError(RuntimeError):
    pass


class NodeTools:
    """The actual capabilities, jailed to configured roots.

    Every filesystem path is resolved and checked against the allowed roots
    *after* resolution, so `../` cannot escape.
    """

    def __init__(self, roots: list[Path], shell_allowlist: list[str]) -> None:
        self.roots = [r.resolve() for r in roots]
        self.shell_allowlist = shell_allowlist

    # -- path safety --------------------------------------------------

    def _resolve(self, raw: str) -> Path:
        path = Path(raw).expanduser().resolve()
        if not any(path == root or root in path.parents for root in self.roots):
            allowed = ", ".join(str(r) for r in self.roots)
            raise ToolError(f"path outside the allowed roots ({allowed}): {path}")
        return path

    # -- filesystem ---------------------------------------------------

    def fs_list(self, path: str = "") -> str:
        target = self._resolve(path or str(self.roots[0]))
        if not target.is_dir():
            raise ToolError(f"not a directory: {target}")
        entries = []
        for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            try:
                size = item.stat().st_size if item.is_file() else 0
            except OSError:
                size = 0
            entries.append({"name": item.name, "dir": item.is_dir(), "size": size})
        return json.dumps({"path": str(target), "entries": entries[:500]})

    def fs_read(self, path: str, max_bytes: int = 100_000) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"not a file: {target}")
        data = target.read_bytes()[:max_bytes]
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolError(f"{target} is not UTF-8 text") from None

    def fs_write(self, path: str, content: str) -> str:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} characters to {target}"

    def fs_delete(self, path: str) -> str:
        target = self._resolve(path)
        if target.is_dir():
            raise ToolError("refusing to delete a directory; delete files individually")
        if not target.exists():
            return f"{target} does not exist; nothing deleted"
        target.unlink()
        return f"Deleted {target}"

    # -- shell --------------------------------------------------------

    def shell_exec(self, command: str, timeout: int = 60) -> str:
        head = (command or "").strip().split()
        if not head:
            raise ToolError("empty command")
        if self.shell_allowlist and head[0] not in self.shell_allowlist:
            raise ToolError(
                f"'{head[0]}' is not in this node's shell allowlist "
                f"({', '.join(self.shell_allowlist)})"
            )
        try:
            done = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                timeout=timeout, cwd=self.roots[0],
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"command exceeded {timeout}s and was killed") from None
        out = done.stdout.strip()
        err = done.stderr.strip()
        parts = [f"exit_code: {done.returncode}"]
        if out:
            parts.append(f"stdout:\n{out[:8000]}")
        if err:
            parts.append(f"stderr:\n{err[:4000]}")
        return "\n\n".join(parts)

    # -- system -------------------------------------------------------

    def system_stats(self) -> str:
        stats = collect_metrics()
        stats.update(machine_specs())
        return json.dumps(stats, indent=2)

    # -- dispatch -----------------------------------------------------

    def run(self, tool: str, arguments: dict) -> str:
        handlers = {
            "fs.list": self.fs_list,
            "fs.read": self.fs_read,
            "fs.write": self.fs_write,
            "fs.delete": self.fs_delete,
            "shell.exec": self.shell_exec,
            "system.stats": self.system_stats,
        }
        handler = handlers.get(tool)
        if handler is None:
            raise ToolError(f"this node does not implement '{tool}'")
        return handler(**arguments)


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError:
            log.warning("%s is corrupt; ignoring it", CONFIG_PATH)
    return {}


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2))
    try:
        CONFIG_PATH.chmod(0o600)      # it holds this node's secret
    except OSError:
        pass


async def heartbeat_loop(socket) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        await socket.send(json.dumps({"type": "heartbeat", "metrics": collect_metrics()}))


async def session(url: str, config: dict, tools: NodeTools, name: str, enrol_token: str | None) -> None:
    """One connection attempt. Returns when the socket closes."""
    async with websockets.connect(url, ping_interval=20, ping_timeout=20) as socket:
        specs = machine_specs()
        hello = {
            "type": "hello",
            "name": name,
            "platform": platform.system().lower(),
            "capabilities": detect_capabilities(),
            **specs,
        }
        if config.get("node_id") and config.get("secret"):
            hello["node_id"] = config["node_id"]
            hello["secret"] = config["secret"]
        elif enrol_token:
            hello["enrolment_token"] = enrol_token
        else:
            raise SystemExit(
                "No stored credentials and no --enrol token. Mint one with:\n"
                "  POST /api/v1/nodes/enrol"
            )

        await socket.send(json.dumps(hello))
        welcome = json.loads(await socket.recv())

        if welcome.get("type") != "welcome":
            raise SystemExit(f"Server refused the connection: {welcome.get('error')}")

        if welcome.get("secret"):
            # First enrolment: persist the credentials for future restarts.
            config = {"node_id": welcome["node_id"], "secret": welcome["secret"], "name": name}
            save_config(config)
            log.info("enrolled as %s; credentials saved to %s", welcome["node_id"], CONFIG_PATH)
        else:
            log.info("reconnected as %s", welcome["node_id"])

        log.info("capabilities: %s", ", ".join(detect_capabilities()))
        beat = asyncio.create_task(heartbeat_loop(socket))
        try:
            async for raw in socket:
                message = json.loads(raw)
                if message.get("type") != "dispatch":
                    continue
                request_id = message.get("request_id")
                tool = message.get("tool", "")
                arguments = message.get("arguments") or {}
                log.info("running %s %s", tool, arguments)
                try:
                    output = tools.run(tool, arguments)
                    reply = {"type": "result", "request_id": request_id, "ok": True, "output": output}
                except ToolError as exc:
                    reply = {"type": "result", "request_id": request_id, "ok": False, "error": str(exc)}
                except Exception as exc:  # noqa: BLE001 - never die on one bad call
                    log.exception("tool %s failed", tool)
                    reply = {"type": "result", "request_id": request_id, "ok": False, "error": repr(exc)}
                await socket.send(json.dumps(reply))
        finally:
            beat.cancel()


async def run_forever(args: argparse.Namespace) -> None:
    roots = [Path(r).expanduser() for r in args.root]
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    tools = NodeTools(roots, args.allow)

    url = args.server.rstrip("/") + "/ws/node"
    config = load_config()
    name = args.name or config.get("name") or platform.node()
    backoff = 2

    while True:
        try:
            await session(url, config, tools, name, args.enrol)
            backoff = 2                     # a clean disconnect: retry promptly
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - reconnect through anything
            log.warning("connection lost (%s); retrying in %ds", exc, backoff)
        config = load_config()              # pick up credentials saved on first enrol
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis PC node agent")
    parser.add_argument("--server", required=True,
                        help="Orchestrator base URL, e.g. wss://host.ts.net or ws://192.168.1.10:8000")
    parser.add_argument("--enrol", help="One-time enrolment token (first start only)")
    parser.add_argument("--name", help="Node name (defaults to the hostname)")
    parser.add_argument("--root", action="append", default=[],
                        help="Directory this node may touch; repeatable. Defaults to ~/jarvis-workspace")
    parser.add_argument("--allow", action="append", default=[],
                        help="Permitted shell command, repeatable. Empty means allow anything "
                             "(only sensible on a node cleared for untrusted code)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not args.root:
        args.root = [str(Path.home() / "jarvis-workspace")]

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    try:
        asyncio.run(run_forever(args))
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
