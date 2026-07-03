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
CALL_MANAGER_FALLBACK = "Передам ваш вопрос менеджеру — он скоро свяжется с вами 🙏"

DEFAULT_STYLE = (
    "Пиши в нейтрально-дружелюбном, спокойном сервисном тоне: без лишней "
    "официальности, коротко по делу, с ощущением «сейчас поможем / подключим / "
    "проверим».\n"
    "Обращайся в основном на «вы»: «можете», «у вас», «вам», «подключили вас», "
    "«напишите мне»; иногда допускай мягкое «мы» от лица сервиса.\n"
    "Делай сообщения короткими: чаще одно предложение или 1–3 короткие фразы; "
    "длинные абзацы используй только для объявлений, инструкций, реквизитов или "
    "важных изменений.\n"
    "Не дави и не торопи клиента — тон спокойный и доброжелательный, как у "
    "живого менеджера поддержки, а не у бота.\n"
    "Здоровайся только в начале диалога, а не в каждом сообщении; дальше отвечай "
    "по сути, без повторных «здравствуйте».\n"
    "НИКОГДА не используй «Привет» / «Приветик» — только вежливое «Здравствуйте» "
    "или «Добрый день/утро/вечер».\n"
    "Эмодзи используй умеренно и к месту (например 💚 🙂 ✅ 📱), чтобы смягчить "
    "тон — не больше одного-двух на сообщение и не в каждом ответе.\n"
    "Всегда подсказывай следующий простой шаг («напишите мне», «сейчас проверю», "
    "«оформить можно у бота»), чтобы клиенту было понятно, что делать дальше.\n"
    "Без канцелярита и сухих шаблонов; пиши живым, человеческим языком."
)

_SYSTEM = """Ты — менеджер сервиса Spotify Premium в Казахстане и России. \
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
этого коротко и по-человечески скажи, что передашь вопрос менеджеру и он скоро \
свяжется с клиентом, и поставь в САМОМ НАЧАЛЕ ответа маркер {marker} (клиент его \
не увидит). Лучше честно передать вопрос менеджеру, чем придумать неверный ответ.
- Это касается и реквизитов для оплаты, если их нет в данных: скажи, что \
менеджер пришлёт реквизиты и скоро свяжется, и поставь маркер {marker}.
- Никогда не обещай возвраты, скидки и не подтверждай оплату сам.
- Пиши на языке клиента (русский по умолчанию, можно казахский/английский).
- Приветствуй клиента только вежливо: «Здравствуйте» или «Добрый день/утро/вечер». \
НИКОГДА не используй «Привет» / «Приветик».
- Коротко. Не пиши простыни. Без официального тона.
- Форматируй ТОЛЬКО через Telegram HTML, и только когда это правда помогает \
читаемости (выделить цену, тариф, ключевое слово, сгруппировать реквизиты). \
Разрешённые теги: <b>жирный</b>, <i>курсив</i>, <u>подчёркнутый</u>, \
<s>зачёркнутый</s>, <code>моноширинный</code> (удобно для номера карты — по \
нажатию он копируется), <a href="ссылка">текст</a>. \
НЕ используй Markdown (никаких **жирного**, _курсива_, #заголовков, «- »/«* »). \
НЕ используй <blockquote> (цитаты) — пиши обычным текстом. \
Telegram НЕ поддерживает HTML-списки и заголовки (<ul>, <li>, <h1> и т.п.) — \
для перечислений (например цен) пиши каждый пункт с новой строки, выделяя \
название тарифа через <b>…</b>. Не переусердствуй с форматированием — короткий \
ответ на один вопрос форматировать не нужно, сообщение должно выглядеть живым, \
а не как страница с разметкой. Символы < > & вне тегов не используй (или пиши \
словами).
- Добавляй эмодзи и в обычные сообщения (например когда называешь клиенту его \
статус, тариф или срок оплаты), чтобы они выглядели живыми и аккуратными — \
1–2 уместных на сообщение. Эмодзи ВСЕГДА позитивные и доброжелательные \
(например ✨ 💚 🙂 ✅ 🎵 👍 😊), даже если оплата просрочена. НИКОГДА не используй \
негативные, тревожные или «ругающие» эмодзи (❌ ⚠️ 😡 😞 😢 🚫 ⛔️ 💀) и не нагнетай \
тон — даже про просрочку пиши спокойно и по-доброму.
- Твоя цель в проактивных сообщениях — вежливо напомнить про оплату и помочь оплатить.

{prices}

{context}
"""


# Marker the model emits instead of typing out a full price list. The bot swaps
# it for `price_table()` — a perfectly-aligned, deterministic table. Done this way
# because the model can't reliably reproduce exact HTML/whitespace (it flattens
# blockquotes and column alignment), but it CAN reliably emit a short literal
# marker (same trick as CALL_MANAGER_MARKER).
PRICE_TABLE_MARKER = "[[PRICE_TABLE]]"


