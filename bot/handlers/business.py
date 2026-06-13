"""Telegram Business / Chat Automation handlers.

The manager links this bot to their personal account (Settings → Chat
Automation, or Telegram Business → Chat-bots). Incoming customer messages
then arrive here as `business_message` updates; replies go out AS the
manager via `business_connection_id`.
"""

import asyncio
import html
import logging
import time

from aiogram import Bot, Router
from aiogram.types import BusinessConnection, Message

from config import settings
from bot.db import local, payments
from bot.services import llm
from bot.services.dispatch import send_or_approve
from bot.utils import prompts

logger = logging.getLogger(__name__)
router = Router(name="business")

# Per-customer debounce. People often split one thought across several quick
# messages; rather than answer each line, we hold a short window and let the
# latest message's task generate one reply for the whole burst. A newer message
# cancels the previous pending task and accumulates its text.
_pending: dict[int, asyncio.Task] = {}
_pending_texts: dict[int, list[str]] = {}


def _customer_ref(customer_id: int, username: str | None) -> str:
    """How a customer is named in manager pings. With a @username the manager can
    tap straight through to the chat; otherwise fall back to the raw id. Mirrors
    `dispatch._customer_label`."""
    if username:
        return f"@{username} (<code>{customer_id}</code>)"
    return f"<code>{customer_id}</code>"


@router.business_connection()
async def on_connection(event: BusinessConnection, bot: Bot) -> None:
    """Manager connected (or toggled) the bot on their account."""
    # aiogram moved this field: <3.15 exposes `can_reply`, newer uses `rights.can_reply`.
    rights = getattr(event, "rights", None)
    if rights is not None:
        can_reply = bool(getattr(rights, "can_reply", True))
    else:
        can_reply = bool(getattr(event, "can_reply", True))
    await local.upsert_connection(
        conn_id=event.id,
        owner_id=event.user.id,
        is_enabled=event.is_enabled,
        can_reply=bool(can_reply),
    )
    if event.is_enabled and can_reply:
        msg = ("✅ Бот подключён к вашему аккаунту. Теперь я буду отвечать "
               "клиентам от вашего имени и напоминать должникам об оплате.\n\n"
               "Команды: /status, /style, /auto")
    elif event.is_enabled and not can_reply:
        msg = ("⚠️ Бот подключён, но без права отвечать. Включите «Отвечать на "
               "сообщения» в настройках подключения, чтобы я мог писать клиентам.")
    else:
        msg = "🔌 Бот отключён от аккаунта."
    try:
        await bot.send_message(event.user.id, msg)
    except Exception:  # noqa: BLE001
        logger.warning("could not DM owner %s on connection", event.user.id)


@router.business_message()
async def on_business_message(message: Message, bot: Bot) -> None:
    conn_id = message.business_connection_id
    if not conn_id or not message.text:
        return

    conn = await local.get_connection(conn_id)
    if not conn or not conn["is_enabled"] or not conn["can_reply"]:
        return

    owner_id = conn["owner_id"]

    # The manager's own outgoing messages also arrive here — record for context,
    # don't reply. A manual message also means the manager has taken this chat
    # over: start (or reset) a quiet window so the bot doesn't talk over them.
    if message.from_user and message.from_user.id == owner_id:
        await local.add_message(message.chat.id, "assistant", message.text)
        # But not every owner message is the manager stepping in by hand:
        # Telegram Business away/greeting auto-replies arrive with
        # `is_from_offline`, and messages this bot itself sent on the manager's
        # behalf carry `sender_business_bot`. Treating those as a takeover would
        # let one automatic greeting silence the bot for `manager_takeover_hours`.
        # Only a genuinely hand-typed message opens the quiet window.
        is_automated = bool(message.is_from_offline) or message.sender_business_bot is not None
        if not is_automated:
            await local.record_manager_activity(message.chat.id)
        return

    customer_id = message.chat.id
    await local.add_message(customer_id, "user", message.text)

    # If the manager recently replied to this customer by hand, stay out of the
    # chat — keep storing messages for context, but let the manager handle it.
    if await local.manager_active_within(customer_id, settings.manager_takeover_hours):
        logger.info(
            "Manager active in chat %s within %dh — skipping bot reply",
            customer_id, settings.manager_takeover_hours,
        )
        return

    # Abuse guard: cap paid LLM calls per customer. The message is still stored
    # (kept for context), we just don't generate a reply while they're flooding.
    now = int(time.time())
    per_min, per_hour = await local.count_recent_user_messages(
        customer_id, now - 60, now - 3600
    )
    if per_min > settings.reply_rate_per_min or per_hour > settings.reply_rate_per_hour:
        logger.info(
            "Rate-limited customer %s (%d/min, %d/hr) — skipping LLM reply",
            customer_id, per_min, per_hour,
        )
        return

    # Hold a short window for follow-up messages, then reply to the whole burst
    # at once. The newest message's task supersedes any earlier pending one.
    _pending_texts.setdefault(customer_id, []).append(message.text)
    pending = _pending.get(customer_id)
    if pending and not pending.done():
        pending.cancel()
    username = message.from_user.username if message.from_user else None
    _pending[customer_id] = asyncio.create_task(
        _debounced_reply(bot, conn_id, owner_id, customer_id, username)
    )


