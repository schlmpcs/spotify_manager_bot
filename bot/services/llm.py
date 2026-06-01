"""Async LLM client routing over a chain of free OpenRouter models.

OpenRouter exposes many models behind one OpenAI-compatible endpoint. The free
tags (``...:free``) are generous but rate-limited per-model, so we keep an
ordered list and, on a 429 (or any transient failure), fall through to the next
model — borrowed from https://github.com/rlxrd/assist_ai_bot. Strictly async
(aiohttp); returns None on total failure so callers can degrade gracefully.
"""

import logging
from typing import Optional

import aiohttp

from config import settings

logger = logging.getLogger(__name__)

# Models that, when hit, mean "this whole attempt is doomed — stop rotating".
# Anything else (429, 5xx, timeout, model unavailable) just tries the next tag.
_FATAL_STATUSES = {401, 403}


async def chat(messages: list[dict], *, temperature: float = 0.6) -> Optional[str]:
    """Call OpenRouter chat-completions, rotating through the free-model chain.

    `messages` is a list of {role, content}. Returns the assistant text from the
    first model that answers, or None if every model fails (caller decides
    fallback).
    """
    url = settings.openrouter_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key.get_secret_value()}",
        "Content-Type": "application/json",
        # Optional attribution headers OpenRouter recommends; harmless if unused.
        "HTTP-Referer": "https://github.com/schlmpcs/spotify_manager_bot",
        "X-Title": "spotify-manager-bot",
    }
    timeout = aiohttp.ClientTimeout(total=settings.llm_timeout)

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            for model in settings.llm_models:
                payload = {
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "temperature": temperature,
                    "max_tokens": settings.llm_max_tokens,
                }
                try:
                    async with session.post(url, json=payload) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            choices = data.get("choices") or []
                            content = (
                                (choices[0].get("message") or {}).get("content", "")
                                if choices
                                else ""
                            ).strip()
                            if content:
                                return content
                            logger.warning("%s returned empty content; trying next", model)
                            continue

                        body = await resp.text()
                        if resp.status in _FATAL_STATUSES:
                            logger.error(
                                "OpenRouter auth error %s (%s) — aborting chain: %s",
                                resp.status, model, body,
                            )
                            return None
                        # 429 / 404 / 5xx → rotate to the next free model.
                        logger.warning(
                            "OpenRouter %s on %s; trying next model: %s",
                            resp.status, model, body[:200],
                        )
                except aiohttp.ClientError as e:
                    logger.warning("Network error on %s; trying next model: %s", model, e)
                except Exception as e:  # noqa: BLE001 — never let one model kill the chain
                    logger.warning("Unexpected error on %s; trying next model: %s", model, e)
    except Exception as e:  # noqa: BLE001 — session-level failure
        logger.error("LLM call failed: %s", e)
        return None

    logger.error("All %d OpenRouter models failed", len(settings.llm_models))
    return None