def price_list() -> str:
    """General tariff block injected into the prompt — real config prices the bot
    may quote to anyone. The data lives in ``settings`` — never hard-code amounts.

    For a *full* price-list request the model must NOT type the table itself
    (it mangles the formatting); it emits ``PRICE_TABLE_MARKER`` and the bot
    substitutes the deterministic ``price_table()``."""
    return (
        "ТАРИФЫ (актуальные цены, можно называть клиентам):\n"
        f"- Индивидуальная подписка: "
        f"{settings.kz_individual_price}₸/мес (Казахстан) · {settings.ru_individual_price}₽/мес (Россия)\n"
        f"- Duo (на двоих): "
        f"{settings.kz_duo_price}₸/мес (Казахстан) · {settings.ru_duo_price}₽/мес (Россия)\n"
        "\n"
        "Когда клиент просит ПОЛНЫЙ список цен / прайс / «сколько стоят подписки» — "
        f"НЕ печатай цены и таблицу сам. Просто вставь на ОТДЕЛЬНОЙ строке маркер "
        f"{PRICE_TABLE_MARKER} — система сама подставит красиво оформленную таблицу "
        "с актуальными ценами. Можно добавить короткую живую фразу до маркера "
        "(например «Конечно, вот наши тарифы:»). Если же спрашивают про ОДИН "
        "конкретный тариф (например «сколько Duo в Казахстане») — можно просто "
        "назвать эту цену словами, без маркера."
    )


def price_table() -> str:
    """Deterministic, perfectly-aligned customer-facing price table.

    Built entirely in code (never retyped by the model), so the formatting always
    renders. Uses a monospace <pre> block — the only reliable way for a bot to
    show an aligned grid (Telegram's native tables aren't exposed via bot HTML).
    Emoji are kept OUT of the <pre> cells so the columns stay aligned."""
    rows = [
        ("Тариф", "Казахстан", "Россия"),
        ("Индивид.", f"{settings.kz_individual_price}₸", f"{settings.ru_individual_price}₽"),
        ("Duo", f"{settings.kz_duo_price}₸", f"{settings.ru_duo_price}₽"),
    ]
    w0 = max(len(r[0]) for r in rows)
    w1 = max(len(r[1]) for r in rows)
    w2 = max(len(r[2]) for r in rows)

    def _row(r: tuple[str, str, str]) -> str:
        return f"{r[0]:<{w0}}  {r[1]:>{w1}}  {r[2]:>{w2}}"

    head = _row(rows[0])
    table = "\n".join([head, "─" * len(head), *[_row(r) for r in rows[1:]]])
    bot = settings.purchase_bot_username
    return (
        "🎵 <b>Цены на Spotify Premium</b>\n\n"
        f"<pre>{table}</pre>\n"
        f"✨ Подключиться → @{bot}"
    )


def insert_price_table(reply: str) -> str:
    """Swap the model's ``PRICE_TABLE_MARKER`` for the real deterministic table."""
    if PRICE_TABLE_MARKER not in reply:
        return reply
    return reply.replace(PRICE_TABLE_MARKER, price_table()).strip()


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

# Appended whenever the customer has a pending connection request. Takes priority
# over the segment objective: when someone asks "когда подключат подписку?" the
# answer is grounded in this — заявка принята, подключают в течение рабочего дня.
PENDING_REQUEST_OBJECTIVE = (
    "ВАЖНО: у клиента есть активная заявка на подключение — она уже принята и "
    "обрабатывается, подписка ещё НЕ подключена. Если клиент спрашивает, когда "
    "подключат/активируют подписку (или почему её ещё нет) — успокой его: заявка "
    "передана и принята, подписку подключат в течение одного рабочего дня. Если "
    "есть ещё вопросы — пусть напишет здесь, менеджер скоро свяжется. Не называй "
    "точное время, не обещай мгновенное подключение и не отправляй клиента "
    "оформлять заявку заново."
)

# Appended only for customers who have a GROUP (family) plan. Spotify has begun
# disconnecting family accounts, so a group subscriber who reports that their
# subscription stopped working must be told this honestly and offered the two
# real paths (switch to individual via the bot, or talk to the manager) — NOT
# punted with the call-manager marker as if it were an unknown technical fault.
GROUP_DISCONNECT_GUIDANCE = (
    "ВАЖНО (у клиента ГРУППОВАЯ / семейная подписка): если клиент пишет, что его "
    "подписка отключилась / слетела / перестала работать / его выкинуло из семьи — "
    "это НЕ техническая поломка и НЕ повод сразу передавать вопрос менеджеру. "
    "Объясни честно и спокойно: Spotify начал отключать пользователей семейных "
    "(групповых) подписок, и быстро восстановить групповое подключение, скорее "
    "всего, не получится. Предложи клиенту два варианта: 1) подключить "
    f"индивидуальную подписку через бота @{settings.purchase_bot_username} — она "
    "стабильнее и не зависит от семьи; 2) написать менеджеру, чтобы он подсказал "
    "по ситуации. Не обещай восстановление группы и не называй сроков. Маркер "
    f"{CALL_MANAGER_MARKER} в этом случае НЕ ставь — ты сам объясняешь ситуацию, "
    "менеджер только как один из вариантов по желанию клиента."
)

