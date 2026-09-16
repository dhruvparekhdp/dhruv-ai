"""Node enrolment, authentication, capability placement, and dispatch.

The safety properties here matter as much as the plumbing: a node must not
become the untrusted-code sandbox by accident, and a missing node must be
reported rather than silently falling back to running the work locally.
"""

from __future__ import annotations

import json

import pytest

from app.nodes import store
from app.nodes.gateway import NodeGateway, NoNodeAvailable
from app.nodes.models import NodeCapability, NodeStatus, parse_capabilities
from tests.conftest import TEST_PASSCODE


def _enrol(name="worker", caps=(NodeCapability.EXECUTE,), **kw):
    return store.enrol(name=name, platform="linux", capabilities=set(caps), **kw)


# --- enrolment tokens -----------------------------------------------------


def test_enrolment_token_is_single_use(agents) -> None:
    store.init_db()
    token = store.create_enrolment_token("bench")

    assert store.redeem_enrolment_token(token) is True
    assert store.redeem_enrolment_token(token) is False      # already used
    assert store.redeem_enrolment_token("enrol_nonsense") is False


def test_expired_enrolment_token_is_refused(agents, monkeypatch) -> None:
    store.init_db()
    monkeypatch.setattr(store, "ENROLMENT_TTL_MINUTES", -1)   # already expired
    token = store.create_enrolment_token()
    assert store.redeem_enrolment_token(token) is False


# --- node records ---------------------------------------------------------


def test_enrol_and_authenticate(agents) -> None:
    store.init_db()
    node, secret = _enrol("bench-01")

    assert node.node_id.startswith("node_")
    assert store.authenticate(node.node_id, secret) is True
    assert store.authenticate(node.node_id, "wrong") is False
    assert store.authenticate("node_nope", secret) is False


def test_secret_is_not_stored_in_clear(agents) -> None:
    from app.core.db import fetch_one, get_conn

    store.init_db()
    node, secret = _enrol()
    with get_conn() as conn:
        row = fetch_one(conn, "SELECT secret_hash FROM nodes WHERE node_id = ?", (node.node_id,))
    assert secret not in row["secret_hash"]
    assert len(row["secret_hash"]) == 64          # sha256 hex


def test_revoke_kills_the_secret(agents) -> None:
    store.init_db()
    node, secret = _enrol()

    assert store.revoke(node.node_id) is True
    assert store.authenticate(node.node_id, secret) is False
    assert store.revoke(node.node_id) is False


def test_untrusted_is_off_by_default_and_explicit_to_grant(agents) -> None:
    """A node must never become the code sandbox by accident."""
    store.init_db()
    node, _ = _enrol()
    assert store.get(node.node_id).untrusted_ok is False

    assert store.set_untrusted_ok(node.node_id, True) is True
    assert store.get(node.node_id).untrusted_ok is True
    assert store.set_untrusted_ok("node_nope", True) is False


def test_boot_marks_every_node_offline(agents) -> None:
    """Pre-crash presence is not evidence a node is still there."""
    store.init_db()
    node, _ = _enrol()
    assert store.get(node.node_id).status is NodeStatus.ONLINE

    assert store.mark_all_offline() == 1
    assert store.get(node.node_id).status is NodeStatus.OFFLINE
    assert store.mark_all_offline() == 0          # idempotent


def test_unknown_capabilities_are_ignored_not_fatal(agents) -> None:
    """A newer node advertising something we don't know should still connect."""
    caps = parse_capabilities(["EXECUTE", "TIME_TRAVEL", "desktop"])
    assert caps == {NodeCapability.EXECUTE, NodeCapability.DESKTOP}


# --- placement ------------------------------------------------------------


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def close(self) -> None:
        pass


async def test_placement_picks_a_node_with_the_capability(agents) -> None:
    store.init_db()
    gw = NodeGateway()

    executor, _ = _enrol("executor", (NodeCapability.EXECUTE,))
    reporter, _ = _enrol("reporter", (NodeCapability.REPORT,))
    await gw.attach(executor.node_id, FakeSocket())
    await gw.attach(reporter.node_id, FakeSocket())

    chosen = gw.select(NodeCapability.EXECUTE)
    assert chosen is not None and chosen.node_id == executor.node_id

    assert gw.select(NodeCapability.INFERENCE) is None      # nobody can
    assert gw.select(NodeCapability.REPORT).node_id == reporter.node_id


async def test_untrusted_work_refuses_a_node_without_consent(agents) -> None:
    """The safety property: no silent fallback onto a trusted machine."""
    store.init_db()
    gw = NodeGateway()
    node, _ = _enrol("plain", (NodeCapability.EXECUTE,))
    await gw.attach(node.node_id, FakeSocket())

    assert gw.select(NodeCapability.EXECUTE) is not None
    assert gw.select(NodeCapability.EXECUTE, untrusted=True) is None

    store.set_untrusted_ok(node.node_id, True)
    assert gw.select(NodeCapability.EXECUTE, untrusted=True) is not None


