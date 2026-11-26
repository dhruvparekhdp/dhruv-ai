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

from app.agent import prompts, retrieval
from app.agent import router as intent_router
from app.agent.engines import EngineRouter
from app.agent.retrieval import Retrieval
from app.core import usage as usage_ledger
from app.core.telemetry import telemetry
from app.knowledge.store import DocContext, retrieve_for_chat
from app.services.websocket_manager import ws_manager

log = logging.getLogger("jarvis.orchestrator")

MODES = ("fast", "research", "local")
_HISTORY_TURNS = 8

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
    mode: str = "fast"
    method: str = ""
    evidence: list[dict[str, str]] | None = None


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
        mode: str = "fast",
    ) -> TurnResult:
        """Execute one complete turn, logging it whether it succeeds or fails."""
        started = time.perf_counter()
        if mode not in MODES:
            mode = "fast"

        intent = intent_override or intent_router.classify_intent(user_prompt)
        await ws_manager.broadcast(
            "orchestrator",
            f"Received command -> intent: {intent}",
            intent=intent,
            session_id=session_id,
        )

        # Mood check-ins pass their own prompt and must not search the web.
        gathered = Retrieval()
        docs = DocContext()
        if system_prompt is None:
            gathered = await retrieval.gather(user_prompt, local_only=(mode == "local"))
            docs = retrieve_for_chat(user_prompt)

        effective_prompt = system_prompt or prompts.BASE_SYSTEM_PROMPT
        if not intent_router.is_supported(intent):
            effective_prompt += _UNSUPPORTED_NOTE.format(intent=intent)
        if mode == "local":
            effective_prompt += (
                "\n\nThis turn is local only. Do not claim you searched the web "
                "or called Groq or Gemini."
            )
        block = retrieval.untrusted_block(gathered)
        if block:
            effective_prompt += "\n\n" + block
        doc_block = docs.prompt_block()
        if doc_block:
            effective_prompt += "\n\n" + doc_block
        if gathered.note:
            effective_prompt += "\n\n" + gathered.note
        if docs.note:
            effective_prompt += "\n\n" + docs.note

        messages = _messages(session_id, effective_prompt, user_prompt)
        prefer = "deep" if mode == "research" else "fast"

        try:
            # Chat is not a mission. It still counts toward the daily budget
            # and toward the "chat" agent budget.
            with usage_ledger.scope("", "chat"):
                result = self.engines.chat(
                    messages=messages,
                    prefer=prefer,
                    local_only=(mode == "local"),
                )
        except Exception as exc:
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
        answer = _finish_answer(
            text=result.text,
            provider=result.provider,
            prompt=user_prompt,
            intent=intent,
            gathered=gathered,
            docs=docs,
        )
        method = _method(
            provider=result.provider,
            intent=intent,
            mode=mode,
            gathered=gathered,
            docs=docs,
        )

        trajectory_id = telemetry.log_trajectory(
            session_id=session_id,
            intent_category=intent,
            user_prompt=user_prompt,
            system_prompt=effective_prompt,
            model_provider=result.provider,
            model_name=result.model,
            agent_reasoning=result.reasoning,
            raw_llm_response=answer,
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
            response=answer,
            provider=result.provider,
            model=result.model,
            latency_ms=latency_ms,
            degraded=result.provider == "ECHO",
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            mode=mode,
            method=method,
            evidence=[*gathered.evidence(), *docs.evidence()],
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


def _messages(session_id: str, system: str, user_prompt: str) -> list[dict[str, str]]:
    """Prior turns, then this prompt. The system prompt of older turns stays out."""
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    try:
        prior = telemetry.list_turns(session_id, limit=_HISTORY_TURNS)
    except Exception:  # noqa: BLE001 - a history read must not sink the turn
        prior = []
    for turn in prior:
        user = str(turn.get("user") or "")[:4000]
        assistant = str(turn.get("assistant") or "")[:4000]
        if user:
            messages.append({"role": "user", "content": user})
        if assistant:
            messages.append({"role": "assistant", "content": assistant})
    messages.append({"role": "user", "content": user_prompt})
    return messages


def _finish_answer(
    *,
    text: str,
    provider: str,
    prompt: str,
    intent: str,
    gathered: Retrieval,
    docs: DocContext,
) -> str:
    """Add citations and the refusals a fallback model will not invent."""
    answer = text.strip()
    if provider == "ECHO" and gathered.sources:
        excerpts = "\n\n".join(f"{source.title}\n{source.excerpt}" for source in gathered.sources)
        answer = (
            "Retrieved just now. I'm on the local fallback, so this is the source text, "
            "not a model write-up.\n\n" + excerpts
        )
        if "idea" in prompt.lower():
            answer += "\n\nI can't draft ideas beyond that excerpt until a model key is configured."
    citations = retrieval.citation_block(gathered)
    if citations and citations not in answer:
        answer = f"{answer}\n\n{citations}" if answer else citations
    if gathered.note and gathered.note not in answer:
        answer = f"{gathered.note}\n\n{answer}" if answer else gathered.note
    answer = _append_documents(answer, provider, docs)
    if intent == intent_router.QUANT_TRADING and "no order was placed" not in answer.lower():
        answer += (
            "\n\nNo order was placed. This is read-only research, and a figure without "
            "a retrieved source is uncertain."
        )
    if (
        intent == intent_router.OS_AUTOMATION
        and _wants_shell(prompt)
        and "can't run commands" not in answer.lower()
    ):
        answer += "\n\nI can't run commands from this chat."
    return answer.strip()


def _wants_shell(prompt: str) -> bool:
    lowered = prompt.lower()
    return any(phrase in lowered for phrase in ("run the command", "run this", "execute", "sudo "))


def _append_documents(answer: str, provider: str, docs: DocContext) -> str:
    if provider == "ECHO" and docs.sources:
        excerpts = "\n\n".join(f"{source.label()}\n{source.excerpt}" for source in docs.sources)
        if excerpts not in answer:
            answer = f"{answer}\n\nFrom the knowledge base:\n\n{excerpts}".strip()
    citation = docs.citation()
    if citation and citation not in answer:
        answer = f"{answer}\n\n{citation}".strip()
    if docs.note and docs.note not in answer:
        answer = f"{docs.note}\n\n{answer}".strip() if answer else docs.note
    return answer


def _method(
    *, provider: str, intent: str, mode: str, gathered: Retrieval, docs: DocContext
) -> str:
    """One or two sentences about what was used. Not a chain of thought."""
    complex_turn = bool(
        gathered.sources
        or gathered.note
        or docs.sources
        or docs.note
        or mode != "fast"
        or intent != intent_router.PERSONAL_CHAT
    )
    if not complex_turn:
        return ""
    parts: list[str] = []
    if mode == "local":
        parts.append("Local only: the prompt was not sent to an external provider.")
    elif provider == "ECHO":
        parts.append("Answered on the local fallback.")
    else:
        parts.append(f"Answered with {provider}.")
    if gathered.sources:
        parts.append(
            f"Used {len(gathered.sources)} retrieved source(s). Page text was treated as untrusted."
        )
    if docs.sources:
        parts.append(
            f"Used {len(docs.sources)} document passage(s). File text was treated as untrusted."
        )
    if intent == intent_router.QUANT_TRADING:
        parts.append("No order was placed.")
    return " ".join(parts)


orchestrator = Orchestrator()
