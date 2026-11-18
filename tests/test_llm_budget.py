"""LLM provider cooldown."""

from __future__ import annotations

from app.agent import llm_budget
from app.agent.engines import EngineRouter, EngineResult


def test_cooled_provider_skipped_in_chain(env):
    llm_budget.clear()
    router = EngineRouter()
    router.groq = type("E", (), {"provider": "GROQ"})()
    router.gemini = None
    router.ollama = None

    llm_budget.mark_cooled("GROQ", 60.0)
    chain = router._chain(deep=False)
    providers = [getattr(e, "provider", "") for e in chain]
    assert "GROQ" not in providers
    assert providers[-1] == "ECHO"
