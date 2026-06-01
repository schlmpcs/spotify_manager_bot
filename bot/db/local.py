"""This bot's own state: business connections, message history, owner
settings, and pending approval drafts. Stored in a small SQLite file —
the payment data lives in Postgres (see payments.py)."""

import time
from typing import Optional

import aiosqlite

_db: Optional[aiosqlite.Connection] = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    id          TEXT PRIMARY KEY,         -- business_connection_id
    owner_id    INTEGER NOT NULL,
    is_enabled  INTEGER NOT NULL DEFAULT 1,
    can_reply   INTEGER NOT NULL DEFAULT 1,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,         -- the customer's chat (= user id)
    role        TEXT NOT NULL,            -- 'user' | 'assistant'
    text        TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);

CREATE TABLE IF NOT EXISTS owner_settings (
    owner_id        INTEGER PRIMARY KEY,
    style_prompt    TEXT,
    auto_reply      INTEGER,              -- NULL = use global default
    proactive_mode  TEXT                  -- NULL = use global default
);

CREATE TABLE IF NOT EXISTS drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    INTEGER NOT NULL,
    conn_id     TEXT NOT NULL,
    chat_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,            -- 'reply' | 'outreach'
    text        TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS outreach_log (
    customer_id INTEGER NOT NULL,
    sent_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outreach_customer ON outreach_log(customer_id, sent_at);
"""


async def init() -> None:
    global _db
    _db = await aiosqlite.connect(__path())
    _db.row_factory = aiosqlite.Row
    await _db.executescript(SCHEMA)
    await _db.commit()


def __path() -> str:
    from config import settings
    return settings.local_db_path


async def close() -> None:
    if _db:
        await _db.close()


def _now() -> int:
    return int(time.time())


# ── connections ────────────────────────────────────────────────────────────
async def upsert_connection(conn_id: str, owner_id: int, is_enabled: bool,
                            can_reply: bool) -> None:
    await _db.execute(
        """INSERT INTO connections (id, owner_id, is_enabled, can_reply, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             is_enabled=excluded.is_enabled,
             can_reply=excluded.can_reply,
             updated_at=excluded.updated_at""",
        (conn_id, owner_id, int(is_enabled), int(can_reply), _now()),
    )
    await _db.commit()


async def get_connection(conn_id: str) -> Optional[aiosqlite.Row]:
    cur = await _db.execute("SELECT * FROM connections WHERE id=?", (conn_id,))
    return await cur.fetchone()


async def get_owner_connection(owner_id: int) -> Optional[aiosqlite.Row]:
    """The active connection for an owner (used for proactive outreach)."""
    cur = await _db.execute(
        "SELECT * FROM connections WHERE owner_id=? AND is_enabled=1 AND can_reply=1 "
        "ORDER BY updated_at DESC LIMIT 1",
        (owner_id,),
    )
    return await cur.fetchone()


# ── message history ────────────────────────────────────────────────────────
async def add_message(chat_id: int, role: str, text: str) -> None:
    await _db.execute(
        "INSERT INTO messages (chat_id, role, text, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, role, text, _now()),
    )
    await _db.commit()


async def get_history(chat_id: int, limit: int = 10) -> list[dict]:
    cur = await _db.execute(
        "SELECT role, text FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
        (chat_id, limit),
    )
    rows = await cur.fetchall()
    return [{"role": r["role"], "content": r["text"]} for r in reversed(rows)]


async def count_recent_user_messages(
    chat_id: int, minute_cutoff: int, hour_cutoff: int
) -> tuple[int, int]:
    """(# customer messages in the last minute, # in the last hour) — abuse guard.

    `minute_cutoff` / `hour_cutoff` are unix-second thresholds (now-60, now-3600).
    """
    cur = await _db.execute(
        """SELECT
             COALESCE(SUM(CASE WHEN created_at > ? THEN 1 ELSE 0 END), 0) AS per_min,
             COUNT(*) AS per_hour
           FROM messages
           WHERE chat_id=? AND role='user' AND created_at > ?""",
        (minute_cutoff, chat_id, hour_cutoff),
    )
    row = await cur.fetchone()
    return int(row["per_min"]), int(row["per_hour"])


# ── owner settings ─────────────────────────────────────────────────────────
async def get_owner(owner_id: int) -> Optional[aiosqlite.Row]:
    cur = await _db.execute(
        "SELECT * FROM owner_settings WHERE owner_id=?", (owner_id,)
    )
    return await cur.fetchone()


async def set_style_prompt(owner_id: int, prompt: str) -> None:
    await _db.execute(
        """INSERT INTO owner_settings (owner_id, style_prompt) VALUES (?, ?)
           ON CONFLICT(owner_id) DO UPDATE SET style_prompt=excluded.style_prompt""",
        (owner_id, prompt),
    )
    await _db.commit()


async def seed_style_prompt(owner_id: int, prompt: str) -> None:
    """Set the owner's style ONLY if they haven't got one yet.

    Lets us ship a sensible default voice as the *active* per-owner style on
    first run, without clobbering a style the manager later set via /style.
    """
    await _db.execute(
        """INSERT INTO owner_settings (owner_id, style_prompt) VALUES (?, ?)
           ON CONFLICT(owner_id) DO UPDATE SET style_prompt=excluded.style_prompt
           WHERE owner_settings.style_prompt IS NULL""",
        (owner_id, prompt),
    )
    await _db.commit()


async def set_auto_reply(owner_id: int, value: bool) -> None:
    await _db.execute(
        """INSERT INTO owner_settings (owner_id, auto_reply) VALUES (?, ?)
           ON CONFLICT(owner_id) DO UPDATE SET auto_reply=excluded.auto_reply""",
        (owner_id, int(value)),
    )
    await _db.commit()


# ── drafts (approval flow) ─────────────────────────────────────────────────
async def create_draft(owner_id: int, conn_id: str, chat_id: int,
                       kind: str, text: str) -> int:
    cur = await _db.execute(
        """INSERT INTO drafts (owner_id, conn_id, chat_id, kind, text, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (owner_id, conn_id, chat_id, kind, text, _now()),
    )
    await _db.commit()
    return cur.lastrowid


async def get_draft(draft_id: int) -> Optional[aiosqlite.Row]:
    cur = await _db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,))
    return await cur.fetchone()


async def update_draft_text(draft_id: int, text: str) -> None:
    await _db.execute("UPDATE drafts SET text=? WHERE id=?", (text, draft_id))
    await _db.commit()


async def delete_draft(draft_id: int) -> None:
    await _db.execute("DELETE FROM drafts WHERE id=?", (draft_id,))
    await _db.commit()


# ── outreach cooldown ──────────────────────────────────────────────────────
async def contacted_recently(customer_id: int, within_days: int) -> bool:
    cutoff = _now() - within_days * 86400
    cur = await _db.execute(
        "SELECT 1 FROM outreach_log WHERE customer_id=? AND sent_at>? LIMIT 1",
        (customer_id, cutoff),
    )
    return await cur.fetchone() is not None


async def log_outreach(customer_id: int) -> None:
    await _db.execute(
        "INSERT INTO outreach_log (customer_id, sent_at) VALUES (?, ?)",
        (customer_id, _now()),
    )
    await _db.commit()
