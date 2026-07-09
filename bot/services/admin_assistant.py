"""Owner-side assistant for asking about clients in the payment database."""

from __future__ import annotations

import re
from datetime import date

from bot.db import payments
from bot.services import llm

_USERNAME_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]{4,31})")
_ID_RE = re.compile(r"\b\d{5,15}\b")
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_]+")
_DAYS_RE = re.compile(r"\b(\d{1,3})\s*(?:дн|день|дня|дней|day|days)\b", re.I)

_STOPWORDS = {
    "about", "admin", "all", "any", "ask", "client", "clients", "customer",
    "customers", "data", "find", "for", "give", "how", "info", "list", "look",
    "me", "need", "payment", "please", "show", "status", "tell", "the", "user",
    "what", "when", "where", "which", "who",
    "базе", "будет", "бы", "все", "всех", "где", "данные", "дата", "дату",
    "должен", "должна", "должники", "должников", "есть", "заявка", "заявки",
    "клиент", "клиента", "клиенты", "клиентов", "когда", "кому", "кто",
    "мне", "надо", "найди", "оплата", "оплате", "оплатил", "оплатить",
    "платеж", "платёж", "по", "покажи", "подписка", "подписке", "про",
    "просрочка", "просрочен", "сколько", "статус", "что",
}

_OVERDUE_WORDS = (
    "overdue", "debt", "debtor", "late", "просроч", "должн", "долг",
)
_DUE_WORDS = (
    "due", "today", "tomorrow", "soon", "сегодня", "завтра", "скоро",
    "оплатить", "срок",
)
_PENDING_WORDS = (
    "pending", "request", "заявк", "ожидан", "подключен", "подключить",
)
_STATS_WORDS = (
    "summary", "stats", "statistics", "count", "counts", "сколько",
    "статист", "сводк", "итог",
)

_ADMIN_SYSTEM = """You are an internal assistant for the Spotify service manager.
Answer the manager/admin, not the customer.
Use only the CLIENT DATA block. Do not invent dates, prices, payments, usernames,
or subscription state. If the data is missing, say that it is missing.
Be concise and practical. Plain text only, no HTML."""


async def answer(question: str) -> str:
    """Answer an owner/admin question using read-only payment DB helpers."""
    text = (question or "").strip()
    if not text:
        return "Напишите вопрос про клиента: Telegram ID, @username или имя."

    found, missing = await _statuses_from_refs(text)
    if found:
        return await _answer_about_statuses(text, found, missing)
    if missing:
        return "Не найдено в базе: " + ", ".join(missing)

    candidates = await _search_candidates(text)
    if len(candidates) == 1:
        status = await _status_from_identity(candidates[0])
        return await _answer_about_statuses(text, [status], [])
    if len(candidates) > 1:
        return _format_candidates(candidates)

    low = text.lower()
    if _has_any(low, _STATS_WORDS):
        return _format_stats(await payments.get_client_stats())
    if _has_any(low, _PENDING_WORDS):
        return _format_pending(await payments.get_pending_requests(limit=20))
    if _has_any(low, _OVERDUE_WORDS):
        rows = await payments.get_overdue_customers(min_days_overdue=1)
        return _format_entries("Просроченные клиенты", rows, empty="Просрочек нет.")
    if _has_any(low, _DUE_WORDS):
        days = _extract_days(low)
        rows = await payments.get_due_customers(max_days=days, limit=20)
        title = "Клиенты с оплатой сегодня" if days == 0 else f"Оплаты в ближайшие {days} дн."
        return _format_entries(title, rows, empty="В этот период оплат нет.")

    return (
        "Не понял, какого клиента проверить.\n"
        "Напишите Telegram ID, @username или имя. Например: @username, "
        "123456789, или \"кто просрочил оплату\"."
    )


