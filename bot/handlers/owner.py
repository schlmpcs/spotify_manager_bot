"""Commands for the manager (owner) to configure and control the bot."""

import html
import json
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import settings
from bot.db import local, payments
from bot.services import admin_assistant
from bot.services.dispatch import deliver, send_manual_card, send_or_approve
from bot.utils import prompts
from bot.utils.prompts import DEFAULT_STYLE
from bot.utils.states import OwnerStates

logger = logging.getLogger(__name__)
router = Router(name="owner")
router.message.filter(F.from_user.id.in_(settings.owner_ids))


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Я — бот-ассистент менеджера.\n\n"
        "Я отвечаю клиентам <b>от вашего имени</b> и напоминаю должникам об оплате, "
        "опираясь на реальную базу платежей.\n\n"
        "<b>Как подключить:</b>\n"
        "1. Настройки Telegram → <b>Telegram для бизнеса</b> → <b>Чат-боты</b> "
        "(или <b>Автоматизация чатов</b>).\n"
        f"2. Добавьте этого бота и выберите чаты.\n"
        "3. Включите право «Отвечать на сообщения».\n\n"
        "Команды: /status · /style · /auto · /client · /nudge · /emojiid\n"
        "Можно также просто написать мне вопрос про клиента."
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    conn = await local.get_owner_connection(message.from_user.id)
    owner = await local.get_owner(message.from_user.id)
    if owner and owner["auto_reply"] is not None:
        auto = bool(owner["auto_reply"])
    else:
        auto = settings.auto_reply
    style_set = bool(owner and owner["style_prompt"])
    lines = [
        f"🔌 Подключение: {'✅ активно' if conn else '❌ нет'}",
        f"🤖 Авто-ответы: {'вкл' if auto else 'выкл (черновики на подтверждение)'}",
        f"📣 Проактивные напоминания: {settings.proactive_mode}",
        f"✍️ Стиль: {'задан' if style_set else 'по умолчанию'}",
    ]
    await message.answer("\n".join(lines))


@router.message(Command("style"))
async def cmd_style(message: Message, state: FSMContext) -> None:
    await state.set_state(OwnerStates.setting_style)
    await message.answer(
        "✍️ Опишите, как вы общаетесь с клиентами — тон, длина, эмодзи и т.д.\n\n"
        f"<i>Пример:</i> {DEFAULT_STYLE}\n\n"
        "Отправьте текст одним сообщением (или /cancel)."
    )


