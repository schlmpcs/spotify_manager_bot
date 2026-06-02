"""Deliver a message AS the manager (via business_connection_id), or route it
to the manager for one-tap approval when running in approve mode / on escalation.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from urllib.parse import quote

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


# A real Telegram public username: starts with a letter, 5–32 chars, letters/
# digits/underscore. The payment DB sometimes stores str(user_id) or a short
# garbage value in the username column when the customer has no public handle —
# those must NOT be turned into a t.me link (it resolves to "user doesn't exist").
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def _valid_username(username: str | None) -> bool:
    return bool(username and _USERNAME_RE.match(username))


def _customer_label(chat_id: int, username: str | None) -> str:
    """How a customer is shown on the draft card. The @handle is shown only when
    it's a valid public username (the payment DB sometimes stores str(user_id)
    or a short value there). The id stays a tap-to-copy `<code>` so the card can
    never fail to send (a tg://user mention the bot can't resolve might)."""
    handle = f"@{username} " if _valid_username(username) else ""
    return f"{handle}(<code>{chat_id}</code>)"


_TAG_RE = re.compile(r"<[^>]+>")


def _plain(text: str) -> str:
    """Strip HTML tags and unescape entities → what a human would actually type.
    Messages are formatted with Telegram HTML for the *bot* to send; when the
    manager sends a draft by hand (manual-send card) there's no HTML parsing, so
    the tags must be removed or the customer would see literal '<b>'."""
    return html.unescape(_TAG_RE.sub("", text))


def _chat_url(username: str | None, text: str | None = None) -> str | None:
    """A prefilled-message link to the customer's chat. Telegram only supports
    pre-filling the input box (`?text=`) on a public-username link, so this needs
    a *valid* @username — a numeric id or stale value has no working t.me link
    (those rely on the `tg://user?id=` open-chat link in the card instead).

    With `text`, Telegram pre-fills the input box (the manager still taps Send).
    Per Telegram's rules a leading '@' must be space-prefixed so it isn't read as
    an inline query."""
    if not _valid_username(username):
        return None
    base = f"https://t.me/{username}"
    if not text:
        return base
    if text.startswith("@"):
        text = " " + text
    url = f"{base}?text={quote(text)}"
    # Guard against an over-long button URL (Telegram can reject it, which would
    # sink the whole owner notification). If so, drop the prefill — the manager
    # still gets the open-chat link and copies the text from the card.
    return url if len(url) <= 2000 else base


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
        # The bot can't deliver this — frame it as a manual send. A valid public
        # @username gets the «✍️ Написать» button that opens the chat with the
        # text already in the input box; otherwise the manager opens the chat via
        # the id link above and copies the text from the tap-to-copy block.
        how = (
            "Нажмите «✍️ Написать» ниже — чат откроется с готовым текстом, "
            "останется нажать «Отправить»."
            if _valid_username(username) else
            "У клиента нет @username — откройте чат вручную, скопируйте текст "
            "и отправьте от себя:"
        )
        # Show the plain text the manager will actually send (no HTML tags).
        return (f"{head} → клиент {label}{note}\n\n{how}\n"
                f"<pre>{html.escape(_plain(text))}</pre>")
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
    # On a manual-send card the open-chat link pre-fills the input with the text
    # (plain, since the manager sends it by hand), so they just open and tap Send.
    kb = (manual_send_kb(draft_id, _chat_url(username, _plain(text))) if manual
          else draft_kb(draft_id))
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
