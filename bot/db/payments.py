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
    """Open the read-only pool to the payment DB.

    Non-fatal: if the DB is unreachable we log and leave `_pool` as None so the
    bot still starts, polls, and accepts the Business/Chat-Automation
    connection. Grounding queries return None/[] until the DB comes back —
    call connect() again to retry.
    """
    global _pool
    try:
        _pool = await asyncpg.create_pool(
            dsn=settings.database_dsn, min_size=1, max_size=4
        )
        logger.info("Connected to payment database")
    except Exception as e:  # noqa: BLE001 — degrade gracefully, don't crash the bot
        _pool = None
        logger.error(
            "Could not connect to payment DB (%s) — starting WITHOUT grounding; "
            "replies will lack payment data until the DB is reachable", e
        )


async def close() -> None:
    if _pool:
        await _pool.close()


def _region(group_display_id: str) -> str:
    return "RU" if str(group_display_id).startswith("1") else "KZ"


def _price(region: str) -> int:
    return settings.ru_group_price if region == "RU" else settings.kz_group_price


def _as_date(value) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    return value


def _row_to_status(row, today: date) -> dict:
    region = _region(row["group_display_id"])
    npd = _as_date(row["next_payment_date"])
    days = (npd - today).days if npd else None
    return {
        "user_id": row["user_id"],
        "first_name": row["first_name"],
        "username": row["username"],
        "group_name": row["group_name"],
        "group_display_id": row["group_display_id"],
        "region": region,
        "currency": "₽" if region == "RU" else "₸",
        "amount": _price(region),
        "next_payment_date": npd,
        "days_until_due": days,          # negative == overdue
        "is_overdue": days is not None and days < 0,
    }


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


async def get_customer_status(user_id: int) -> Optional[dict]:
    """Full grounding context for one customer (may belong to several groups)."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            _BASE_SELECT + " AND ug.user_id = $1 ORDER BY next_payment_date",
            user_id,
        )
    if not rows:
        return None
    today = date.today()
    groups = [_row_to_status(r, today) for r in rows]
    return {
        "user_id": user_id,
        "first_name": rows[0]["first_name"],
        "username": rows[0]["username"],
        "groups": groups,
        "any_overdue": any(g["is_overdue"] for g in groups),
    }


async def get_overdue_customers(min_days_overdue: int = 1) -> list[dict]:
    """One row per overdue (user, group), most overdue first."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            _BASE_SELECT
            + """ AND COALESCE(
                    (SELECT next_payment_date FROM payments
                     WHERE user_id = ug.user_id AND group_id = g.group_id
                     ORDER BY payment_id DESC LIMIT 1),
                    g.next_payment_date
                  ) < CURRENT_DATE
                  ORDER BY next_payment_date ASC"""
        )
    today = date.today()
    out = []
    for r in rows:
        s = _row_to_status(r, today)
        if s["days_until_due"] is not None and -s["days_until_due"] >= min_days_overdue:
            out.append(s)
    return out
