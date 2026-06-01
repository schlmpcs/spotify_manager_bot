"""Commands for the manager (owner) to configure and control the bot."""

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import settings
from bot.db import local
from bot.services.dispatch import deliver
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
        "Команды: /status · /style · /auto · /nudge"
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


@router.message(Command("nudge"))
async def cmd_nudge(message: Message, bot: Bot) -> None:
    """Run the overdue-customer outreach right now."""
    from bot.services.outreach import run_outreach
    await message.answer("📣 Запускаю напоминания должникам…")
    sent, drafted, failed = await run_outreach(bot)
    await message.answer(
        f"Готово. Отправлено: {sent}, на подтверждении: {drafted}, не доставлено: {failed}."
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
            await call.answer(f"Не удалось: {err}", show_alert=True)


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
