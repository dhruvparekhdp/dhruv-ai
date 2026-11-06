"""Deny-by-default tool execution and exact-action approvals.

The handler runs only from `execute_tool`. An approval is a stored row bound
to the hash of one call, and that row has to still match when the handler
is about to run.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from app.agents.runner import AgentRunner
from app.missions.models import ApprovalDecision
from app.missions.scheduler import MissionScheduler
from app.tools.executor import execute_tool
from app.tools.policy import PolicyLevel, action_hash
from app.tools.registry import Capability, check_permission, registry
from tests.conftest import scripted_router, text_result, tool_result

MISSION = "msn_policy"
ARGS = {"scope": "user", "key": "city"}


def _grants(*levels: Capability) -> set[Capability]:
    return set(levels)


async def _call(
    name: str,
    arguments: dict[str, Any],
    *,
    allowed: list[str],
    granted: set[Capability],
    mission_id: str = MISSION,
    approval_id: str | None = None,
    agent_id: str = "memory",
) -> str:
    return await execute_tool(
        name,
        arguments,
        granted_capabilities=granted,
        allowed_tools=allowed,
        mission_id=mission_id,
        agent_id=agent_id,
        approval_id=approval_id,
    )


def _approve(store: Any, name: str, arguments: dict[str, Any], **extra: Any) -> str:
    approval_id = store.create_approval(
        mission_id=MISSION,
        task_id="task_policy",
        agent_id="memory",
        tool_name=name,
        tool_arguments=arguments,
        risk="dangerous",
        **extra,
    )
    assert store.decide_approval(approval_id, ApprovalDecision.APPROVED, decided_by="owner")
    return approval_id


class _Spy:
    def __init__(self, name: str) -> None:
        self.calls: list[dict[str, Any]] = []
        self._original = registry.get(name)
        assert self._original is not None

        async def handler(**kwargs: Any) -> str:
            self.calls.append(kwargs)
            return "ran"

        registry._tools[name] = replace(self._original, handler=handler)

    def restore(self) -> None:
        assert self._original is not None
        registry._tools[self._original.name] = self._original


# --- registration and levels ---------------------------------------------


async def test_unregistered_tool_is_denied(agents) -> None:
    result = await _call(
        "does.not.exist",
        {},
        allowed=["does.not.exist"],
        granted=_grants(Capability.READ, Capability.WRITE, Capability.EXECUTE),
    )
    assert result.startswith("[denied:")
    assert "not registered" in result


def test_external_and_dangerous_need_approval_internal_write_does_not(agents) -> None:
    fetch = registry.get("web.fetch")
    forget = registry.get("memory.forget")
    write = registry.get("memory.write")
    assert fetch is not None and fetch.policy is PolicyLevel.EXTERNAL
    assert forget is not None and forget.policy is PolicyLevel.DANGEROUS
    assert write is not None and write.policy is PolicyLevel.INTERNAL_WRITE

    external = check_permission(
        fetch, granted_capabilities={Capability.READ}, allowed_tools=["web.fetch"]
    )
    dangerous = check_permission(
        forget, granted_capabilities={Capability.WRITE}, allowed_tools=["memory.forget"]
    )
    internal = check_permission(
        write, granted_capabilities={Capability.WRITE}, allowed_tools=["memory.write"]
    )
    assert external.needs_approval and dangerous.needs_approval
    assert internal.allowed and not internal.needs_approval


def test_every_registered_tool_declares_a_policy_level(agents) -> None:
    assert registry.names()
    for name in registry.names():
        tool = registry.get(name)
        assert tool is not None
        assert isinstance(tool.policy, PolicyLevel)


def test_agents_cannot_call_each_other_and_dispatch_is_the_scheduler(agents) -> None:
    """An agent id is not a tool, and the runner does not call handlers itself."""
    assert set(agents.ids()).isdisjoint(registry.names())
    invoke = inspect.getsource(AgentRunner._invoke)
    assert "execute_tool(" in invoke
    assert "tool.handler" not in invoke
    assert "self.runner.run(" in inspect.getsource(MissionScheduler._execute_task)


async def test_runner_reaches_tools_only_through_the_executor(agents, monkeypatch) -> None:
    async def fake_execute(*_args: Any, **_kwargs: Any) -> str:
        return "from-executor"

    monkeypatch.setattr("app.agents.runner.execute_tool", fake_execute)
    router, _ = scripted_router([
        tool_result("memory.read", {"scope": "user"}),
        text_result("ok"),
    ])
    result = await AgentRunner(router).run(agents.get("memory"), objective="Read it.")
    assert result.tool_log[0]["output"] == "from-executor"


# --- the handler does not run without a matching approval ----------------


async def test_external_tool_is_blocked_without_approval(agents) -> None:
    spy = _Spy("web.fetch")
    try:
        result = await _call(
            "web.fetch",
            {"url": "https://example.com"},
            allowed=["web.fetch"],
            granted=_grants(Capability.READ),
            agent_id="research",
        )
    finally:
        spy.restore()
    assert "requires approval" in result
    assert spy.calls == []


async def test_dangerous_tool_is_blocked_without_approval(agents) -> None:
    from app.memory.store import read, write

    write("user", "city", "Pune")
    result = await _call(
        "memory.forget",
        ARGS,
        allowed=["memory.forget"],
        granted=_grants(Capability.WRITE),
    )
    assert "requires approval" in result
    assert read("user", "city")[0]["value"] == "Pune"


async def test_changed_params_do_not_match_the_approval(agents) -> None:
    from app.missions import store

    spy = _Spy("memory.forget")
    try:
        approval_id = _approve(store, "memory.forget", ARGS)
        result = await _call(
            "memory.forget",
            {"scope": "user", "key": "town"},
            allowed=["memory.forget"],
            granted=_grants(Capability.WRITE),
            approval_id=approval_id,
        )
        record = store.get_approval(approval_id)
    finally:
        spy.restore()

    assert result == "[denied: approval was for a different action]"
    assert spy.calls == []
    assert record is not None and record["consumed_at"] is None


async def test_an_approval_is_single_use(agents) -> None:
    from app.missions import store

    spy = _Spy("memory.forget")
    try:
        approval_id = _approve(store, "memory.forget", ARGS)
        first = await _call(
            "memory.forget", ARGS,
            allowed=["memory.forget"], granted=_grants(Capability.WRITE),
            approval_id=approval_id,
        )
        second = await _call(
            "memory.forget", ARGS,
            allowed=["memory.forget"], granted=_grants(Capability.WRITE),
            approval_id=approval_id,
        )
    finally:
        spy.restore()

    assert first == "ran"
    assert second == "[denied: approval was already used]"
    assert len(spy.calls) == 1


async def test_an_expired_approval_is_rejected(agents) -> None:
    from app.missions import store

    spy = _Spy("web.fetch")
    arguments = {"url": "https://example.com/one"}
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    try:
        approval_id = _approve(store, "web.fetch", arguments, expires_at=past)
        result = await _call(
            "web.fetch", arguments,
            allowed=["web.fetch"], granted=_grants(Capability.READ),
            approval_id=approval_id, agent_id="research",
        )
        record = store.get_approval(approval_id)
    finally:
        spy.restore()

    assert result == "[denied: approval has expired]"
    assert spy.calls == []
    assert record is not None and record["consumed_at"] is None


async def test_approval_record_survives_a_fresh_read(agents) -> None:
    """The approval lives in the database. A new connection sees the same row."""
    from app.core.db import fetch_one, get_conn
    from app.missions import store

    approval_id = _approve(store, "memory.forget", ARGS)
    # Drop the object from this scope's point of view and read through a
    # connection opened for this query, which is what a restarted process does.
    with get_conn() as conn:
        raw = fetch_one(conn, "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,))
    assert raw is not None
    assert raw["mission_id"] == MISSION
    assert raw["decision"] == "APPROVED"
    assert raw["decided_by"] == "owner"
    assert raw["decided_at"] is not None
    assert raw["params_hash"] == action_hash("memory.forget", ARGS)

    spy = _Spy("memory.forget")
    try:
        result = await _call(
            "memory.forget", ARGS,
            allowed=["memory.forget"], granted=_grants(Capability.WRITE),
            approval_id=approval_id,
        )
    finally:
        spy.restore()
    assert result == "ran"
    assert len(spy.calls) == 1


def test_new_approvals_expire_after_fifteen_minutes(agents) -> None:
    from app.missions import store

    before = datetime.now(UTC)
    approval_id = store.create_approval(
        mission_id=MISSION, task_id="task_policy", agent_id="memory",
        tool_name="memory.forget", tool_arguments=ARGS, risk="dangerous",
    )
    after = datetime.now(UTC)
    record = store.get_approval(approval_id)
    assert record is not None
    expires = datetime.fromisoformat(str(record["expires_at"]).replace(" ", "T"))
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    assert before + timedelta(minutes=15) - timedelta(seconds=2) <= expires
    assert expires <= after + timedelta(minutes=15) + timedelta(seconds=2)


def test_older_approval_table_gains_the_hash_columns(agents) -> None:
    """A database created before this ticket still accepts a bound approval."""
    from app.core.db import ddl_types, execute, get_conn
    from app.missions import store

    types = ddl_types()
    with get_conn() as conn:
        execute(conn, "DROP TABLE IF EXISTS approvals")
        execute(
            conn,
            f"""
            CREATE TABLE approvals (
                approval_id TEXT PRIMARY KEY,
                mission_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                created_at {types["ts"]},
                decided_at {types["ts"]},
                agent_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                tool_arguments {types["json"]},
                risk TEXT NOT NULL,
                decision TEXT NOT NULL,
                decided_by TEXT DEFAULT ''
            )
            """,
        )
    store.init_db()
    approval_id = _approve(store, "shell.exec", {"command": "echo hi"})
    record = store.get_approval(approval_id)
    assert record is not None
    assert record["params_hash"] == action_hash("shell.exec", {"command": "echo hi"})
    assert record["expires_at"] is not None
    assert record["consumed_at"] is None
