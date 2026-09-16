"""The turn pipeline -- Jarvis's core loop.

    classify intent -> select engine -> complete -> log trajectory -> broadcast

This is a linear state machine rather than a LangGraph graph. Phase 1 has no
branching, no tool calls, and no loops, so a graph library would be ceremony
around a straight line. The seams that later phases need -- an intent node, an
engine node, a telemetry interceptor around both -- are already separated, so
swapping in LangGraph when the coding and trading subsystems add real cycles is
a contained change.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.agent import prompts
from app.agent import router as intent_router
from app.agent.engines import EngineRouter
from app.core.telemetry import telemetry
from app.services.websocket_manager import ws_manager

log = logging.getLogger("jarvis.orchestrator")

# Appended when the router recognises an intent this build cannot execute, so
# the model is told about its own missing subsystems rather than improvising.
_UNSUPPORTED_NOTE = """

IMPORTANT: the user's request was classified as {intent}, a subsystem that is \
not built yet in this phase. Tell them plainly that this capability is not \
wired up, name what phase it belongs to, and do not claim to have performed \
any action."""


@dataclass
class TurnResult:
    trajectory_id: str
    intent: str
    response: str
    provider: str
    model: str
    latency_ms: int
    degraded: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0


class Orchestrator:
    def __init__(self, engine_router: EngineRouter | None = None) -> None:
        self.engines = engine_router or EngineRouter()

    async def run_turn(
        self,
        *,
        session_id: str,
        user_prompt: str,
        system_prompt: str | None = None,
        intent_override: str | None = None,
    ) -> TurnResult:
        """Execute one complete turn, logging it whether it succeeds or fails."""
        started = time.perf_counter()

        intent = intent_override or intent_router.classify_intent(user_prompt)
        await ws_manager.broadcast(
            "orchestrator",
            f"Received command -> intent: {intent}",
            intent=intent,
            session_id=session_id,
        )

        effective_prompt = system_prompt or prompts.BASE_SYSTEM_PROMPT
        if not intent_router.is_supported(intent):
            effective_prompt += _UNSUPPORTED_NOTE.format(intent=intent)

        try:
            result = self.engines.complete(
                intent=intent,
                system_prompt=effective_prompt,
                user_prompt=user_prompt,
            )
        except Exception as exc:  # noqa: BLE001 - logged as a failed trajectory
            latency_ms = int((time.perf_counter() - started) * 1000)
            log.exception("turn failed for session %s", session_id)
            telemetry.log_trajectory(
                session_id=session_id,
                intent_category=intent,
                user_prompt=user_prompt,
                system_prompt=effective_prompt,
                model_provider="NONE",
                model_name="none",
                agent_reasoning=f"Execution failed: {exc}",
                raw_llm_response="",
                latency_ms=latency_ms,
                execution_success=False,
                error_message=str(exc),
            )
            await ws_manager.broadcast("error", f"Turn failed: {exc}", session_id=session_id)
            raise

        latency_ms = int((time.perf_counter() - started) * 1000)

        trajectory_id = telemetry.log_trajectory(
            session_id=session_id,
            intent_category=intent,
            user_prompt=user_prompt,
            system_prompt=effective_prompt,
            model_provider=result.provider,
            model_name=result.model,
            agent_reasoning=result.reasoning,
            raw_llm_response=result.text,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            latency_ms=latency_ms,
            ttft_ms=result.ttft_ms,
            execution_success=True,
        )

        await ws_manager.broadcast(
            "telemetry",
            f"Trajectory logged via {result.provider} in {latency_ms}ms",
            trajectory_id=trajectory_id,
            provider=result.provider,
            model=result.model,
            latency_ms=latency_ms,
            tokens=result.prompt_tokens + result.completion_tokens,
        )

        return TurnResult(
            trajectory_id=trajectory_id,
            intent=intent,
            response=result.text,
            provider=result.provider,
            model=result.model,
            latency_ms=latency_ms,
            degraded=result.provider == "ECHO",
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )

    async def run_mood_checkin(
        self, *, session_id: str, score: int, note: str = ""
    ) -> tuple[TurnResult, str]:
        """Log a mood check-in and generate Jarvis's response to it."""
        label = prompts.mood_label(score)
        user_prompt = prompts.build_mood_prompt(score, note)

        await ws_manager.broadcast(
            "mood", f"Mood check-in: {score}/5 ({label})", score=score, session_id=session_id
        )

        turn = await self.run_turn(
            session_id=session_id,
            user_prompt=user_prompt,
            system_prompt=prompts.MOOD_SYSTEM_PROMPT,
            intent_override=intent_router.PERSONAL_CHAT,
        )

        checkin_id = telemetry.log_mood(
            session_id=session_id,
            mood_score=score,
            mood_label=label,
            note=note,
            trajectory_id=turn.trajectory_id,
        )
        return turn, checkin_id


orchestrator = Orchestrator()