async def test_disconnected_nodes_are_not_selected(agents) -> None:
    store.init_db()
    gw = NodeGateway()
    node, _ = _enrol("worker", (NodeCapability.EXECUTE,))
    await gw.attach(node.node_id, FakeSocket())
    assert gw.select(NodeCapability.EXECUTE) is not None

    await gw.detach(node.node_id)
    assert gw.select(NodeCapability.EXECUTE) is None
    assert store.get(node.node_id).status is NodeStatus.OFFLINE


# --- dispatch -------------------------------------------------------------


async def test_dispatch_round_trip(agents) -> None:
    import asyncio

    store.init_db()
    gw = NodeGateway()
    node, _ = _enrol("worker", (NodeCapability.EXECUTE,))
    socket = FakeSocket()
    await gw.attach(node.node_id, socket)

    async def respond() -> None:
        # Wait for the dispatch to land, then answer it like a real node would.
        for _ in range(50):
            if socket.sent:
                break
            await asyncio.sleep(0.01)
        request_id = socket.sent[-1]["request_id"]
        gw.resolve(request_id, {"ok": True, "output": "hello from the node", "error": ""})

    asyncio.create_task(respond())
    result = await gw.dispatch(
        tool="shell.exec", arguments={"command": "echo hi"},
        capability=NodeCapability.EXECUTE, timeout=5,
    )

    assert result["ok"] is True
    assert result["output"] == "hello from the node"
    assert socket.sent[-1]["tool"] == "shell.exec"
    # The node is released again once the call returns.
    assert store.get(node.node_id).current_task_id is None


async def test_dispatch_without_a_node_is_reported_not_faked(agents) -> None:
    store.init_db()
    gw = NodeGateway()

    with pytest.raises(NoNodeAvailable, match="no connected node"):
        await gw.dispatch(
            tool="shell.exec", arguments={}, capability=NodeCapability.EXECUTE, timeout=1
        )


async def test_dispatch_times_out_rather_than_hanging(agents) -> None:
    store.init_db()
    gw = NodeGateway()
    node, _ = _enrol("silent", (NodeCapability.EXECUTE,))
    await gw.attach(node.node_id, FakeSocket())          # never answers

    with pytest.raises(NoNodeAvailable, match="did not answer"):
        await gw.dispatch(
            tool="shell.exec", arguments={}, capability=NodeCapability.EXECUTE, timeout=0.2
        )
    assert store.get(node.node_id).current_task_id is None


async def test_heartbeat_updates_metrics(agents) -> None:
    store.init_db()
    gw = NodeGateway()
    node, _ = _enrol("worker")
    await gw.attach(node.node_id, FakeSocket())

    reply = await gw.handle_message(node.node_id, {
        "type": "heartbeat", "metrics": {"cpu": 12.5, "ram_used_mb": 2048},
    })
    assert reply == {"type": "heartbeat_ack"}
    assert store.get(node.node_id).metrics["cpu"] == 12.5


async def test_unknown_message_type_is_answered_not_ignored(agents) -> None:
    store.init_db()
    gw = NodeGateway()
    node, _ = _enrol()
    reply = await gw.handle_message(node.node_id, {"type": "nonsense"})
    assert reply["type"] == "error"


# --- the websocket + API --------------------------------------------------


def test_node_joins_reconnects_and_is_rejected_on_a_bad_secret(client) -> None:
    minted = client.post("/api/v1/nodes/enrol", json={"label": "bench"})
    assert minted.status_code == 201
    token = minted.json()["enrolment_token"]

    # First contact: join with the one-time token.
    with client.websocket_connect("/ws/node") as ws:
        ws.send_text(json.dumps({
            "type": "hello", "enrolment_token": token, "name": "bench-01",
            "platform": "linux", "capabilities": ["EXECUTE", "DESKTOP"],
            "ram_mb": 16384, "cores": 10,
        }))
        welcome = json.loads(ws.receive_text())

    assert welcome["type"] == "welcome"
    node_id, secret = welcome["node_id"], welcome["secret"]

    listing = client.get("/api/v1/nodes").json()
    assert listing["count"] == 1
    entry = listing["nodes"][0]
    assert entry["name"] == "bench-01"
    assert set(entry["capabilities"]) == {"EXECUTE", "DESKTOP"}
    assert entry["ram_mb"] == 16384

    # Reconnect with the issued credentials.
    with client.websocket_connect("/ws/node") as ws:
        ws.send_text(json.dumps({"type": "hello", "node_id": node_id, "secret": secret}))
        again = json.loads(ws.receive_text())
    assert again["type"] == "welcome" and "secret" not in again

    # A wrong secret is refused.
    with client.websocket_connect("/ws/node") as ws:
        ws.send_text(json.dumps({"type": "hello", "node_id": node_id, "secret": "wrong"}))
        assert json.loads(ws.receive_text())["type"] == "error"


