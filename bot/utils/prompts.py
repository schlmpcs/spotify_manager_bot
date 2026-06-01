"""System-prompt construction and safety triggers.

The model speaks AS the manager. It must answer ONLY from the grounded
context block we inject — never invent prices, dates, or card numbers — and
must escalate anything sensitive to a human.
"""

from __future__ import annotations

from datetime import date

from config import settings

# Topics we never auto-answer — drafts go to the manager for approval instead.
ESCALATION_TRIGGERS = (
    "уже оплат", "оплатил", "оплатила", "перевёл", "перевел", "скинул",
    "верните", "возврат", "верни деньги", "не работает", "обман", "мошен",
    "верну", "чек", "квитанц", "жалоб", "верните деньги", "развод",
    "refund", "scam", "already paid", "doesn't work", "not working",
)

# Marker the model prepends when it can't answer from the grounded data and the
# question must go to a human. `business.py` strips it, sends the customer the
# reassurance text, and pings the manager to follow up. Keep it distinctive so
# it never collides with normal customer-facing prose.
CALL_MANAGER_MARKER = "[[CALL_MANAGER]]"

# Sent to the customer when the model emits the marker but no text of its own.
CALL_MANAGER_FALLBACK = "Сейчас уточню у менеджера и вернусь к вам 🙏"

DEFAULT_STYLE = (
    "Пиши неформально, по-дружески, кратко и по делу. Без канцелярита, "
    "без длинных абзацев, максимум пара коротких предложений. Можно лёгкий "
    "смайлик, но не перебарщивай."
)

_SYSTEM = """Ты — менеджер сервиса Spotify Premium (семейная подписка) в Казахстане и России. \
Ты отвечаешь клиентам ОТ ПЕРВОГО ЛИЦА, как живой человек-менеджер, а не бот.

Стиль общения менеджера:
{style}

Строгие правила:
- Отвечай ТОЛЬКО на основе данных из блоков «ТАРИФЫ» и «ДАННЫЕ КЛИЕНТА» ниже. \
Никогда не выдумывай даты, номера карт или реквизиты для оплаты.
- Цены можно и нужно называть из блока «ТАРИФЫ» — даже если клиента нет в базе \
(например, новый клиент спрашивает «сколько стоит»). Если не знаешь страну клиента — \
коротко спроси (Казахстан или Россия) или назови оба варианта.
- НЕ ВЫДУМЫВАЙ ответы. Если ты не знаешь ответа, вопрос не про тарифы/оплату \
(технические проблемы, особые просьбы, спорные ситуации) или нужных данных нет \
ни в «ТАРИФАХ», ни в «ДАННЫХ КЛИЕНТА» — НЕ пытайся ответить и не гадай. Вместо \
этого коротко и по-человечески скажи, что уточнишь у менеджера и вернёшься с \
ответом, и поставь в САМОМ НАЧАЛЕ ответа маркер {marker} (клиент его не увидит). \
Лучше честно передать вопрос менеджеру, чем придумать неверный ответ.
- Это касается и реквизитов для оплаты, если их нет в данных: скажи, что \
менеджер пришлёт реквизиты, и поставь маркер {marker}.
- Никогда не обещай возвраты, скидки и не подтверждай оплату сам.
- Пиши на языке клиента (русский по умолчанию, можно казахский/английский).
- Коротко. Не пиши простыни. Без официального тона.
- Пиши обычным текстом, как человек в Telegram. БЕЗ Markdown-разметки: \
никаких **жирного**, _курсива_, #заголовков, маркеров «- » или «* » в списках. \
Если перечисляешь цены — просто короткими строками с новой строки.
- Твоя цель в проактивных сообщениях — вежливо напомнить про оплату и помочь оплатить.

{prices}

{context}
"""


def price_list() -> str:
    """General tariff block — real config prices the bot may quote to anyone."""
    return (
        "ТАРИФЫ (актуальные цены, можно называть клиентам):\n"
        f"- Семейная подписка (слот в общей группе): "
        f"{settings.kz_group_price}₸/мес (Казахстан) · {settings.ru_group_price}₽/мес (Россия)\n"
        f"- Индивидуальная подписка: "
        f"{settings.kz_individual_price}₸/мес (Казахстан) · {settings.ru_individual_price}₽/мес (Россия)\n"
        f"- Duo (на двоих): "
        f"{settings.kz_duo_price}₸/мес (Казахстан) · {settings.ru_duo_price}₽/мес (Россия)"
    )


