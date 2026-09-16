"""Dual-engine LLM router: Groq for speed, Gemini for depth, echo as fallback.

Routing matrix from the design doc:
  * Rapid chat / OS / terminal      -> Groq   (fastest streaming inference)
  * Large-context reasoning, code,
    trading analysis, vision        -> Gemini (1M-token context, multimodal)
  * Everything, if the network or
    both keys are unavailable       -> echo   (local, deterministic)

The echo engine is not a stub for tests -- it is a real degraded mode. It means
the service deploys, boots, and demonstrably works before any API key exists,
so a cloud deploy is never blocked on a signup queue. `/health` always reports
which engine is actually live.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

from app.agent import router as intent_router
from app.core.config import Settings, get_settings

log = logging.getLogger("jarvis.engines")


@dataclass
class ToolCall:
    """One tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class EngineResult:
    """One completion, plus the telemetry the flywheel needs."""

    text: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # None for non-streaming calls, where time-to-first-token is not
    # separately observable. Populated once streaming lands.
    ttft_ms: int | None = None
    reasoning: str = ""
    fallbacks: list[str] = field(default_factory=list)
    #: Tools the model wants run before it can answer.
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class EngineError(RuntimeError):
    """Raised when a provider call fails; triggers fallback to the next engine."""


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


class GroqEngine:
    provider = "GROQ"

    def __init__(self, settings: Settings) -> None:
        self.model = settings.groq_model
        self._api_key = settings.groq_api_key
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            from groq import Groq

            self._client = Groq(api_key=self._api_key)
        return self._client

    def complete(self, system_prompt: str, user_prompt: str) -> EngineResult:
        return self.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 1200,
    ) -> EngineResult:
        """Groq speaks the OpenAI wire format, so messages pass through as-is."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        try:
            completion = self._get_client().chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - surfaced as EngineError
            raise EngineError(f"Groq call failed: {exc}") from exc

        choice = completion.choices[0].message
        calls: list[ToolCall] = []
        for raw in getattr(choice, "tool_calls", None) or []:
            try:
                arguments = json.loads(raw.function.arguments or "{}")
            except json.JSONDecodeError:
                # A model emitting malformed JSON is normal enough that it must
                # not kill the task -- the runner reports it back as a tool error.
                arguments = {"__malformed__": raw.function.arguments}
            calls.append(ToolCall(id=raw.id, name=raw.function.name, arguments=arguments))

        usage = completion.usage
        return EngineResult(
            text=(choice.content or "").strip(),
            provider=self.provider,
            model=self.model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            tool_calls=calls,
        )


class GeminiEngine:
    provider = "GEMINI"

    def __init__(self, settings: Settings) -> None:
        self.model = settings.gemini_model
        self._api_key = settings.gemini_api_key
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def complete(self, system_prompt: str, user_prompt: str) -> EngineResult:
        return self.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 1200,
    ) -> EngineResult:
        """Gemini uses its own shape, so OpenAI-style messages are converted.

        System messages become `system_instruction`; assistant turns become
        role "model"; tool results become function-response parts.
        """
        try:
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - dependency ships with the app
            raise EngineError(f"google-genai unavailable: {exc}") from exc

        system_parts = [m["content"] for m in messages if m.get("role") == "system"]
        contents: list[Any] = []

        for message in messages:
            role = message.get("role")
            if role == "system":
                continue
            if role == "user":
                contents.append(types.Content(role="user", parts=[types.Part(text=message["content"])]))
            elif role == "assistant":
                parts: list[Any] = []
                if message.get("content"):
                    parts.append(types.Part(text=message["content"]))
                for call in message.get("tool_calls") or []:
                    parts.append(
                        types.Part(
                            function_call=types.FunctionCall(
                                name=call["function"]["name"],
                                args=json.loads(call["function"]["arguments"] or "{}"),
                            )
                        )
                    )
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
            elif role == "tool":
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                function_response=types.FunctionResponse(
                                    name=message.get("name", "tool"),
                                    response={"result": message.get("content", "")},
                                )
                            )
                        ],
                    )
                )

        config_kwargs: dict[str, Any] = {
            "temperature": 0.7,
            "max_output_tokens": max_tokens,
        }
        if system_parts:
            config_kwargs["system_instruction"] = "\n\n".join(system_parts)
        if tools:
            config_kwargs["tools"] = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t["function"]["name"],
                            description=t["function"].get("description", ""),
                            parameters=t["function"].get("parameters", {}),
                        )
                        for t in tools
                    ]
                )
            ]

        try:
            response = self._get_client().models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(**config_kwargs),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as EngineError
            raise EngineError(f"Gemini call failed: {exc}") from exc

        calls: list[ToolCall] = []
        text_chunks: list[str] = []
        for candidate in getattr(response, "candidates", None) or []:
            for part in getattr(getattr(candidate, "content", None), "parts", None) or []:
                function_call = getattr(part, "function_call", None)
                if function_call is not None:
                    calls.append(
                        ToolCall(
                            id=f"gemini_{len(calls)}",
                            name=function_call.name,
                            arguments=dict(function_call.args or {}),
                        )
                    )
                elif getattr(part, "text", None):
                    text_chunks.append(part.text)

        usage = getattr(response, "usage_metadata", None)
        return EngineResult(
            text="".join(text_chunks).strip(),
            provider=self.provider,
            model=self.model,
            prompt_tokens=getattr(usage, "prompt_token_count", 0) or 0,
            completion_tokens=getattr(usage, "candidates_token_count", 0) or 0,
            tool_calls=calls,
        )


class EchoEngine:
    """Local no-key fallback.

    Produces real, varied replies for greetings and mood check-ins so the app is
    genuinely usable before any provider is configured. It is honest about being
    offline rather than pretending to be the model.
    """

    provider = "ECHO"
    model = "jarvis-local-echo"

    _GREETINGS = (
        "Hey. Jarvis here, running on local fallback -- no model key configured yet, "
        "so this is me, not an LLM. Everything else works: check in with your mood "
        "and it gets logged properly.",
        "Hi. I'm up and answering, though I'm on the built-in fallback right now "
        "rather than a real model. Add a Groq or Gemini key and I get a lot more "
        "interesting.",
    )

    _MOOD_REPLIES = {
        1: "That sounds like a hard one. Logged it. I'm running on local fallback so I "
           "can't say anything clever yet -- but the check-in is saved.",
        2: "Noted, and sorry it's a flat one. Recorded it so the trend is there when "
           "you want to look back.",
        3: "Okay is a perfectly fine place to be. Logged.",
        4: "Good to hear. Saved it.",
        5: "Great day -- logged, and worth remembering what made it one.",
    }

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 1200,
    ) -> EngineResult:
        """Answer from the last user turn.

        Never emits tool calls: this engine cannot reason about when a tool is
        warranted, and inventing calls would produce confidently wrong actions.
        Agents therefore degrade to plain text without a provider key.
        """
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        return self.complete(system, user)

    def complete(self, system_prompt: str, user_prompt: str) -> EngineResult:
        lowered = user_prompt.lower()
        text = ""

        if lowered.startswith("mood check-in:"):
            for score, reply in self._MOOD_REPLIES.items():
                if f"{score}/5" in user_prompt:
                    text = reply
                    break

        if not text:
            if any(word in lowered for word in ("hi", "hello", "hey", "yo", "morning", "evening")):
                text = random.choice(self._GREETINGS)
            else:
                text = (
                    "I hear you, but I'm on the local fallback engine -- no model key is "
                    "configured, so I can't reason about that properly yet. Set GROQ_API_KEY "
                    "or GEMINI_API_KEY and restart, and I'll answer for real."
                )

        return EngineResult(
            text=text,
            provider=self.provider,
            model=self.model,
            reasoning="No provider key configured; served by local echo engine.",
        )


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------


class EngineRouter:
    """Chooses an engine per intent and falls back on failure."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.groq = GroqEngine(self.settings) if self.settings.groq_enabled else None
        self.gemini = GeminiEngine(self.settings) if self.settings.gemini_enabled else None
        self.echo = EchoEngine()

    # -- introspection ---------------------------------------------------

    def available(self) -> list[str]:
        names = []
        if self.groq:
            names.append("groq")
        if self.gemini:
            names.append("gemini")
        names.append("echo")
        return names

    @property
    def is_degraded(self) -> bool:
        """True when no real provider is configured."""
        return self.groq is None and self.gemini is None

    def describe(self) -> dict[str, Any]:
        return {
            "available": self.available(),
            "degraded": self.is_degraded,
            "groq_model": self.settings.groq_model if self.groq else None,
            "gemini_model": self.settings.gemini_model if self.gemini else None,
        }

    # -- selection -------------------------------------------------------

    def _chain_for(self, intent: str) -> list[Any]:
        """Ordered engines to try for an intent, per the doc's routing matrix."""
        deep = intent in (intent_router.CODING_AGENT, intent_router.QUANT_TRADING)
        return self._chain(deep=deep)

    def _chain(self, *, deep: bool) -> list[Any]:
        preferred = [self.gemini, self.groq] if deep else [self.groq, self.gemini]
        chain = [engine for engine in preferred if engine is not None]
        chain.append(self.echo)
        return chain

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        prefer: str = "fast",
        max_tokens: int = 1200,
    ) -> EngineResult:
        """Multi-turn completion with optional tool calling.

        Walks the same fallback chain as `complete()`. An engine that raises or
        returns nothing usable is skipped; the echo engine terminates the chain
        and never fails.
        """
        chain = self._chain(deep=(prefer == "deep"))
        fallbacks: list[str] = []

        for engine in chain:
            try:
                result = engine.chat(messages, tools, max_tokens)
            except EngineError as exc:
                log.warning("engine %s failed, falling back: %s", engine.provider, exc)
                fallbacks.append(f"{engine.provider}: {exc}")
                continue

            # A tool call with no prose is a perfectly good response.
            if not result.text and not result.wants_tools:
                fallbacks.append(f"{engine.provider}: empty response")
                continue

            result.fallbacks = fallbacks
            return result

        raise EngineError("all engines failed, including local fallback")

    def complete(self, *, intent: str, system_prompt: str, user_prompt: str) -> EngineResult:
        """Run the completion, walking the fallback chain on failure."""
        chain = self._chain_for(intent)
        fallbacks: list[str] = []

        for position, engine in enumerate(chain):
            started = time.perf_counter()
            try:
                result = engine.complete(system_prompt, user_prompt)
            except EngineError as exc:
                log.warning("engine %s failed, falling back: %s", engine.provider, exc)
                fallbacks.append(f"{engine.provider}: {exc}")
                continue

            if not result.text:
                log.warning("engine %s returned empty text, falling back", engine.provider)
                fallbacks.append(f"{engine.provider}: empty response")
                continue

            result.fallbacks = fallbacks
            if not result.reasoning:
                why = "preferred for deep reasoning" if position == 0 and intent in (
                    intent_router.CODING_AGENT, intent_router.QUANT_TRADING
                ) else "preferred for low-latency chat"
                result.reasoning = (
                    f"Intent {intent} routed to {engine.provider} ({engine.model}); {why}."
                )
                if fallbacks:
                    result.reasoning += f" Recovered after {len(fallbacks)} failed engine(s)."
            log.info(
                "turn served by %s in %.0fms", engine.provider,
                (time.perf_counter() - started) * 1000,
            )
            return result

        # Unreachable: EchoEngine never raises and never returns empty text.
        raise EngineError("all engines failed, including local fallback")
