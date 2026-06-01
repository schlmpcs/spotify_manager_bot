"""Proactive outreach: nudge overdue customers first, as the manager.

Telegram only lets a personal account message users it already has a chat
with, so a nudge that can't be delivered falls back to a draft the manager
sees (handled inside dispatch.send_or_approve)."""

import logging

from aiogram import Bot

from config import settings
from bot.db import local, payments
from bot.services import llm
from bot.services.dispatch import send_or_approve
from bot.utils import prompts

logger = logging.getLogger(__name__)


async def run_outreach(bot: Bot, *, force: bool = False) -> tuple[int, int, int]:
    """Returns (sent, drafted, failed)."""
    mode = settings.proactive_mode
    if mode == "off" and not force:
        return (0, 0, 0)
    auto = mode == "auto"

    # Send under the first connected manager identity.
    owner_conn = None
    for oid in settings.owner_ids:
        owner_conn = await local.get_owner_connection(oid)
        if owner_conn:
            break
    if not owner_conn:
        logger.info("outreach: no active business connection, skipping")
        return (0, 0, 0)

    owner_id, conn_id = owner_conn["owner_id"], owner_conn["id"]
    owner = await local.get_owner(owner_id)
    style = owner["style_prompt"] if owner else None

    overdue = await payments.get_overdue_customers(settings.proactive_overdue_days)
    seen: set[int] = set()
    sent = drafted = failed = 0

    for row in overdue:
        cid = row["user_id"]
        if cid in seen:
            continue
        seen.add(cid)
        if await local.contacted_recently(cid, settings.proactive_cooldown_days):
            continue

        status = await payments.get_customer_status(cid)
        if not status:
            continue

        text = await llm.chat([
            {"role": "system", "content": prompts.system_prompt(status, style)},
            {"role": "user", "content": prompts.outreach_instruction(status)},
        ])
        if not text:
            failed += 1
            continue

        ok = await send_or_approve(
            bot, owner_id, conn_id, cid, "outreach", text, auto=auto
        )
        await local.log_outreach(cid)
        if ok:
            sent += 1
        else:
            drafted += 1

    logger.info("outreach done: sent=%s drafted=%s failed=%s", sent, drafted, failed)
    return sent, drafted, failed