@router.message(OwnerStates.setting_style, Command("cancel"))
async def cancel_style(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


@router.message(OwnerStates.setting_style, F.text)
async def save_style(message: Message, state: FSMContext) -> None:
    await local.set_style_prompt(message.from_user.id, message.text.strip())
    await state.clear()
    await message.answer("✅ Стиль сохранён.")


@router.message(Command("auto"))
async def cmd_auto(message: Message) -> None:
    arg = (message.text or "").split(maxsplit=1)
    val = arg[1].strip().lower() if len(arg) > 1 else ""
    if val in ("on", "вкл", "1", "true"):
        await local.set_auto_reply(message.from_user.id, True)
        await message.answer("🤖 Авто-ответы включены.")
    elif val in ("off", "выкл", "0", "false"):
        await local.set_auto_reply(message.from_user.id, False)
        await message.answer("✍️ Авто-ответы выключены — буду присылать черновики.")
    else:
        await message.answer("Использование: <code>/auto on</code> или <code>/auto off</code>")


@router.message(Command("client"))
async def cmd_client(message: Message, bot: Bot) -> None:
    """Owner asks about a client by id, @username, name, or a short question."""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 1:
        await message.answer(
            "Использование: <code>/client 123456789</code> или "
            "<code>/client @username</code>.\n"
            "Можно спросить обычным текстом: <code>кто просрочил оплату?</code>"
        )
        return
    await _answer_admin_question(message, bot, parts[1])


@router.message(Command("nudge"))
async def cmd_nudge(message: Message, bot: Bot) -> None:
    """Run the overdue-customer outreach right now."""
    from bot.services.outreach import run_outreach
    await message.answer("📣 Запускаю напоминания должникам…")
    r = await run_outreach(bot, force=True)
    await message.answer(
        "Готово.\n"
        f"1-е напоминание: {r['stage1']} · 2-е напоминание: {r['stage2']}\n"
        f"Отправлено: {r['sent']} · на подтверждении: {r['drafted']}\n"
        f"Передано вам (не оплатили после 2-го): {r['final']}"
    )


@router.message(Command("emojiid"))
async def cmd_emojiid(message: Message, state: FSMContext) -> None:
    """Harvest custom_emoji_id values: the owner sends a message with the Premium
    emoji they want, and the bot replies with a ready-to-paste CUSTOM_EMOJI_IDS."""
    await state.set_state(OwnerStates.getting_emoji_ids)
    await message.answer(
        "🎨 Пришлите <b>одним сообщением</b> премиум-эмодзи (анимированные), "
        "которые хотите использовать в сообщениях бота — я верну их ID для "
        "<code>CUSTOM_EMOJI_IDS</code>.\n\n"
        "Вставлять такие эмодзи можно только с Telegram Premium. Можно прислать "
        "сразу несколько подряд. (/cancel — отмена)"
    )


@router.message(OwnerStates.getting_emoji_ids, Command("cancel"))
async def cancel_emojiid(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


@router.message(OwnerStates.getting_emoji_ids)
async def capture_emoji_ids(message: Message, state: FSMContext) -> None:
    text = message.text or message.caption or ""
    ents = [e for e in (message.entities or message.caption_entities or [])
            if e.type == "custom_emoji"]
    if not ents:
        await message.answer(
            "Не вижу премиум-эмодзи в сообщении. Пришлите анимированные эмодзи "
            "(нужен Telegram Premium) или /cancel."
        )
        return
    # offsets/lengths are UTF-16 based — extract_from handles that correctly.
    pairs: dict[str, str] = {}
    for e in ents:
        glyph = e.extract_from(text)
        if glyph:
            pairs[glyph] = e.custom_emoji_id
    await state.clear()
    lines = "\n".join(f"{g} → <code>{i}</code>" for g, i in pairs.items())
    merged = dict(settings.custom_emoji_ids)
    merged.update(pairs)
    snippet = json.dumps(merged, ensure_ascii=False)
    await message.answer(
        f"✅ Нашёл {len(pairs)} эмодзи:\n{lines}\n\n"
        "Добавьте в <code>.env</code> и перезапустите бота "
        "(уже сохранённые сохранены, новые добавлены):\n"
        f"<code>CUSTOM_EMOJI_IDS={html.escape(snippet)}</code>\n\n"
        "Ключ — обычный эмодзи, который бот заменит на ваш премиум-вариант "
        "в своих сообщениях. Клиентам без Premium покажется обычный эмодзи."
    )
    # Diagnostic: render the emoji in a message the bot sends DIRECTLY (not via
    # the business connection). If they animate here but stay plain for customers,
    # the blocker is Telegram's business-account limit, not the IDs.
    test = " ".join(
        f'<tg-emoji emoji-id="{i}">{g}</tg-emoji>' for g, i in pairs.items()
    )
    try:
        await message.answer(
            f"🔎 Проверка: {test}\n\n"
            "Если эмодзи выше анимированы — ID рабочие и у вас есть Premium. "
            "Если в чатах с клиентами они всё равно показываются обычными — это "
            "ограничение Telegram: кастомные эмодзи от бота не отображаются в "
            "сообщениях, отправленных от имени бизнес-аккаунта."
        )
    except TelegramBadRequest:
        await message.answer(
            "⚠️ Не удалось показать эмодзи (Telegram отклонил кастомные эмодзи). "
            "Обычно это значит, что у аккаунта-владельца бота нет Telegram Premium "
            "или ID недействителен."
        )


# ── draft approval callbacks (not owner-filtered above, so filter here) ─────
@router.callback_query(F.data.startswith("d:"))
async def on_draft_action(call: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    if call.from_user.id not in settings.owner_ids:
        await call.answer()
        return
    _, action, draft_id_s = call.data.split(":")
    draft_id = int(draft_id_s)
    draft = await local.get_draft(draft_id)
    if not draft:
        await call.answer("Черновик не найден", show_alert=True)
        return

    if action == "skip":
        await local.delete_draft(draft_id)
        await call.message.edit_text(call.message.html_text + "\n\n❌ Пропущено")
        await call.answer("Пропущено")
        return

    if action == "edit":
        await state.set_state(OwnerStates.editing_draft)
        await state.update_data(draft_id=draft_id)
        await call.message.answer("✏️ Пришлите новый текст сообщения:")
        await call.answer()
        return

    if action == "send":
        ok, err = await deliver(bot, draft["conn_id"], draft["chat_id"], draft["text"])
        if ok:
            await local.add_message(draft["chat_id"], "assistant", draft["text"])
            await local.delete_draft(draft_id)
            await call.message.edit_text(call.message.html_text + "\n\n✅ Отправлено")
            await call.answer("Отправлено")
        else:
            status = await payments.get_customer_status(draft["chat_id"])
            username = status.get("username") if status else None
            await send_manual_card(
                bot,
                call.from_user.id,
                draft["conn_id"],
                draft["chat_id"],
                draft["kind"],
                draft["text"],
                err,
                username=username,
            )
            await local.delete_draft(draft_id)
            await call.message.edit_text(
                call.message.html_text + "\n\n⚠️ Не удалось отправить автоматически. "
                "Создал карточку для ручной отправки."
            )
            await call.answer("Нужно отправить вручную")


@router.message(OwnerStates.editing_draft, F.text)
async def on_edit_draft(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    draft_id = data.get("draft_id")
    draft = await local.get_draft(draft_id) if draft_id else None
    if not draft:
        await state.clear()
        await message.answer("Черновик не найден.")
        return
    ok, err = await deliver(bot, draft["conn_id"], draft["chat_id"], message.text)
    await state.clear()
    if ok:
        await local.add_message(draft["chat_id"], "assistant", message.text)
        await local.delete_draft(draft_id)
        await message.answer("✅ Отправлено.")
    else:
        await message.answer(f"❌ Не удалось отправить: {err}")


@router.message(F.text)
async def on_owner_text_question(message: Message, bot: Bot) -> None:
    """Treat ordinary private owner messages as DB-backed client questions."""
    text = (message.text or "").strip()
    if message.chat.type != "private":
        return
    if not text:
        return
    if text.startswith("/"):
        await message.answer(
            "Команды: /status · /style · /auto · /client · /nudge · /emojiid\n"
            "Или напишите обычный вопрос про клиента: @username, Telegram ID, "
            "имя, просрочки, заявки."
        )
        return
    if _wants_reminder_drafts(text):
        await _create_reminder_drafts(message, bot)
        return
    await _answer_admin_question(message, bot, text)


async def _answer_admin_question(message: Message, bot: Bot, question: str) -> None:
    try:
        await bot.send_chat_action(message.chat.id, "typing")
    except Exception:  # noqa: BLE001
        pass
    reply = await admin_assistant.answer(question)
    await message.answer(reply, parse_mode=None)


def _wants_reminder_drafts(text: str) -> bool:
    low = text.lower()
    return (
        any(word in low for word in ("напомин", "напомн", "remind", "reminder"))
        and any(word in low for word in ("кажд", "всем", "должн", "просроч"))
        and any(word in low for word in ("отправ", "чернов", "кноп", "send"))
    )


async def _create_reminder_drafts(message: Message, bot: Bot) -> None:
    owner_id = message.from_user.id
    conn = await local.get_owner_connection(owner_id)
    if not conn:
        await message.answer(
            "Нет активного Business-подключения. Сначала подключите бота к "
            "аккаунту менеджера и включите право отвечать на сообщения."
        )
        return

    overdue = await payments.get_overdue_customers(settings.proactive_overdue_days)
    if not overdue:
        await message.answer("Просроченных клиентов сейчас нет.")
        return

    await message.answer("Готовлю черновики напоминаний с кнопками отправки.")
    seen: set[int] = set()
    drafted = 0
    stage1 = 0
    stage2 = 0

    for row in overdue:
        customer_id = row["user_id"]
        if customer_id in seen:
            continue
        seen.add(customer_id)

        status = await payments.get_customer_status(customer_id)
        if not status:
            continue
        overdue_entries = [e for e in status["entries"] if e["is_overdue"]]
        if not overdue_entries:
            continue
        primary = overdue_entries[0]
        stage = await _reminder_draft_stage(customer_id, primary)
        text = prompts.nudge_text(stage, primary)
        await send_or_approve(
            bot,
            owner_id,
            conn["id"],
            customer_id,
            "outreach",
            text,
            auto=False,
            username=status.get("username"),
        )
        drafted += 1
        if stage == 1:
            stage1 += 1
        else:
            stage2 += 1

    await message.answer(
        f"Готово: создано черновиков {drafted}.\n"
        f"Первое напоминание: {stage1}, повторное: {stage2}.\n"
        "В каждом черновике есть кнопки «Отправить», «Изменить», «Пропустить»."
    )


async def _reminder_draft_stage(customer_id: int, entry: dict) -> int:
    next_payment = entry.get("next_payment_date")
    cycle_due = next_payment.isoformat() if next_payment else "?"
    state = await local.get_outreach_state(customer_id)
    if state and state["cycle_due"] == cycle_due and state["stage"] >= 1:
        return 2
    return 1