async def _statuses_from_refs(text: str) -> tuple[list[dict], list[str]]:
    found: list[dict] = []
    missing: list[str] = []
    seen: set[int] = set()

    for username in dict.fromkeys(_USERNAME_RE.findall(text)):
        ident = await payments.get_user_identity_by_username(username)
        if not ident:
            missing.append(f"@{username}")
            continue
        status = await _status_from_identity(ident)
        if status["user_id"] not in seen:
            seen.add(status["user_id"])
            found.append(status)

    for raw_id in dict.fromkeys(_ID_RE.findall(text)):
        user_id = int(raw_id)
        if user_id in seen:
            continue
        ident = await payments.get_user_identity(user_id)
        if not ident:
            status = await payments.get_customer_status(user_id)
            if status:
                seen.add(user_id)
                found.append(status)
            else:
                missing.append(raw_id)
            continue
        status = await _status_from_identity(ident)
        seen.add(status["user_id"])
        found.append(status)

    return found, missing


async def _status_from_identity(identity: dict) -> dict:
    status = await payments.get_customer_status(identity["user_id"])
    if status:
        return status
    return {
        "user_id": identity["user_id"],
        "first_name": identity.get("first_name"),
        "username": identity.get("username"),
        "entries": [],
        "pending_requests": [],
        "any_overdue": False,
        "segment": "no_active_subscription",
    }


async def _search_candidates(text: str) -> list[dict]:
    terms = _search_terms(text)
    if not terms:
        return []

    found: dict[int, dict] = {}
    for term in terms:
        for item in await payments.find_customers(term, limit=8):
            found.setdefault(item["user_id"], item)
        if len(found) >= 8:
            break
    return list(found.values())[:8]


def _search_terms(text: str) -> list[str]:
    cleaned = _USERNAME_RE.sub(" ", text)
    cleaned = _ID_RE.sub(" ", cleaned)
    terms: list[str] = []
    for raw in _WORD_RE.findall(cleaned):
        term = raw.strip("_").lower()
        if len(term) < 3 or term in _STOPWORDS or term.isdigit():
            continue
        if any(word in term for word in ("клиент", "подпис", "оплат", "просроч")):
            continue
        terms.append(term)
    return sorted(dict.fromkeys(terms), key=len, reverse=True)


async def _answer_about_statuses(
    question: str, statuses: list[dict], missing: list[str]
) -> str:
    context = "\n\n".join(_status_context(s) for s in statuses[:5])
    if missing:
        context += "\n\nNOT FOUND: " + ", ".join(missing)
    reply = await llm.chat(
        [
            {"role": "system", "content": _ADMIN_SYSTEM},
            {
                "role": "user",
                "content": f"Manager question:\n{question}\n\nCLIENT DATA:\n{context}",
            },
        ],
        temperature=0.2,
    )
    if reply:
        return reply.strip()
    return _format_statuses(statuses, missing)


def _status_context(status: dict) -> str:
    lines = [
        f"client_id: {status['user_id']}",
        f"name: {status.get('first_name') or 'unknown'}",
        f"username: @{status['username']}" if status.get("username") else "username: missing",
        f"segment: {status.get('segment') or 'unknown'}",
        f"today: {date.today():%Y-%m-%d}",
    ]
    entries = status.get("entries") or []
    if entries:
        lines.append("subscriptions:")
        for entry in entries:
            next_payment = _fmt_date(entry.get("next_payment_date"))
            group = f"; group={entry.get('group_display_id')}" if entry.get("group_display_id") else ""
            lines.append(
                "- "
                f"{entry['label']}; kind={entry['kind']}; region={entry['region']}; "
                f"amount={entry['amount']}{entry['currency']}/month; "
                f"next_payment_date={next_payment}; "
                f"{_days_text(entry.get('days_until_due'))}{group}"
            )
    else:
        lines.append("subscriptions: no active group, individual, or duo subscription found")

    pending = status.get("pending_requests") or []
    if pending:
        lines.append("pending_requests:")
        for req in pending:
            created = _fmt_date(req.get("created_at"))
            lines.append(
                f"- request_id={req['request_id']}; {req['label']}; "
                f"region={req.get('region') or 'unknown'}; created={created}"
            )
    return "\n".join(lines)


def _format_statuses(statuses: list[dict], missing: list[str]) -> str:
    lines: list[str] = []
    for status in statuses:
        lines.append(_short_status(status))
    if missing:
        lines.append("Не найдено: " + ", ".join(missing))
    return "\n\n".join(lines)


