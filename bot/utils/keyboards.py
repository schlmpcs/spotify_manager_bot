from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def draft_kb(draft_id: int) -> InlineKeyboardMarkup:
    """Approve / edit / skip a drafted message before it goes out as the manager."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отправить", callback_data=f"d:send:{draft_id}"),
        InlineKeyboardButton(text="✏️ Изменить", callback_data=f"d:edit:{draft_id}"),
        InlineKeyboardButton(text="❌ Пропустить", callback_data=f"d:skip:{draft_id}"),
    ]])


def manual_send_kb(draft_id: int, chat_url: str | None) -> InlineKeyboardMarkup:
    """For a draft the bot can't deliver (e.g. the customer is outside Telegram's
    24h business-reply window). The bot can't send it, so the only real action is
    for the manager to send it by hand: a one-tap link to open the chat (when we
    know the @username) plus a button to dismiss the card. No "Отправить" — it
    would just fail again through the same business connection."""
    rows = []
    if chat_url:
        rows.append([InlineKeyboardButton(text="✍️ Открыть чат", url=chat_url)])
    rows.append([InlineKeyboardButton(text="✖️ Убрать", callback_data=f"d:skip:{draft_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