# Per-segment OBJECTIVE — frames *what* the message should achieve and *which
# facts* are relevant. It must never dictate tone/voice (that's the manager's
# own /style); it only sets the goal grounded in the customer's payment stage.
SEGMENT_OBJECTIVE = {
    "overdue": "ЗАДАЧА: у клиента есть просроченная оплата — помоги ему оплатить "
               "(подскажи, как и куда, но реквизиты не выдумывай; при необходимости "
               "скажи, что пришлёшь их).",
    "due_today": "ЗАДАЧА: оплата клиента сегодня — при случае напомни и помоги оплатить.",
    "due_soon": "ЗАДАЧА: ответь по сути вопроса; срок оплаты приближается, можно упомянуть.",
    "paid_ahead": "ЗАДАЧА: подписка оплачена надолго вперёд — НЕ напоминай об оплате.",
    "active": "ЗАДАЧА: подписка активна, оплата не требуется в ближайшее время — "
              "просто помоги по вопросу клиента.",
    "unknown": "ЗАДАЧА: срок оплаты неизвестен — не утверждай ничего про даты и суммы, "
               "при необходимости уточни.",
}


def _when_phrase(d: int | None, npd) -> str:
    if d is None:
        return "дата оплаты неизвестна"
    if d < 0:
        return f"ПРОСРОЧЕНО на {-d} дн. (срок был {npd:%d.%m.%Y})"
    if d == 0:
        return f"оплата сегодня ({npd:%d.%m.%Y})"
    return f"оплатить до {npd:%d.%m.%Y} (через {d} дн.)"


def build_context_block(status: dict | None) -> str:
    """Render the grounded customer data the model is allowed to use."""
    if not status:
        bot = settings.purchase_bot_username
        return (
            "ДАННЫЕ КЛИЕНТА: клиент не найден в базе платежей — это НОВЫЙ "
            "клиент, ещё не оформивший подписку.\n"
            f"ЗАДАЧА: помоги ему подключиться. Оформление и оплата подписки "
            f"происходят ТОЛЬКО через бота @{bot} — направь клиента туда "
            f"(«чтобы подключиться, напишите боту @{bot}»). Цены можешь назвать "
            "из блока «ТАРИФЫ». Сам реквизиты/ссылки не выдумывай и не проводи "
            "оплату здесь — всё оформление на стороне бота. На вопросы отвечай "
            f"здесь. Подробности также есть в канале: {settings.channel_url}."
        )
    lines = ["ДАННЫЕ КЛИЕНТА:"]
    name = status.get("first_name") or "клиент"
    lines.append(f"- Имя: {name}")
    today = date.today()
    for e in status.get("entries", []):
        when = _when_phrase(e["days_until_due"], e["next_payment_date"])
        # "группа 132" already reads as a label; individual/duo carry their own.
        what = e["label"][0].upper() + e["label"][1:]
        lines.append(
            f"- {what} ({e['region']}): {e['amount']}{e['currency']}/мес — {when}"
        )
    lines.append(f"(сегодня {today:%d.%m.%Y})")
    lines.append(SEGMENT_OBJECTIVE.get(status.get("segment", "unknown"),
                                       SEGMENT_OBJECTIVE["unknown"]))
    return "\n".join(lines)


def system_prompt(status: dict | None, style: str | None) -> str:
    return _SYSTEM.format(
        style=(style or DEFAULT_STYLE).strip(),
        prices=price_list(),
        context=build_context_block(status),
        marker=CALL_MANAGER_MARKER,
    )


def needs_escalation(text: str) -> bool:
    low = text.lower()
    return any(trig in low for trig in ESCALATION_TRIGGERS)


def split_call_manager(reply: str) -> tuple[bool, str]:
    """Detect the manager-handoff marker in a model reply.

    Returns ``(wants_manager, customer_text)`` — the marker stripped out, and a
    sensible fallback substituted if the model emitted only the marker.
    """
    if CALL_MANAGER_MARKER not in reply:
        return False, reply
    clean = reply.replace(CALL_MANAGER_MARKER, "").strip()
    return True, clean or CALL_MANAGER_FALLBACK


def outreach_instruction(status: dict) -> str:
    """User-turn instruction that asks the model to draft a first nudge."""
    entries = status.get("entries") or []
    overdue = [e for e in entries if e["is_overdue"]]
    e = overdue[0] if overdue else (entries[0] if entries else None)
    what = f"{e['label']}, {e['amount']}{e['currency']}" if e else "подписку"
    return (
        "Напиши первое короткое дружелюбное сообщение этому клиенту с напоминанием "
        f"оплатить ({what}). "
        "Не дави, просто по-человечески напомни и предложи помощь с оплатой. "
        "Одно сообщение, без приветственных шаблонов вроде «Здравствуйте, уважаемый»."
    )
