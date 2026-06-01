"""System-prompt construction and safety triggers.

The model speaks AS the manager. It must answer ONLY from the grounded
context block we inject — never invent prices, dates, or card numbers — and
must escalate anything sensitive to a human.
"""

from __future__ import annotations

from datetime import date

# Topics we never auto-answer — drafts go to the manager for approval instead.
ESCALATION_TRIGGERS = (
    "уже оплат", "оплатил", "оплатила", "перевёл", "перевел", "скинул",
    "верните", "возврат", "верни деньги", "не работает", "обман", "мошен",
    "верну", "чек", "квитанц", "жалоб", "верните деньги", "развод",
    "refund", "scam", "already paid", "doesn't work", "not working",
)

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
- Отвечай ТОЛЬКО на основе данных из блока «ДАННЫЕ КЛИЕНТА» ниже. \
Никогда не выдумывай суммы, даты, номера карт или реквизиты.
- Если клиент спрашивает то, чего нет в данных, или хочет реквизиты для оплаты — \
коротко скажи, что сейчас уточнишь / пришлёшь, и не придумывай цифры.
- Никогда не обещай возвраты, скидки и не подтверждай оплату сам.
- Пиши на языке клиента (русский по умолчанию, можно казахский/английский).
- Коротко. Не пиши простыни. Без официального тона.
- Твоя цель в проактивных сообщениях — вежливо напомнить про оплату и помочь оплатить.

{context}
"""


def build_context_block(status: dict | None) -> str:
    """Render the grounded customer data the model is allowed to use."""
    if not status:
        return ("ДАННЫЕ КЛИЕНТА: клиент не найден в базе платежей. "
                "Будь вежлив, уточни детали, ничего не выдумывай.")
    lines = ["ДАННЫЕ КЛИЕНТА:"]
    name = status.get("first_name") or "клиент"
    lines.append(f"- Имя: {name}")
    today = date.today()
    for g in status.get("groups", []):
        npd = g["next_payment_date"]
        d = g["days_until_due"]
        if d is None:
            when = "дата оплаты неизвестна"
        elif d < 0:
            when = f"ПРОСРОЧЕНО на {-d} дн. (срок был {npd:%d.%m.%Y})"
        elif d == 0:
            when = f"оплата сегодня ({npd:%d.%m.%Y})"
        else:
            when = f"оплатить до {npd:%d.%m.%Y} (через {d} дн.)"
        lines.append(
            f"- Группа {g['group_display_id']} ({g['region']}): "
            f"{g['amount']}{g['currency']}/мес — {when}"
        )
    lines.append(f"(сегодня {today:%d.%m.%Y})")
    return "\n".join(lines)


def system_prompt(status: dict | None, style: str | None) -> str:
    return _SYSTEM.format(
        style=(style or DEFAULT_STYLE).strip(),
        context=build_context_block(status),
    )


def needs_escalation(text: str) -> bool:
    low = text.lower()
    return any(trig in low for trig in ESCALATION_TRIGGERS)


def outreach_instruction(status: dict) -> str:
    """User-turn instruction that asks the model to draft a first nudge."""
    overdue = [g for g in status["groups"] if g["is_overdue"]]
    g = overdue[0] if overdue else status["groups"][0]
    return (
        "Напиши первое короткое дружелюбное сообщение этому клиенту с напоминанием "
        f"оплатить подписку (группа {g['group_display_id']}, {g['amount']}{g['currency']}). "
        "Не дави, просто по-человечески напомни и предложи помощь с оплатой. "
        "Одно сообщение, без приветственных шаблонов вроде «Здравствуйте, уважаемый»."
    )
