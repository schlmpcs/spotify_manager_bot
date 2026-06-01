"""Telegram Business / Chat Automation handlers.

The manager links this bot to their personal account (Settings → Chat
Automation, or Telegram Business → Chat-bots). Incoming customer messages
then arrive here as `business_message` updates; replies go out AS the
manager via `business_connection_id`.
"""

import logging

from aiogram import Bot, Router
from aiogram.types import BusinessConnection, Message

from config import settings
from bot.db import local, payments
from bot.services import llm
from bot.services.dispatch import send_or_approve
from bot.utils import prompts

logger = logging.getLogger(__name__)
router = Router(name="business")


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

    # The manager's own outgoing messages also arrive here — record for context, don't reply.
    if message.from_user and message.from_user.id == owner_id:
        await local.add_message(message.chat.id, "assistant", message.text)
        return

    customer_id = message.chat.id
    await local.add_message(customer_id, "user", message.text)

    # Ground the reply in real payment data.
    status = await payments.get_customer_status(customer_id)

    owner = await local.get_owner(owner_id)
    style = owner["style_prompt"] if owner else None
    if owner and owner["auto_reply"] is not None:
        auto = bool(owner["auto_reply"])
    else:
        auto = settings.auto_reply

    escalate = prompts.needs_escalation(message.text)

    history = await local.get_history(customer_id, limit=10)
    llm_messages = [{"role": "system", "content": prompts.system_prompt(status, style)}]
    llm_messages += history  # already ends with the current user message

    reply = await llm.chat(llm_messages)
    if not reply:
        # Don't auto-send anything if the model failed — ping the manager.
        try:
            await bot.send_message(
                owner_id,
                f"🤖 Клиент <code>{customer_id}</code> написал, но ИИ не ответил. "
                f"Ответьте вручную.\n\n«{message.text}»",
            )
        except Exception:  # noqa: BLE001
            pass
        return

    await send_or_approve(
        bot, owner_id, conn_id, customer_id, "reply", reply,
        auto=auto and not escalate,
        reason="чувствительная тема — проверьте" if escalate else None,
    )
