"""Upgrade plain emoji glyphs to Telegram Premium custom (animated) emoji.

Telegram lets a bot send ``<tg-emoji emoji-id="…">`` entities in private chats
when the bot's OWNER account has Telegram Premium (Bot API changelog, Feb 2026).
Each custom emoji is addressed by a numeric ``custom_emoji_id`` tied to one emoji
in a pack — harvest those with the ``/emojiid`` owner command, then map
``glyph → id`` in ``CUSTOM_EMOJI_IDS`` (``.env``).

``decorate()`` wraps every mapped glyph in a ``<tg-emoji>`` tag, keeping the
original glyph as the inner fallback (what non-Premium viewers — i.e. most
customers — and any place a custom emoji can't render will see). Unmapped glyphs
are left untouched; with an empty map the text is returned verbatim.

This is the single integration point: ``dispatch.deliver`` calls ``decorate``
right before sending, so it covers both the fixed templates (prices, requisites,
nudges) and the LLM's own replies. If Telegram rejects the custom emoji (owner
not Premium / bad id / not permitted over the business connection) the sender
calls ``note_failure()`` and retries without the upgrade, and decoration stays
off for the rest of the process so we don't double-send on every message.
"""

from __future__ import annotations

import html
import logging

from config import settings

logger = logging.getLogger(__name__)

# Flipped on the first time a send is rejected for a custom-emoji reason, so we
# stop paying the failed-send + retry cost on every subsequent message.
_runtime_disabled = False


def enabled() -> bool:
    return bool(settings.custom_emoji_ids) and not _runtime_disabled


def decorate(text: str) -> str:
    """Replace each mapped plain glyph in ``text`` with its custom-emoji tag.

    Glyphs already wrapped by an earlier replacement aren't touched again (the
    inserted tag is ASCII apart from the glyph itself), so order doesn't matter.
    """
    if not text or not enabled():
        return text
    for glyph, emoji_id in settings.custom_emoji_ids.items():
        if not glyph or not emoji_id or glyph not in text:
            continue
        eid = html.escape(str(emoji_id), quote=True)
        text = text.replace(glyph, f'<tg-emoji emoji-id="{eid}">{glyph}</tg-emoji>')
    return text


def note_failure() -> None:
    """Disable decoration for the rest of the process after a rejected send."""
    global _runtime_disabled
    if not _runtime_disabled:
        _runtime_disabled = True
        logger.warning(
            "Custom emoji rejected by Telegram — disabling glyph upgrade. "
            "Check that the bot owner has Telegram Premium and the IDs in "
            "CUSTOM_EMOJI_IDS are valid."
        )
