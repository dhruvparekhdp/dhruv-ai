"""Web tools.

Deliberately minimal: fetch a page and extract readable text. No search-engine
key is wired up because every free search API needs its own signup, and the
Research Agent is useful without one as long as it can read a URL you give it.

`web.search` is registered but returns an honest "not configured" message
rather than a fabricated result list -- a fake answer is worse than no answer.
"""

from __future__ import annotations

import asyncio
import html
import re
from typing import Any

from app.core.config import get_settings
from app.tools.registry import Capability, Risk, ToolError, tool

MAX_CHARS = 6_000
TIMEOUT_SECONDS = 15

_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\n{3,}")


def _to_text(raw_html: str) -> str:
    """Crude but dependency-free HTML → text."""
    text = _SCRIPT_STYLE.sub(" ", raw_html)
    text = _TAGS.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return _WHITESPACE.sub("\n\n", text).strip()


@tool(
    name="web.fetch",
    description=(
        "Fetch a web page and return its readable text. Use this to read a specific URL. "
        "Content is truncated, so prefer specific pages over site roots."
    ),
    parameters={
        "type": "object",
        "properties": {"url": {"type": "string", "description": "Absolute http(s) URL."}},
        "required": ["url"],
    },
    capability=Capability.READ,
    risk=Risk.SAFE,
)
async def web_fetch_tool(url: str, **_: Any) -> str:
    if not url.startswith(("http://", "https://")):
        raise ToolError("url must start with http:// or https://")

    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - httpx ships with the app
        raise ToolError("httpx is not installed") from exc

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers={"User-Agent": "Jarvis/0.2 (personal assistant)"})
    except asyncio.TimeoutError as exc:
        raise ToolError(f"timed out after {TIMEOUT_SECONDS}s fetching {url}") from exc
    except Exception as exc:  # noqa: BLE001 - surfaced to the model as a tool error
        raise ToolError(f"could not fetch {url}: {exc}") from exc

    if response.status_code >= 400:
        raise ToolError(f"{url} returned HTTP {response.status_code}")

    text = _to_text(response.text)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + f"\n\n[truncated at {MAX_CHARS} characters]"
    return text or "[page had no extractable text]"


@tool(
    name="web.search",
    description="Search the web for a query. Returns result titles and URLs.",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
    capability=Capability.READ,
    risk=Risk.SAFE,
)
async def web_search_tool(query: str, **_: Any) -> str:
    settings = get_settings()
    if not settings.search_api_key:
        # Honest failure. Inventing plausible-looking results would poison both
        # the answer and the training data collected from it.
        raise ToolError(
            "Web search is not configured (no SEARCH_API_KEY). "
            "Ask the user for a specific URL and use web.fetch instead."
        )

    try:
        import httpx

        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(
                "https://api.tavily.com/search",
                json={"api_key": settings.search_api_key, "query": query, "max_results": 5},
            )
        response.raise_for_status()
        results = response.json().get("results", [])
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"search failed: {exc}") from exc

    if not results:
        return f"No results for '{query}'."
    return "\n".join(
        f"- {item.get('title', 'untitled')} — {item.get('url', '')}\n  {item.get('content', '')[:300]}"
        for item in results
    )
