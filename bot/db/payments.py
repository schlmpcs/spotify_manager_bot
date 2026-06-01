"""Read-only access to the main payment bot's PostgreSQL database.

Every AI message is grounded in this data so the bot never invents a due
date or amount. Queries mirror `get_overdue_users` in the main bot's
operations.py (same COALESCE-of-last-payment logic, same is_phantom guard).
"""

import logging
from datetime import date, datetime
from typing import Optional

import asyncpg

from config import settings

logger = logging.getLogger(__name__)
_pool: Optional[asyncpg.Pool] = None


async def connect() -> None:
    global _pool
    _pool = await asyncpg.create_pool(dsn=settings.database_dsn, min_size=1, max_size=4)
    logger.info("Connected to payment database")


async def close() -> None:
    if _pool:
        await _pool.close()


def _region(group_display_id: str) -> str:
    return "RU" if str(group_display_id).startswith("1") else "KZ"


def _price(region: str) -> int:
    return settings.ru_group_price if region == "RU" else settings.kz_group_price


def _currency(region: str) -> str:
    return "₽" if region == "RU" else "₸"


def _as_date(value) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    return value


# Human-readable product names for individual (non-group) subscriptions.
_PLAN_LABEL = {
    "individual": "индивидуальная подписка",
    "duo": "Duo-подписка (на двоих)",
    "family_individual": "семейная подписка (инд.)",
}


def _group_entry(row, today: date) -> dict:
    """One group-membership slot rendered as a unified status entry."""
    region = _region(row["group_display_id"])
    npd = _as_date(row["next_payment_date"])
    days = (npd - today).days if npd else None
    return {
        "kind": "group",
        "label": f"группа {row['group_display_id']}",
        "user_id": row["user_id"],
        "first_name": row["first_name"],
        "username": row["username"],
        "group_name": row["group_name"],
        "group_display_id": row["group_display_id"],
        "region": region,
        "currency": _currency(region),
        "amount": _price(region),
        "next_payment_date": npd,
        "days_until_due": days,          # negative == overdue
        "is_overdue": days is not None and days < 0,
    }


def _individual_entry(row, today: date) -> dict:
    """An individual / duo subscription rendered as a unified status entry.

    Unlike groups, `individual_clients` carries its own `region` and `price`
    columns, so we trust those rather than the display_id heuristic.
    """
    region = (row["region"] or "RU").upper()
    plan = row["plan"]
    npd = _as_date(row["next_payment_date"])
    days = (npd - today).days if npd else None
    return {
        "kind": plan if plan in ("individual", "duo") else "individual",
        "plan": plan,
        "label": _PLAN_LABEL.get(plan, "индивидуальная подписка"),
        "user_id": row["user_id"],
        "first_name": row["first_name"],
        "username": row["username"],
        "region": region,
        "currency": _currency(region),
        "amount": row["price"],
        "next_payment_date": npd,
        "days_until_due": days,          # negative == overdue
        "is_overdue": days is not None and days < 0,
    }


def _segment(entries: list[dict]) -> str:
    """Coarse payment-lifecycle stage used to frame the model's objective.

    Drives WHAT facts/goal we surface — never the manager's tone.
    """
    if not entries:
        return "unknown"
    if any(e["is_overdue"] for e in entries):
        return "overdue"
    days = [e["days_until_due"] for e in entries if e["days_until_due"] is not None]
    if not days:
        return "unknown"
    nearest = min(days)
    if nearest == 0:
        return "due_today"
    if nearest <= 7:
        return "due_soon"
    if nearest >= 32:
        return "paid_ahead"
    return "active"


_BASE_SELECT = """
    SELECT
        ug.user_id,
        u.username,
        u.first_name,
        g.group_name,
        g.display_id AS group_display_id,
        COALESCE(
            (SELECT next_payment_date FROM payments
             WHERE user_id = ug.user_id AND group_id = g.group_id
             ORDER BY payment_id DESC LIMIT 1),
            g.next_payment_date
        ) AS next_payment_date
    FROM user_groups ug
    JOIN groups g ON ug.group_id = g.group_id
    JOIN users  u ON ug.user_id = u.user_id
    WHERE ug.is_phantom = FALSE
"""

# Individual / duo subscriptions live in their own table (no group). Mirror the
# group COALESCE shape: latest individual payment's next_payment_date wins,
# falling back to the client's own next_payment_date.
_IND_SELECT = """
    SELECT
        ic.id AS client_id,
        ic.user_id,
        u.username,
        u.first_name,
        ic.plan,
        ic.region,
        ic.price,
        COALESCE(
            (SELECT next_payment_date FROM individual_payments
             WHERE individual_client_id = ic.id
             ORDER BY payment_id DESC LIMIT 1),
            ic.next_payment_date
        ) AS next_payment_date
    FROM individual_clients ic
    JOIN users u ON ic.user_id = u.user_id
    WHERE ic.is_active = TRUE
"""

_IND_OVERDUE_TAIL = """ AND COALESCE(
        (SELECT next_payment_date FROM individual_payments
         WHERE individual_client_id = ic.id
         ORDER BY payment_id DESC LIMIT 1),
        ic.next_payment_date
      ) < CURRENT_DATE
      ORDER BY next_payment_date ASC"""

_GRP_OVERDUE_TAIL = """ AND COALESCE(
        (SELECT next_payment_date FROM payments
         WHERE user_id = ug.user_id AND group_id = g.group_id
         ORDER BY payment_id DESC LIMIT 1),
        g.next_payment_date
      ) < CURRENT_DATE
      ORDER BY next_payment_date ASC"""


async def get_customer_status(user_id: int) -> Optional[dict]:
    """Full grounding context for one customer.

    A customer may hold several group slots and/or an individual/duo plan; all
    of them are merged into a single `entries` list (sorted most-urgent first)
    plus a derived `segment` describing their payment stage.
    """
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        grp = await conn.fetch(
            _BASE_SELECT + " AND ug.user_id = $1 ORDER BY next_payment_date", user_id
        )
        ind = await conn.fetch(
            _IND_SELECT + " AND ic.user_id = $1 ORDER BY next_payment_date", user_id
        )
    if not grp and not ind:
        return None
    today = date.today()
    entries = [_group_entry(r, today) for r in grp]
    entries += [_individual_entry(r, today) for r in ind]
    # Most urgent first: overdue (most negative days) before far-future / unknown.
    entries.sort(key=lambda e: (e["days_until_due"] is None, e["days_until_due"] or 0))
    first = grp[0] if grp else ind[0]
    return {
        "user_id": user_id,
        "first_name": first["first_name"],
        "username": first["username"],
        "entries": entries,
        "any_overdue": any(e["is_overdue"] for e in entries),
        "segment": _segment(entries),
    }


async def get_overdue_customers(min_days_overdue: int = 1) -> list[dict]:
    """One entry per overdue subscription (group slot or individual plan),
    most overdue first. Outreach dedupes by user_id afterwards."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        grp = await conn.fetch(_BASE_SELECT + _GRP_OVERDUE_TAIL)
        ind = await conn.fetch(_IND_SELECT + _IND_OVERDUE_TAIL)
    today = date.today()
    out: list[dict] = []
    for r in grp:
        e = _group_entry(r, today)
        if e["days_until_due"] is not None and -e["days_until_due"] >= min_days_overdue:
            out.append(e)
    for r in ind:
        e = _individual_entry(r, today)
        if e["days_until_due"] is not None and -e["days_until_due"] >= min_days_overdue:
            out.append(e)
    out.sort(key=lambda e: e["days_until_due"])  # most overdue (most negative) first
    return out