def test_reused_enrolment_token_is_refused(client) -> None:
    token = client.post("/api/v1/nodes/enrol", json={}).json()["enrolment_token"]

    with client.websocket_connect("/ws/node") as ws:
        ws.send_text(json.dumps({"type": "hello", "enrolment_token": token, "name": "a"}))
        assert json.loads(ws.receive_text())["type"] == "welcome"

    with client.websocket_connect("/ws/node") as ws:
        ws.send_text(json.dumps({"type": "hello", "enrolment_token": token, "name": "b"}))
        assert json.loads(ws.receive_text())["type"] == "error"

    assert client.get("/api/v1/nodes").json()["count"] == 1


def test_hello_without_credentials_is_refused(client) -> None:
    with client.websocket_connect("/ws/node") as ws:
        ws.send_text(json.dumps({"type": "hello", "name": "impostor"}))
        assert json.loads(ws.receive_text())["type"] == "error"
    assert client.get("/api/v1/nodes").json()["count"] == 0


def test_first_message_must_be_hello(client) -> None:
    with client.websocket_connect("/ws/node") as ws:
        ws.send_text(json.dumps({"type": "heartbeat"}))
        assert json.loads(ws.receive_text())["type"] == "error"


def test_fleet_api_trust_and_revoke(client) -> None:
    token = client.post("/api/v1/nodes/enrol", json={}).json()["enrolment_token"]
    with client.websocket_connect("/ws/node") as ws:
        ws.send_text(json.dumps({"type": "hello", "enrolment_token": token, "name": "sandbox"}))
        node_id = json.loads(ws.receive_text())["node_id"]

    trusted = client.post(f"/api/v1/nodes/{node_id}/trust", json={"untrusted_ok": True})
    assert trusted.status_code == 200
    assert client.get("/api/v1/nodes").json()["nodes"][0]["untrusted_ok"] is True

    assert client.post("/api/v1/nodes/node_nope/trust", json={"untrusted_ok": True}).status_code == 404
    assert client.delete(f"/api/v1/nodes/{node_id}").status_code == 200
    assert client.get("/api/v1/nodes").json()["count"] == 0


def test_fleet_routes_require_auth(client) -> None:
    for path in ("/api/v1/nodes",):
        assert client.get(path, headers={"X-Jarvis-Passcode": "wrong"}).status_code == 401
    assert client.post(
        "/api/v1/nodes/enrol", json={}, headers={"X-Jarvis-Passcode": "wrong"}
    ).status_code == 401


# --- agent -> gateway -> node, end to end --------------------------------


async def test_agent_tool_call_is_routed_to_a_node(agents) -> None:
    """The whole path: an agent asks for a node tool, a node runs it."""
    import asyncio

    from app.agents.runner import AgentRunner
    from app.nodes import gateway as gateway_module
    from tests.conftest import scripted_router, text_result, tool_result

    store.init_db()
    gw = NodeGateway()
    node, _ = _enrol("bench-01", (NodeCapability.EXECUTE,))
    socket = FakeSocket()
    await gw.attach(node.node_id, socket)

    # The runner reaches for the module-level gateway.
    original = gateway_module.gateway
    gateway_module.gateway = gw
    try:
        async def respond() -> None:
            for _ in range(100):
                if socket.sent:
                    break
                await asyncio.sleep(0.01)
            gw.resolve(socket.sent[-1]["request_id"], {
                "ok": True,
                "output": '{"path": "/home/dp/jarvis-workspace", "entries": []}',
                "error": "",
            })

        router, _engine = scripted_router([
            tool_result("fs.list", {"path": "~/jarvis-workspace"}),
            text_result("That workspace is empty."),
        ])
        asyncio.create_task(respond())
        result = await AgentRunner(router).run(
            agents.get("operator"), objective="What is in my workspace?",
            mission_id="msn_secret",
        )
    finally:
        gateway_module.gateway = original

    assert result.text == "That workspace is empty."
    assert socket.sent[-1]["tool"] == "fs.list"
    # Orchestrator internals are injected before dispatch and must be stripped
    # at the boundary -- the node's tool signatures would reject them.
    sent_args = socket.sent[-1]["arguments"]
    assert sent_args == {"path": "~/jarvis-workspace"}
    assert "_mission_id" not in sent_args and "_agent_id" not in sent_args
    assert "jarvis-workspace" in result.tool_log[0]["output"]


