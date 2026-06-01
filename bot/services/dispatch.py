"""Deliver a message AS the manager (via business_connection_id), or route it
to the manager for one-tap approval when running in approve mode / on escalation.
"""

from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from bot.db import local
from bot.utils.keyboards import draft_kb

logger = logging.getLogger(__name__)


async def deliver(bot: Bot, conn_id: str, chat_id: int, text: str,
                  *, typing: bool = True) -> tuple[bool, str | None]:
    """Send `text` into `chat_id` on behalf of the manager's account.

    Works only where the manager already has a chat with that user — Telegram
    blocks cold-starting a conversation from a personal account.
    """
    try:
        if typing:
            try:
                await bot.send_chat_action(
                    chat_id=chat_id, action="typing", business_connection_id=conn_id
                )
                await asyncio.sleep(1.2)
            except TelegramBadRequest:
                pass
        # parse_mode=None: send the model's text verbatim. The bot's global
        # default is HTML, which would render any Markdown the model emits
        # (**bold**, lists) as literal characters — and could fail outright on
        # a stray '<'/'&'. Customers should see plain, human-looking text.
        await bot.send_message(
            chat_id=chat_id, text=text, business_connection_id=conn_id,
            parse_mode=None,
        )
        return True, None
    except TelegramForbiddenError as e:
        return False, f"forbidden: {e.message}"
    except TelegramBadRequest as e:
        # Most common here: no existing chat to initiate from.
        return False, f"bad_request: {e.message}"
    except Exception as e:  # noqa: BLE001
        logger.exception("deliver failed")
        return False, str(e)


def _preview(kind: str, chat_id: int, text: str, reason: str | None) -> str:
    head = "📨 Черновик ответа" if kind == "reply" else "📣 Напоминание об оплате"
    note = f"\n⚠️ {html.escape(reason)}" if reason else ""
    # The preview itself is sent as HTML — escape the model text so '<'/'&'
    # in a reply can't break the owner's draft card.
    return f"{head} → клиент <code>{chat_id}</code>{note}\n\n{html.escape(text)}"


async def send_or_approve(bot: Bot, owner_id: int, conn_id: str, chat_id: int,
                          kind: str, text: str, *, auto: bool,
                          reason: str | None = None) -> bool:
    """auto=True → send immediately as the manager.
    auto=False → store a draft and DM the manager Send/Edit/Skip buttons."""
    if auto:
        ok, err = await deliver(bot, conn_id, chat_id, text)
        if ok:
            await local.add_message(chat_id, "assistant", text)
            return True
        # Couldn't reach as the manager — fall back to manual approval/notice.
        reason = (reason or "") + f" (не доставлено: {err})"
        auto = False

    draft_id = await local.create_draft(owner_id, conn_id, chat_id, kind, text)
    try:
        await bot.send_message(
            owner_id,
            _preview(kind, chat_id, text, reason),
            reply_markup=draft_kb(draft_id),
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not notify owner %s about draft", owner_id)
    return False
