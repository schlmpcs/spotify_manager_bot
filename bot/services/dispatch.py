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
from bot.utils.keyboards import draft_kb, manual_send_kb

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
        # The model is told to format with Telegram HTML (<b>, <i>, …), so we
        # send under the bot's global HTML default. If it slips and emits a
        # stray '<'/'&' or a broken tag, Telegram rejects the message with a
        # parse error — fall back to sending the text verbatim as plain text so
        # the customer still gets the reply.
        try:
            await bot.send_message(
                chat_id=chat_id, text=text, business_connection_id=conn_id,
            )
        except TelegramBadRequest as e:
            if not _is_parse_error(e):
                raise
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


def _is_parse_error(e: TelegramBadRequest) -> bool:
    """True when Telegram rejected the message because of bad HTML entities."""
    return "can't parse entities" in (e.message or "").lower()


def _customer_label(chat_id: int, username: str | None) -> str:
    """How a customer is shown on the draft card. With a @username the manager
    can tap straight through to the chat; otherwise fall back to the raw id."""
    if username:
        return f"@{username} (<code>{chat_id}</code>)"
    return f"<code>{chat_id}</code>"


def _chat_url(username: str | None) -> str | None:
    """A tap-to-open link to the customer's chat — only possible from a public
    @username; a bare numeric id has no public t.me link."""
    return f"https://t.me/{username}" if username else None


def _manual_reason(err: str | None) -> str:
    """Turn a raw Telegram delivery error into a plain-language note telling the
    manager why the bot couldn't send and that they must do it by hand."""
    e = (err or "").upper()
    if "PEER_USAGE_MISSING" in e:
        return ("бот не может написать — клиент не писал вам за последние 24 ч "
                "(ограничение Telegram). Откройте чат и отправьте сами 👇")
    if "FORBIDDEN" in e or "BLOCKED" in e or "INITIATE" in e:
        return ("бот не может написать этому клиенту (нет диалога или он "
                "заблокирован). Откройте чат и отправьте сами 👇")
    return f"бот не смог отправить ({err}). Отправьте вручную 👇"


def _preview(kind: str, chat_id: int, text: str, reason: str | None,
             *, escape_text: bool, username: str | None = None,
             manual: bool = False) -> str:
    head = "📨 Черновик ответа" if kind == "reply" else "📣 Напоминание об оплате"
    note = f"\n⚠️ {html.escape(reason)}" if reason else ""
    label = _customer_label(chat_id, username)
    if manual:
        # The bot can't deliver this — frame it as a manual send. Put the text in
        # a tap-to-copy block so the manager can copy → open the chat → paste.
        return (
            f"{head} → клиент {label}{note}\n\n"
            "Скопируйте текст и отправьте от себя:\n"
            f"<pre>{html.escape(text)}</pre>"
        )
    # Show the draft exactly as the customer would see it — render the model's
    # HTML. If it turns out to be malformed (parse error on send), we re-render
    # with the text escaped so the owner still sees the draft as raw text.
    body = html.escape(text) if escape_text else text
    return f"{head} → клиент {label}{note}\n\n{body}"


async def send_or_approve(bot: Bot, owner_id: int, conn_id: str, chat_id: int,
                          kind: str, text: str, *, auto: bool,
                          reason: str | None = None,
                          username: str | None = None) -> bool:
    """auto=True → send immediately as the manager.
    auto=False → store a draft and DM the manager Send/Edit/Skip buttons.

    `username` (the customer's Telegram @handle, when known) is shown on the
    draft card so the manager can tap through to the chat."""
    # manual=True means the bot already tried and Telegram refused delivery, so
    # the owner has to send by hand — the card drops "Отправить" (it would fail
    # again) for an "open chat" link instead.
    manual = False
    if auto:
        ok, err = await deliver(bot, conn_id, chat_id, text)
        if ok:
            await local.add_message(chat_id, "assistant", text)
            return True
        # Couldn't reach as the manager — fall back to a manual-send card.
        reason = _manual_reason(err)
        manual = True

    draft_id = await local.create_draft(owner_id, conn_id, chat_id, kind, text)
    kb = manual_send_kb(draft_id, _chat_url(username)) if manual else draft_kb(draft_id)
    try:
        try:
            await bot.send_message(
                owner_id,
                _preview(kind, chat_id, text, reason, escape_text=False,
                         username=username, manual=manual),
                reply_markup=kb,
            )
        except TelegramBadRequest as e:
            if not _is_parse_error(e):
                raise
            # Model emitted broken HTML — show it escaped so the card still sends.
            await bot.send_message(
                owner_id,
                _preview(kind, chat_id, text, reason, escape_text=True,
                         username=username, manual=manual),
                reply_markup=kb,
            )
    except Exception:  # noqa: BLE001
        logger.exception("could not notify owner %s about draft", owner_id)
    return False