async def test_node_tool_without_a_node_reports_unavailable(agents) -> None:
    """No silent local fallback -- the agent is told the fleet cannot do it."""
    from app.agents.runner import AgentRunner
    from app.nodes import gateway as gateway_module
    from tests.conftest import scripted_router, text_result, tool_result

    store.init_db()
    gw = NodeGateway()
    original = gateway_module.gateway
    gateway_module.gateway = gw
    try:
        router, _ = scripted_router([
            tool_result("system.stats", {}),
            text_result("No machine is connected right now."),
        ])
        result = await AgentRunner(router).run(
            agents.get("operator"), objective="How is the laptop doing?"
        )
    finally:
        gateway_module.gateway = original

    assert "[unavailable:" in result.tool_log[0]["output"]
    assert "no connected node" in result.tool_log[0]["output"]


def test_dangerous_node_tools_require_approval(agents) -> None:
    """Running commands and deleting files on real hardware always parks."""
    from app.tools.registry import Capability, Risk, check_permission, registry

    for name in ("shell.exec", "fs.delete"):
        tool = registry.get(name)
        assert tool.risk is Risk.DANGEROUS, name
        verdict = check_permission(
            tool,
            granted_capabilities={Capability.READ, Capability.WRITE, Capability.EXECUTE},
            allowed_tools=[name],
        )
        assert verdict.allowed and verdict.needs_approval, name


def test_operator_agent_is_wired_to_node_tools(agents) -> None:
    operator = agents.get("operator")
    assert operator is not None
    assert {"fs.list", "shell.exec", "system.stats"} <= set(operator.tools)


# --- the dispatch endpoint ------------------------------------------------


def test_dispatch_endpoint_rejects_non_node_tools(client) -> None:
    response = client.post("/api/v1/nodes/dispatch", json={"tool": "memory.read", "arguments": {}})
    assert response.status_code == 400
    assert "not a node tool" in response.json()["detail"]


def test_dispatch_endpoint_reports_no_node(client) -> None:
    """503, not a fabricated result, when the fleet is empty."""
    response = client.post("/api/v1/nodes/dispatch", json={"tool": "system.stats", "arguments": {}})
    assert response.status_code == 503
    assert "no connected node" in response.json()["detail"]


def test_dispatch_endpoint_requires_auth(client) -> None:
    response = client.post(
        "/api/v1/nodes/dispatch",
        json={"tool": "system.stats"},
        headers={"X-Jarvis-Passcode": "wrong"},
    )
    assert response.status_code == 401


# --- node-side safety boundaries -----------------------------------------
#
# These exercise the agent's own enforcement, which is what actually stands
# between a model and the filesystem. Verified live against a running node:
# `rm -rf /` refused by the allowlist, /etc/passwd and ../ traversal both
# refused by the path jail.


def _tools(tmp_path, allow=("echo",)):
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "jarvis_node_agent", "nodes/pc/agent.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["jarvis_node_agent"] = module
    spec.loader.exec_module(module)
    return module, module.NodeTools([tmp_path], list(allow))


def test_node_path_jail_blocks_absolute_escape(tmp_path) -> None:
    module, tools = _tools(tmp_path)
    with pytest.raises(module.ToolError, match="outside the allowed roots"):
        tools.fs_read("/etc/passwd")


def test_node_path_jail_blocks_traversal(tmp_path) -> None:
    """`../` must be resolved before the check, not after."""
    module, tools = _tools(tmp_path)
    with pytest.raises(module.ToolError, match="outside the allowed roots"):
        tools.fs_read(str(tmp_path / ".." / ".." / "etc" / "passwd"))


def test_node_shell_allowlist(tmp_path) -> None:
    module, tools = _tools(tmp_path, allow=("echo",))
    assert "exit_code: 0" in tools.shell_exec("echo hi")
    with pytest.raises(module.ToolError, match="not in this node's shell allowlist"):
        tools.shell_exec("rm -rf /")


def test_node_read_write_round_trip(tmp_path) -> None:
    _module, tools = _tools(tmp_path)
    tools.fs_write(str(tmp_path / "note.txt"), "written by the node")
    assert tools.fs_read(str(tmp_path / "note.txt")) == "written by the node"

    listing = json.loads(tools.fs_list(str(tmp_path)))
    assert any(e["name"] == "note.txt" for e in listing["entries"])


def test_node_refuses_to_delete_a_directory(tmp_path) -> None:
    module, tools = _tools(tmp_path)
    (tmp_path / "sub").mkdir()
    with pytest.raises(module.ToolError, match="refusing to delete a directory"):
        tools.fs_delete(str(tmp_path / "sub"))


def test_node_unknown_tool_is_reported(tmp_path) -> None:
    module, tools = _tools(tmp_path)
    with pytest.raises(module.ToolError, match="does not implement"):
        tools.run("does.not.exist", {})