async def _debounced_reply(
    bot: Bot, conn_id: str, owner_id: int, customer_id: int, username: str | None
) -> None:
    """After a quiet window, generate one reply for the customer's message burst."""
    try:
        await asyncio.sleep(settings.reply_debounce_seconds)
    except asyncio.CancelledError:
        return  # a newer message arrived — its task will answer the full batch
    # Past the quiet window: claim the batch. We no longer cancel ourselves, so a
    # message arriving mid-generation starts a fresh cycle rather than aborting.
    _pending.pop(customer_id, None)
    texts = _pending_texts.pop(customer_id, [])
    customer_text = "\n".join(texts).strip()

    # Ground the reply in real payment data.
    status = await payments.get_customer_status(customer_id)

    owner = await local.get_owner(owner_id)
    style = owner["style_prompt"] if owner else None
    if owner and owner["auto_reply"] is not None:
        auto = bool(owner["auto_reply"])
    else:
        auto = settings.auto_reply

    escalate = prompts.needs_escalation(customer_text)

    history = await local.get_history(customer_id, limit=10)
    llm_messages = [{"role": "system", "content": prompts.system_prompt(status, style)}]
    llm_messages += history  # already ends with the customer's latest messages

    reply = await llm.chat(llm_messages)
    if not reply:
        # Don't auto-send anything if the model failed — ping the manager.
        try:
            await bot.send_message(
                owner_id,
                f"🤖 Клиент {_customer_ref(customer_id, username)} написал, но ИИ "
                f"не ответил. Ответьте вручную.\n\n«{html.escape(customer_text)}»",
            )
        except Exception:  # noqa: BLE001
            pass
        return

    # The model flags replies it couldn't ground ("I'll check with the manager").
    # We still send the customer the reassurance, but always ping the manager so
    # they actually follow up — the bot must not be the last word here.
    wants_manager, reply = prompts.split_call_manager(reply)
    # Swap a price-list request marker for the deterministic, aligned table.
    reply = prompts.insert_price_table(reply)

    if wants_manager:
        reason = "клиент ждёт ответа от менеджера"
    elif escalate:
        reason = "чувствительная тема — проверьте"
    else:
        reason = None

    # When the bot is only buying time ("I'll pass this to the manager"), the
    # reassurance is safe to send the customer immediately even in draft mode —
    # it makes no claims and the manager is pinged separately below to give the
    # real answer. Escalation still always drafts for approval.
    auto_send = (auto or wants_manager) and not escalate

    await send_or_approve(
        bot, owner_id, conn_id, customer_id, "reply", reply,
        auto=auto_send,
        reason=reason,
        username=username,
    )

    if wants_manager:
        try:
            await bot.send_message(
                owner_id,
                f"🙋 Клиент {_customer_ref(customer_id, username)} задал вопрос, на "
                f"который я не смог ответить — нужен ваш ответ.\n\n"
                f"«{html.escape(customer_text)}»",
            )
        except Exception:  # noqa: BLE001
            pass
