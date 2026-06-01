"""Async client for a local Ollama server. Strictly async (aiohttp)."""

import logging
from typing import Optional

import aiohttp

from config import settings

logger = logging.getLogger(__name__)


async def chat(messages: list[dict], *, temperature: float = 0.6) -> Optional[str]:
    """Call Ollama /api/chat. `messages` is a list of {role, content}.

    Returns the assistant text, or None on failure (caller decides fallback).
    """
    url = settings.llm_base_url.rstrip("/") + "/api/chat"
    payload = {
        "model": settings.llm_model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": settings.llm_max_tokens,
        },
    }
    timeout = aiohttp.ClientTimeout(total=settings.llm_timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    logger.error("Ollama %s: %s", resp.status, await resp.text())
                    return None
                data = await resp.json()
                return (data.get("message") or {}).get("content", "").strip() or None
    except Exception as e:  # noqa: BLE001 — network/timeout, degrade gracefully
        logger.error("LLM call failed: %s", e)
        return None