def _short_status(status: dict) -> str:
    name = status.get("first_name") or "без имени"
    username = f"@{status['username']}" if status.get("username") else "без username"
    lines = [f"{name} ({username}, id {status['user_id']})"]
    entries = status.get("entries") or []
    if not entries:
        lines.append("- активной подписки не найдено")
    for entry in entries:
        lines.append(
            f"- {entry['label']}, {entry['region']}: {entry['amount']}{entry['currency']}, "
            f"{_days_text(entry.get('days_until_due'))}, дата {_fmt_date(entry.get('next_payment_date'))}"
        )
    pending = status.get("pending_requests") or []
    for req in pending:
        lines.append(
            f"- заявка {req['request_id']}: {req['label']}, создана {_fmt_date(req.get('created_at'))}"
        )
    return "\n".join(lines)


def _format_candidates(candidates: list[dict]) -> str:
    lines = ["Нашёл несколько клиентов. Уточните ID или @username:"]
    for item in candidates:
        username = f"@{item['username']}" if item.get("username") else "без username"
        name = item.get("first_name") or "без имени"
        lines.append(f"- {name} ({username}, id {item['user_id']})")
    return "\n".join(lines)


def _format_entries(title: str, rows: list[dict], *, empty: str) -> str:
    if not rows:
        return empty
    shown = rows[:20]
    lines = [f"{title}: {len(rows)}"]
    for idx, entry in enumerate(shown, start=1):
        lines.append(f"{idx}. {_entry_line(entry)}")
    if len(rows) > len(shown):
        lines.append(f"Показаны первые {len(shown)} из {len(rows)}.")
    return "\n".join(lines)


def _entry_line(entry: dict) -> str:
    name = entry.get("first_name") or "без имени"
    username = f"@{entry['username']}" if entry.get("username") else f"id {entry['user_id']}"
    return (
        f"{name} ({username}) - {entry['label']}, {entry['region']}, "
        f"{entry['amount']}{entry['currency']}, {_days_text(entry.get('days_until_due'))}, "
        f"дата {_fmt_date(entry.get('next_payment_date'))}"
    )


def _format_pending(rows: list[dict]) -> str:
    if not rows:
        return "Активных заявок на подключение нет."
    lines = [f"Активные заявки: {len(rows)}"]
    for idx, req in enumerate(rows[:20], start=1):
        name = req.get("first_name") or "без имени"
        who = f"@{req['username']}" if req.get("username") else f"id {req['user_id']}"
        lines.append(
            f"{idx}. {name} ({who}) - {req['label']}, "
            f"{req.get('region') or 'регион неизвестен'}, создана {_fmt_date(req.get('created_at'))}"
        )
    return "\n".join(lines)


def _format_stats(stats: dict) -> str:
    if not stats:
        return "Не получилось получить статистику из базы."
    return (
        "Сводка по базе:\n"
        f"- активных пользователей: {stats['active_users']}\n"
        f"- групповых слотов: {stats['group_slots']} "
        f"(пользователей: {stats['group_users']})\n"
        f"- индивидуальных: {stats['individual_clients']}\n"
        f"- Duo: {stats['duo_clients']}\n"
        f"- пользователей с individual/Duo: {stats['individual_users']}\n"
        f"- просрочивших пользователей: {stats['overdue_users']}\n"
        f"- pending-заявок: {stats['pending_requests']}"
    )


def _extract_days(text: str) -> int:
    if "сегодня" in text or "today" in text:
        return 0
    if "завтра" in text or "tomorrow" in text:
        return 1
    match = _DAYS_RE.search(text)
    if match:
        return max(0, min(int(match.group(1)), 365))
    return 7


def _days_text(days: int | None) -> str:
    if days is None:
        return "срок неизвестен"
    if days < 0:
        return f"просрочено на {-days} дн."
    if days == 0:
        return "оплата сегодня"
    return f"оплата через {days} дн."


def _fmt_date(value) -> str:
    if not value:
        return "неизвестно"
    if hasattr(value, "strftime"):
        return value.strftime("%d.%m.%Y")
    return str(value)


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)
