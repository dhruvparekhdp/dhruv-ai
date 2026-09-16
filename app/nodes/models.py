"""Node domain model.

A node is any machine that can do work for Jarvis: the host laptop, a spare
laptop, later an Android phone. The iPhone is a node too, but a reporting-only
one (D-013).

**Roles are not hardcoded.** A node advertises what it *can* do and the
scheduler places work against those capabilities (D-023), so the fleet works
with one machine or five and adding hardware needs no change here.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class NodeCapability(str, Enum):
    REPORT = "REPORT"          # can send telemetry only (an iPhone)
    EXECUTE = "EXECUTE"        # can run shell commands and touch files
    DESKTOP = "DESKTOP"        # has a GUI: screenshots, AT-SPI, input injection
    INFERENCE = "INFERENCE"    # can run a local model
    STORAGE = "STORAGE"        # has meaningful disk to spare
    ANDROID = "ANDROID"        # Android companion app


class NodeStatus(str, Enum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    DRAINING = "DRAINING"      # finish current work, accept nothing new


#: A node unreachable for longer than this is presumed gone and its work is
#: rescheduled. Generous enough to survive a Wi-Fi blip.
HEARTBEAT_TIMEOUT_SECONDS = 90


@dataclass
class Node:
    node_id: str
    name: str
    platform: str = "linux"              # linux | windows | darwin | android | ios
    capabilities: set[NodeCapability] = field(default_factory=set)
    ram_mb: int = 0
    cores: int = 0
    #: Whether this node may run untrusted, model-generated code. Deliberately
    #: opt-in: the default must be "no" so a new node cannot silently become
    #: the sandbox.
    untrusted_ok: bool = False
    status: NodeStatus = NodeStatus.OFFLINE
    last_seen: str = ""
    enrolled_at: str = ""
    current_task_id: str | None = None
    #: Free-form: battery, network, OS version, whatever the node reports.
    metrics: dict[str, Any] = field(default_factory=dict)

    def has(self, capability: NodeCapability) -> bool:
        return capability in self.capabilities

    def describe(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "platform": self.platform,
            "capabilities": sorted(c.value for c in self.capabilities),
            "ram_mb": self.ram_mb,
            "cores": self.cores,
            "untrusted_ok": self.untrusted_ok,
            "status": self.status.value,
            "last_seen": self.last_seen,
            "enrolled_at": self.enrolled_at,
            "current_task_id": self.current_task_id,
            "metrics": self.metrics,
        }


def new_node_id() -> str:
    return f"node_{uuid.uuid4()}"


def new_secret() -> str:
    """Long random string a node presents on reconnect."""
    return secrets.token_urlsafe(32)


def new_enrolment_token() -> str:
    return f"enrol_{secrets.token_urlsafe(24)}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_capabilities(raw: Any) -> set[NodeCapability]:
    """Parse capabilities from a node's hello message, ignoring unknown values.

    Tolerant on purpose: a newer node advertising a capability this server does
    not know about should still connect with the ones it does.
    """
    out: set[NodeCapability] = set()
    for item in raw or []:
        try:
            out.add(NodeCapability(str(item).upper()))
        except ValueError:
            continue
    return out
