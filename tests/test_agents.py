"""Agent registry, runner, and permission enforcement.

These are the safety properties the whole multi-agent design rests on, so they
are tested against the real registry and the real tools -- only the model is a
double.
"""

from __future__ import annotations

import pytest

from app.agents.registry import AgentDefinition, AgentDefinitionError, AgentRegistry
from app.agents.runner import AgentRunner
from app.missions.models import Budget
from app.tools.registry import Capability, Risk, check_permission, registry as tool_registry
from tests.conftest import scripted_router, text_result, tool_result

# asyncio_mode=auto in pytest.ini runs async tests without per-test markers.


# --- registry validation --------------------------------------------------


def test_definitions_load_and_validate(agents) -> None:
    assert set(agents.ids()) == {"assistant", "memory", "operator", "planner", "research"}
    research = agents.get("research")
    assert "web.fetch" in research.tools
    assert Capability.READ in research.capabilities


def test_unknown_tool_is_rejected_at_load(agents, tmp_path) -> None:
    (tmp_path / "bad.yaml").write_text(
        "id: bad\nname: Bad\nsystem_prompt: hi\ntools: [does.not.exist]\ncapabilities: [READ]\n"
    )
    with pytest.raises(AgentDefinitionError, match="unknown tool"):
        AgentRegistry().load(tmp_path)


def test_tool_without_matching_capability_is_rejected(agents, tmp_path) -> None:
    """The dangerous mismatch: an agent listing a tool it could never call."""
    (tmp_path / "bad.yaml").write_text(
        "id: bad\nname: Bad\nsystem_prompt: hi\ntools: [memory.write]\ncapabilities: [READ]\n"
    )
    with pytest.raises(AgentDefinitionError, match="not granted"):
        AgentRegistry().load(tmp_path)


def test_planner_is_excluded_from_its_own_roster(agents) -> None:
    roster = agents.roster_for_planner()
    assert "planner:" not in roster
    assert "research:" in roster


# --- permission enforcement ----------------------------------------------


def test_permission_requires_both_allowlist_and_capability(agents) -> None:
    write_tool = tool_registry.get("memory.write")

    denied_by_list = check_permission(
        write_tool, granted_capabilities={Capability.WRITE}, allowed_tools=["memory.read"]
    )
    assert not denied_by_list.allowed

    denied_by_capability = check_permission(
        write_tool, granted_capabilities={Capability.READ}, allowed_tools=["memory.write"]
    )
    assert not denied_by_capability.allowed

    allowed = check_permission(
        write_tool, granted_capabilities={Capability.WRITE}, allowed_tools=["memory.write"]
    )
    assert allowed.allowed and not allowed.needs_approval


def test_dangerous_tool_always_needs_approval(agents) -> None:
    forget = tool_registry.get("memory.forget")
    assert forget.risk is Risk.DANGEROUS
    verdict = check_permission(
        forget, granted_capabilities={Capability.WRITE}, allowed_tools=["memory.forget"]
    )
    assert verdict.allowed and verdict.needs_approval


# --- the runner loop ------------------------------------------------------


async def test_runner_executes_a_tool_then_answers(agents) -> None:
    router, engine = scripted_router([
        tool_result("memory.write", {"scope": "user", "key": "city", "value": "Pune"}),
        text_result("Noted that you're in Pune."),
    ])
    runner = AgentRunner(router)

    result = await runner.run(
        agents.get("memory"), objective="Remember I live in Pune.", mission_id="msn_test"
    )

    assert result.text == "Noted that you're in Pune."
    assert result.tool_calls == 1
    assert result.tool_log[0]["tool"] == "memory.write"

    # The tool actually ran -- the value is retrievable.
    from app.memory.store import read
    assert read("user", "city")[0]["value"] == "Pune"


async def test_runner_refuses_tool_the_agent_lacks(agents) -> None:
    """The assistant may read memory but not write it."""
    router, _ = scripted_router([
        tool_result("memory.write", {"scope": "user", "key": "x", "value": "y"}),
        text_result("I can't store that."),
    ])
    runner = AgentRunner(router)

    result = await runner.run(agents.get("assistant"), objective="Store something.")

    assert result.text == "I can't store that."
    assert "[denied:" in result.tool_log[0]["output"]

    from app.memory.store import read as read_memory
    assert read_memory("user", "x") == []      # nothing was written


