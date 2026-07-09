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


def _pending_entry(row) -> dict:
    """A submitted-but-not-yet-connected purchase request (заявка).

    A customer who just paid/applied sits here until an admin approves them in
    the main bot; only then do they appear in `groups` / `individual_clients`.
    So this is the only place that knows a connection is in progress.
    """
    ctype = row["client_type"]
    label = _PLAN_LABEL.get(ctype) if ctype else "семейная подписка (группа)"
    return {
        "request_id": row["request_id"],
        "region": (row["region"] or "").upper(),
        "client_type": ctype,
        "label": label or "подписка",
        "created_at": _as_date(row["created_at"]),
    }


def _identity(row) -> dict:
    """Minimal user identity returned by admin/client searches."""
    return {
        "user_id": row["user_id"],
        "first_name": row["first_name"],
        "username": row["username"],
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

# Pending connection requests (заявки) for a customer — submitted but not yet
# approved in the main bot. Lets us tell a customer who asks "когда подключат?"
# that their request is in and being processed, instead of treating them as new.
_PENDING_SELECT = """
    SELECT
        pr.request_id,
        pr.region,
        pr.client_type,
        pr.created_at,
        u.username,
        u.first_name
    FROM purchase_requests pr
    LEFT JOIN users u ON u.user_id = pr.user_id
    WHERE pr.user_id = $1 AND pr.status = 'pending'
    ORDER BY pr.created_at DESC
"""

_PENDING_LIST_SELECT = """
    SELECT
        pr.request_id,
        pr.user_id,
        pr.region,
        pr.client_type,
        pr.created_at,
        u.username,
        u.first_name
    FROM purchase_requests pr
    LEFT JOIN users u ON u.user_id = pr.user_id
    WHERE pr.status = 'pending'
    ORDER BY pr.created_at DESC
    LIMIT $1
"""


_GRP_OVERDUE_TAIL = """ AND COALESCE(
        (SELECT next_payment_date FROM payments
         WHERE user_id = ug.user_id AND group_id = g.group_id
         ORDER BY payment_id DESC LIMIT 1),
        g.next_payment_date
      ) < CURRENT_DATE
      ORDER BY next_payment_date ASC"""

_GRP_DUE_TAIL = """ AND COALESCE(
        (SELECT next_payment_date FROM payments
         WHERE user_id = ug.user_id AND group_id = g.group_id
         ORDER BY payment_id DESC LIMIT 1),
        g.next_payment_date
      ) >= CURRENT_DATE
      AND COALESCE(
        (SELECT next_payment_date FROM payments
         WHERE user_id = ug.user_id AND group_id = g.group_id
         ORDER BY payment_id DESC LIMIT 1),
        g.next_payment_date
      ) <= CURRENT_DATE + ($1::int)
      ORDER BY next_payment_date ASC
      LIMIT $2"""

_IND_DUE_TAIL = """ AND COALESCE(
        (SELECT next_payment_date FROM individual_payments
         WHERE individual_client_id = ic.id
         ORDER BY payment_id DESC LIMIT 1),
        ic.next_payment_date
      ) >= CURRENT_DATE
      AND COALESCE(
        (SELECT next_payment_date FROM individual_payments
         WHERE individual_client_id = ic.id
         ORDER BY payment_id DESC LIMIT 1),
        ic.next_payment_date
      ) <= CURRENT_DATE + ($1::int)
      ORDER BY next_payment_date ASC
      LIMIT $2"""

_GROUP_HEADER_SELECT = """
    SELECT
        g.group_id,
        g.group_name,
        g.display_id,
        g.payment_day_of_month,
        g.next_payment_date,
        COALESCE(SUM(ug.slots) FILTER (WHERE ug.is_phantom = FALSE), 0)::int
            AS occupied_slots,
        COALESCE(SUM(ug.slots) FILTER (WHERE ug.is_phantom = TRUE), 0)::int
            AS phantom_slots,
        COUNT(*) FILTER (WHERE ug.is_phantom = FALSE) AS real_member_count,
        COUNT(*) FILTER (WHERE ug.is_phantom = TRUE) AS phantom_member_count
    FROM groups g
    LEFT JOIN user_groups ug ON ug.group_id = g.group_id
    WHERE g.display_id = $1
    GROUP BY
        g.group_id,
        g.group_name,
        g.display_id,
        g.payment_day_of_month,
        g.next_payment_date
"""

_GROUP_MEMBERS_SELECT = """
    SELECT
        u.user_id,
        u.username,
        u.first_name,
        u.display_id AS user_display_id,
        ug.slots,
        ug.is_phantom,
        COALESCE(
            (SELECT next_payment_date FROM payments
             WHERE user_id = ug.user_id AND group_id = ug.group_id
             ORDER BY payment_id DESC LIMIT 1),
            g.next_payment_date
        ) AS next_payment_date,
        (SELECT payment_date FROM payments
         WHERE user_id = ug.user_id AND group_id = ug.group_id
         ORDER BY payment_id DESC LIMIT 1) AS last_payment_date,
        (SELECT COUNT(*) FROM payments
         WHERE user_id = ug.user_id AND group_id = ug.group_id) AS total_payments
    FROM groups g
    JOIN user_groups ug ON ug.group_id = g.group_id
    JOIN users u ON u.user_id = ug.user_id
    WHERE g.group_id = $1
    ORDER BY ug.is_phantom, u.display_id, u.user_id
"""

_AVAILABLE_GROUPS_SELECT = """
    SELECT
        g.group_id,
        g.group_name,
        g.display_id,
        g.next_payment_date,
        COALESCE(SUM(ug.slots) FILTER (WHERE ug.is_phantom = FALSE), 0)::int
            AS occupied_slots,
        COALESCE(SUM(ug.slots) FILTER (WHERE ug.is_phantom = TRUE), 0)::int
            AS phantom_slots
    FROM groups g
    LEFT JOIN user_groups ug ON ug.group_id = g.group_id
    WHERE ($1::text IS NULL OR g.display_id LIKE $1)
    GROUP BY g.group_id, g.group_name, g.display_id, g.next_payment_date
    HAVING COALESCE(SUM(ug.slots) FILTER (WHERE ug.is_phantom = FALSE), 0) < 6
    ORDER BY g.display_id
    LIMIT $2
"""


async def get_user_identity(user_id: int) -> Optional[dict]:
    """Return a user's basic identity from the payment DB, if present."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id, username, first_name FROM users WHERE user_id = $1",
            user_id,
        )
    return _identity(row) if row else None


async def get_user_identity_by_username(username: str) -> Optional[dict]:
    """Return a user's basic identity by exact Telegram username."""
    if not _pool:
        return None
    clean = username.strip().lstrip("@")
    if not clean:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT user_id, username, first_name
               FROM users
               WHERE lower(coalesce(username, '')) = lower($1)
               ORDER BY user_id
               LIMIT 1""",
            clean,
        )
    return _identity(row) if row else None


async def find_customers(query: str, limit: int = 8) -> list[dict]:
    """Search users by id, username, or first name for owner-side lookup."""
    if not _pool:
        return []
    term = (query or "").strip().lstrip("@")
    if not term:
        return []
    limit = max(1, min(limit, 25))
    async with _pool.acquire() as conn:
        if term.isdigit():
            user_id = int(term)
            rows = await conn.fetch(
                """SELECT user_id, username, first_name,
                          CASE WHEN user_id = $1 THEN 0 ELSE 1 END AS rank
                   FROM users
                   WHERE user_id = $1
                      OR lower(coalesce(username, '')) = lower($2)
                   ORDER BY rank, user_id
                   LIMIT $3""",
                user_id, term, limit,
            )
        else:
            pattern = f"%{term.lower()}%"
            rows = await conn.fetch(
                """SELECT user_id, username, first_name,
                          CASE
                            WHEN lower(coalesce(username, '')) = lower($1) THEN 0
                            WHEN lower(coalesce(username, '')) LIKE $2 THEN 1
                            WHEN lower(coalesce(first_name, '')) LIKE $2 THEN 2
                            ELSE 3
                          END AS rank
                   FROM users
                   WHERE lower(coalesce(username, '')) = lower($1)
                      OR lower(coalesce(username, '')) LIKE $2
                      OR lower(coalesce(first_name, '')) LIKE $2
                   ORDER BY rank, user_id
                   LIMIT $3""",
                term, pattern, limit,
            )
    return [_identity(r) for r in rows]


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
        pending = await conn.fetch(_PENDING_SELECT, user_id)
    if not grp and not ind and not pending:
        return None
    today = date.today()
    entries = [_group_entry(r, today) for r in grp]
    entries += [_individual_entry(r, today) for r in ind]
    # Most urgent first: overdue (most negative days) before far-future / unknown.
    entries.sort(key=lambda e: (e["days_until_due"] is None, e["days_until_due"] or 0))
    pending_requests = [_pending_entry(r) for r in pending]
    # A pending-request-only customer isn't in groups/individual_clients yet, so
    # fall back to their `users` row (carried on the request) for the name.
    first = grp[0] if grp else (ind[0] if ind else pending[0])
    return {
        "user_id": user_id,
        "first_name": first["first_name"],
        "username": first["username"],
        "entries": entries,
        "pending_requests": pending_requests,
        "any_overdue": any(e["is_overdue"] for e in entries),
        "segment": _segment(entries),
    }


async def get_customer_status_by_username(username: str) -> Optional[dict]:
    """Full grounding context for one customer, looked up by @username."""
    ident = await get_user_identity_by_username(username)
    if not ident:
        return None
    return await get_customer_status(ident["user_id"])


def _group_member(row, today: date) -> dict:
    npd = _as_date(row["next_payment_date"])
    days = (npd - today).days if npd else None
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "first_name": row["first_name"],
        "user_display_id": row["user_display_id"],
        "slots": row["slots"] or 1,
        "is_phantom": bool(row["is_phantom"]),
        "next_payment_date": npd,
        "days_until_due": days,
        "is_overdue": days is not None and days < 0,
        "last_payment_date": _as_date(row["last_payment_date"]),
        "total_payments": row["total_payments"] or 0,
    }


async def get_group_info(display_id: str) -> Optional[dict]:
    """Detailed read-only status for one family group by display_id."""
    if not _pool:
        return None
    clean_id = str(display_id).strip()
    if not clean_id:
        return None
    async with _pool.acquire() as conn:
        group = await conn.fetchrow(_GROUP_HEADER_SELECT, clean_id)
        if not group:
            return None
        rows = await conn.fetch(_GROUP_MEMBERS_SELECT, group["group_id"])

    today = date.today()
    region = _region(group["display_id"])
    members = [_group_member(r, today) for r in rows]
    real_members = [m for m in members if not m["is_phantom"]]
    return {
        "group_id": group["group_id"],
        "group_name": group["group_name"],
        "display_id": group["display_id"],
        "payment_day_of_month": group["payment_day_of_month"],
        "next_payment_date": _as_date(group["next_payment_date"]),
        "region": region,
        "currency": _currency(region),
        "amount": _price(region),
        "occupied_slots": group["occupied_slots"] or 0,
        "phantom_slots": group["phantom_slots"] or 0,
        "real_member_count": group["real_member_count"] or len(real_members),
        "phantom_member_count": group["phantom_member_count"] or 0,
        "free_slots": max(0, 6 - (group["occupied_slots"] or 0)),
        "members": real_members,
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


async def get_due_customers(max_days: int = 7, limit: int = 20) -> list[dict]:
    """Subscriptions due from today through `max_days` days from now."""
    if not _pool:
        return []
    max_days = max(0, min(max_days, 365))
    limit = max(1, min(limit, 100))
    async with _pool.acquire() as conn:
        grp = await conn.fetch(_BASE_SELECT + _GRP_DUE_TAIL, max_days, limit)
        ind = await conn.fetch(_IND_SELECT + _IND_DUE_TAIL, max_days, limit)
    today = date.today()
    out = [_group_entry(r, today) for r in grp]
    out += [_individual_entry(r, today) for r in ind]
    out.sort(key=lambda e: (e["days_until_due"] is None, e["days_until_due"] or 0))
    return out[:limit]


async def get_pending_requests(limit: int = 20) -> list[dict]:
    """Recent pending purchase requests for the owner assistant."""
    if not _pool:
        return []
    limit = max(1, min(limit, 100))
    async with _pool.acquire() as conn:
        rows = await conn.fetch(_PENDING_LIST_SELECT, limit)
    out: list[dict] = []
    for r in rows:
        pending = _pending_entry(r)
        pending["user_id"] = r["user_id"]
        pending["first_name"] = r["first_name"]
        pending["username"] = r["username"]
        out.append(pending)
    return out


async def get_groups_with_available_slots(
    region: str | None = None, limit: int = 30
) -> list[dict]:
    """Family groups with fewer than 6 occupied real slots."""
    if not _pool:
        return []
    limit = max(1, min(limit, 100))
    pattern = None
    if region:
        pattern = "1%" if region.upper() == "RU" else "0%"
    async with _pool.acquire() as conn:
        rows = await conn.fetch(_AVAILABLE_GROUPS_SELECT, pattern, limit)
    out: list[dict] = []
    for r in rows:
        group_region = _region(r["display_id"])
        occupied = r["occupied_slots"] or 0
        out.append({
            "group_id": r["group_id"],
            "group_name": r["group_name"],
            "display_id": r["display_id"],
            "next_payment_date": _as_date(r["next_payment_date"]),
            "region": group_region,
            "currency": _currency(group_region),
            "amount": _price(group_region),
            "occupied_slots": occupied,
            "phantom_slots": r["phantom_slots"] or 0,
            "free_slots": max(0, 6 - occupied),
        })
    return out


async def get_client_stats() -> dict:
    """High-level read-only counts for owner-side questions."""
    if not _pool:
        return {}
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            WITH group_slots AS (
                SELECT
                    ug.user_id,
                    COALESCE(
                        (SELECT next_payment_date FROM payments
                         WHERE user_id = ug.user_id AND group_id = g.group_id
                         ORDER BY payment_id DESC LIMIT 1),
                        g.next_payment_date
                    ) AS next_payment_date
                FROM user_groups ug
                JOIN groups g ON ug.group_id = g.group_id
                WHERE ug.is_phantom = FALSE
            ),
            individual AS (
                SELECT
                    ic.user_id,
                    ic.plan,
                    COALESCE(
                        (SELECT next_payment_date FROM individual_payments
                         WHERE individual_client_id = ic.id
                         ORDER BY payment_id DESC LIMIT 1),
                        ic.next_payment_date
                    ) AS next_payment_date
                FROM individual_clients ic
                WHERE ic.is_active = TRUE
            ),
            active_users AS (
                SELECT user_id FROM group_slots
                UNION
                SELECT user_id FROM individual
            ),
            overdue_users AS (
                SELECT user_id FROM group_slots WHERE next_payment_date < CURRENT_DATE
                UNION
                SELECT user_id FROM individual WHERE next_payment_date < CURRENT_DATE
            ),
            pending AS (
                SELECT user_id FROM purchase_requests WHERE status = 'pending'
            )
            SELECT
                (SELECT COUNT(*) FROM group_slots) AS group_slots,
                (SELECT COUNT(DISTINCT user_id) FROM group_slots) AS group_users,
                (SELECT COUNT(*) FROM individual WHERE plan = 'individual')
                    AS individual_clients,
                (SELECT COUNT(*) FROM individual WHERE plan = 'duo') AS duo_clients,
                (SELECT COUNT(DISTINCT user_id) FROM individual) AS individual_users,
                (SELECT COUNT(DISTINCT user_id) FROM active_users) AS active_users,
                (SELECT COUNT(DISTINCT user_id) FROM overdue_users) AS overdue_users,
                (SELECT COUNT(*) FROM pending) AS pending_requests
            """
        )
    return dict(row) if row else {}