# Appended only for customers on an INDIVIDUAL or DUO plan. If their subscription
# fell off and they say they've paid, they don't need the manager — the purchase
# bot now has a one-tap "🔌 Переподключить" button that files the reconnect request
# itself (gated to active+paid clients). So we give the steps instead of punting
# with the call-manager marker. Button labels mirror the main bot exactly.
INDIVIDUAL_RECONNECT_GUIDANCE = (
    "ВАЖНО (у клиента ИНДИВИДУАЛЬНАЯ или Duo подписка): если клиент пишет, что его "
    "подписка отключилась / слетела / перестала работать И что он уже оплатил "
    "(или его подписка активна и оплачена) — это НЕ повод передавать вопрос "
    f"менеджеру, маркер {CALL_MANAGER_MARKER} в этом случае НЕ ставь. Переподключение "
    "теперь делается в один тап через бота — спокойно подскажи, как отправить "
    "заявку на переподключение:\n"
    f"1) Открыть бота @{settings.purchase_bot_username} и нажать кнопку "
    "«🔌 Переподключить» (она внизу на клавиатуре и под сообщением «📊 Мой статус»).\n"
    "2) Проверить свой Spotify-логин и нажать «✅ Отправить заявку» (или «✏️ Изменить "
    "логин и пароль», если данные поменялись).\n"
    "3) Менеджер переподключит в ближайшее время.\n"
    "Кнопка работает, только если подписка активна и оплачена. Если у клиента "
    "оплата просрочена — сначала помоги оплатить, переподключение станет доступно "
    "после оплаты. Сам оплату не подтверждай и не обещай мгновенное подключение."
)


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
    pending = status.get("pending_requests") or []
    if pending:
        p = pending[0]
        created = f", подана {p['created_at']:%d.%m.%Y}" if p.get("created_at") else ""
        lines.append(
            f"- ЗАЯВКА НА ПОДКЛЮЧЕНИЕ ({p['label']}{created}): принята, "
            "ещё обрабатывается — подписка пока не подключена."
        )
    lines.append(f"(сегодня {today:%d.%m.%Y})")
    # A pending request is what a "когда подключат?" question is really about, so
    # its objective wins over the payment-stage one.
    if pending:
        lines.append(PENDING_REQUEST_OBJECTIVE)
    else:
        lines.append(SEGMENT_OBJECTIVE.get(status.get("segment", "unknown"),
                                           SEGMENT_OBJECTIVE["unknown"]))
    if any(e.get("kind") == "group" for e in status.get("entries", [])):
        lines.append(GROUP_DISCONNECT_GUIDANCE)
    if any(e.get("kind") in ("individual", "duo") for e in status.get("entries", [])):
        lines.append(INDIVIDUAL_RECONNECT_GUIDANCE)
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


# Staged overdue nudges. These go out verbatim (no LLM) so the wording stays
# consistent and predictable — the manager owns this exact phrasing. Each nudge
# is grounded in the customer's own plan: the amount owed and the payment
# requisites for their region are appended (see `nudge_text`).
#   Day 1: gentle reminder + ask if they'll renew.
#   Day 2: firmer — warn that the subscription will be switched off.
# After the day-2 nudge goes unanswered, no further message is sent to the
# customer; the manager is notified instead (see outreach.run_outreach).
NUDGE_STAGE_1 = "Здравствуйте! Подписка не оплачена — будете продлевать?"
NUDGE_STAGE_2 = "Здравствуйте! Подписка всё ещё не оплачена. Отключать вас от подписки?"

# Indexed by the stage we're about to send (1 = first nudge, 2 = second nudge).
_NUDGE_LEAD = {1: NUDGE_STAGE_1, 2: NUDGE_STAGE_2}


def payment_requisites(region: str) -> str:
    """Payment details quoted to a customer, by region.

    Mirrors the main bot's ``get_payment_info`` ``payment_text``: KZ pays via a
    Kaspi link, RU via a card transfer (bank + card + recipient).
    """
    if (region or "").upper() == "RU":
        return (
            "💳 <b>Перевод на карту:</b>\n"
            f"🏦 Банк: {settings.ru_payment_bank}\n"
            f"💳 Карта: <code>{settings.ru_payment_card}</code>\n"
            f"👤 Получатель: {settings.ru_payment_recipient}"
        )
    return (
        "💳 <b>Оплата на Kaspi Bank:</b>\n"
        f"{settings.kz_payment_link}"
    )


def nudge_text(stage: int, entry: dict) -> str:
    """Build the staged overdue nudge for ``stage`` (1 or 2), grounded in the
    customer's own overdue ``entry``: appends the amount owed, the payment
    requisites for that plan's region, and a reminder to send the receipt to
    the purchase bot so the payment is registered."""
    lead = _NUDGE_LEAD[stage]
    amount, currency = entry["amount"], entry["currency"]
    return (
        f"{lead}\n\n"
        f"💚 К оплате: <b>{amount}{currency}/мес</b> за подписку Spotify\n\n"
        f"{payment_requisites(entry['region'])}\n\n"
        f"📩 После оплаты отправьте чек боту @{settings.purchase_bot_username}, "
        f"чтобы оплата зачлась."
    )