async def test_dangerous_tool_parks_and_is_denied_without_a_hook(agents) -> None:
    router, _ = scripted_router([
        tool_result("memory.forget", {"scope": "user", "key": "city"}),
        text_result("I could not delete it."),
    ])
    runner = AgentRunner(router)

    result = await runner.run(agents.get("memory"), objective="Forget my city.")

    assert "requires human approval" in result.tool_log[0]["output"]


async def test_dangerous_tool_runs_once_approved(agents) -> None:
    from app.memory.store import read as read_memory, write as write_memory

    write_memory("user", "city", "Pune")

    router, _ = scripted_router([
        tool_result("memory.forget", {"scope": "user", "key": "city"}),
        text_result("Deleted."),
    ])
    runner = AgentRunner(router)

    async def approve(tool_name: str, arguments: dict) -> bool:
        return True

    result = await runner.run(
        agents.get("memory"), objective="Forget my city.", approval_hook=approve
    )

    assert result.text == "Deleted."
    assert read_memory("user", "city") == []


async def test_denied_approval_leaves_data_intact(agents) -> None:
    from app.memory.store import read as read_memory, write as write_memory

    write_memory("user", "city", "Pune")

    router, _ = scripted_router([
        tool_result("memory.forget", {"scope": "user", "key": "city"}),
        text_result("Left it alone."),
    ])
    runner = AgentRunner(router)

    async def deny(tool_name: str, arguments: dict) -> bool:
        return False

    await runner.run(agents.get("memory"), objective="Forget my city.", approval_hook=deny)

    assert read_memory("user", "city")[0]["value"] == "Pune"


async def test_tool_call_budget_is_enforced(agents) -> None:
    """A model that keeps calling tools must be cut off, not followed forever."""
    definition = agents.get("memory")
    tight = AgentDefinition(
        id=definition.id, name=definition.name, description=definition.description,
        system_prompt=definition.system_prompt, tools=definition.tools,
        capabilities=definition.capabilities, model_preference=definition.model_preference,
        budget=Budget(max_tool_calls=2),
    )

    script = [tool_result("memory.read", {"scope": "user"}, call_id=f"c{i}") for i in range(6)]
    script.append(text_result("finally done"))
    router, _ = scripted_router(script)

    result = await AgentRunner(router).run(tight, objective="Read memory repeatedly.")

    assert result.tool_calls == 2      # capped, not 6


async def test_runner_stops_after_max_iterations(agents) -> None:
    """A model that never stops calling tools still terminates."""
    from app.agents import runner as runner_module

    script = [
        tool_result("memory.read", {"scope": "user"}, call_id=f"c{i}")
        for i in range(runner_module.MAX_ITERATIONS + 3)
    ]
    router, _ = scripted_router(script)

    result = await AgentRunner(router).run(agents.get("memory"), objective="Loop forever.")

    assert result.iterations <= runner_module.MAX_ITERATIONS
    assert "too many tool-calling rounds" in result.text


async def test_unknown_tool_name_does_not_crash_the_task(agents) -> None:
    router, _ = scripted_router([
        tool_result("does.not.exist", {}),
        text_result("recovered"),
    ])
    result = await AgentRunner(router).run(agents.get("memory"), objective="Try a bad tool.")
    assert result.text == "recovered"
    assert "unknown tool" in result.tool_log[0]["output"]


async def test_context_is_scoped_to_the_task(agents) -> None:
    """Dependency results are passed explicitly, never inherited globally."""
    router, engine = scripted_router([text_result("ok")])

    await AgentRunner(router).run(
        agents.get("assistant"),
        objective="Summarise the finding.",
        context="[research] The answer is 42.",
    )

    user_message = engine.calls[0]["messages"][-1]["content"]
    assert "The answer is 42" in user_message
    assert "Results from earlier tasks" in user_message


async def test_agent_is_never_offered_tools_it_cannot_use(agents) -> None:
    router, engine = scripted_router([text_result("ok")])
    await AgentRunner(router).run(agents.get("assistant"), objective="hi")

    offered = {t["function"]["name"] for t in (engine.calls[0]["tools"] or [])}
    assert offered == {"memory.search", "memory.read"}
    assert "memory.forget" not in offered
