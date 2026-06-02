"""Proactive outreach: a staged sequence of overdue-payment nudges, sent as
the manager.

Each overdue customer moves through one stage per daily run:
  • Day 1  — first nudge  (prompts.NUDGE_STAGE_1): reminder + "будете продлевать?"
  • Day 2  — second nudge (prompts.NUDGE_STAGE_2): "отключать вас от подписки?"
  • Day 3  — no more customer messages; the manager is notified that these
             clients still didn't pay after both nudges.

The nudge texts are fixed (no LLM) so the wording stays consistent. Stage is
tracked per overdue *cycle* (keyed on the due date): once a customer pays and
later lapses again, the new due date restarts the sequence from day 1.

This staged, one-step-per-day progression is for the automatic daily cron. A
manual /nudge (force=True) is unlimited: it ignores the day throttle and the
stage cap and re-sends a reminder to every overdue customer on demand.

Telegram only lets a personal account message users it already has a chat with,
so a nudge that can't be delivered falls back to a draft the manager sees
(handled inside dispatch.send_or_approve)."""

import html
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot

from config import settings
from bot.db import local, payments
from bot.services.dispatch import send_or_approve
from bot.utils import prompts

logger = logging.getLogger(__name__)


def _same_day(ts_a: int, ts_b: int, tz: ZoneInfo) -> bool:
    """True if two unix timestamps fall on the same calendar day in `tz`."""
    return (
        datetime.fromtimestamp(ts_a, tz).date()
        == datetime.fromtimestamp(ts_b, tz).date()
    )


def _final_report(items: list[dict]) -> str:
    """Manager-facing summary of clients who ignored both nudges."""
    lines = [
        "❗️ Эти клиенты не оплатили даже после второго напоминания "
        "(оба напоминания отправлены, оплаты нет):",
        "",
    ]
    for it in items:
        st, e = it["status"], it["entry"]
        name = html.escape(st.get("first_name") or "клиент")
        who = f"@{st['username']}" if st.get("username") else f"<code>{st['user_id']}</code>"
        days = e["days_until_due"]
        overdue = f"просрочка {-days} дн." if days is not None else "срок неизвестен"
        lines.append(
            f"• {name} ({who}) — {e['label']}, {e['amount']}{e['currency']}, {overdue}"
        )
    return "\n".join(lines)


async def run_outreach(bot: Bot, *, force: bool = False) -> dict:
    """Advance every overdue customer one stage.

    Returns {'sent', 'drafted', 'final', 'stage1', 'stage2'}.
    `force=True` (the /nudge command) runs even when PROACTIVE_MODE=off and has
    no limit: it ignores both the once-per-calendar-day guard and the stage cap,
    sending every overdue customer a reminder right now (stage 1 the first time
    in a cycle, the firmer stage 2 thereafter). The gentle staged progression
    (stop after two nudges, then notify the manager) applies only to the
    automatic daily cron (force=False).
    """
    result = {"sent": 0, "drafted": 0, "final": 0, "stage1": 0, "stage2": 0}

    mode = settings.proactive_mode
    if mode == "off" and not force:
        return result
    auto = mode == "auto"

    # Send under the first connected manager identity.
    owner_conn = None
    for oid in settings.owner_ids:
        owner_conn = await local.get_owner_connection(oid)
        if owner_conn:
            break
    if not owner_conn:
        logger.info("outreach: no active business connection, skipping")
        return result

    owner_id, conn_id = owner_conn["owner_id"], owner_conn["id"]
    tz = ZoneInfo(settings.bot_timezone)
    now = int(time.time())

    overdue = await payments.get_overdue_customers(settings.proactive_overdue_days)
    seen: set[int] = set()
    final_items: list[dict] = []

    for row in overdue:
        cid = row["user_id"]
        if cid in seen:
            continue
        seen.add(cid)

        status = await payments.get_customer_status(cid)
        if not status:
            continue
        # The most-urgent overdue entry defines this customer's cycle.
        overdue_entries = [e for e in status["entries"] if e["is_overdue"]]
        if not overdue_entries:
            continue
        primary = overdue_entries[0]
        npd = primary["next_payment_date"]
        cycle_due = npd.isoformat() if npd else "?"

        state = await local.get_outreach_state(cid)
        if state and state["cycle_due"] == cycle_due:
            stage = state["stage"]
            # One stage advance per calendar day for the daily cron — don't double
            # up if it fired already today. A manual /nudge (force=True) overrides
            # this: the manager explicitly asked to push the sequence forward now.
            if not force and _same_day(state["last_sent_at"], now, tz):
                continue
        else:
            stage = 0  # fresh (or first-ever) overdue cycle

        if force:
            # Manual /nudge has no limit: always send a reminder to every overdue
            # customer right now, regardless of stage or how recently we nudged.
            # First time this cycle → stage 1; after that the firmer stage-2 text,
            # re-sent on every /nudge until the customer pays (which resets the
            # cycle). The gentle "stop after 2 + notify manager" staging below is
            # only for the automatic daily cron.
            next_stage = 1 if stage == 0 else 2
            text = prompts.nudge_text(next_stage, primary)
            ok = await send_or_approve(
                bot, owner_id, conn_id, cid, "outreach", text, auto=auto,
                username=status.get("username"),
            )
            await local.set_outreach_state(cid, next_stage, cycle_due)
            await local.log_outreach(cid)
            result["sent" if ok else "drafted"] += 1
            result[f"stage{next_stage}"] += 1
            continue

        if stage >= 3:
            continue  # both nudges sent and manager already notified this cycle

        if stage == 2:
            # Day 3: stop messaging the customer, escalate to the manager.
            final_items.append({"status": status, "entry": primary})
            await local.set_outreach_state(cid, 3, cycle_due)
            continue

        next_stage = stage + 1            # 1 or 2
        # Grounded in this customer's most-urgent overdue plan: amount + region
        # requisites are quoted in the nudge.
        text = prompts.nudge_text(next_stage, primary)
        ok = await send_or_approve(
            bot, owner_id, conn_id, cid, "outreach", text, auto=auto,
            username=status.get("username"),
        )
        await local.set_outreach_state(cid, next_stage, cycle_due)
        await local.log_outreach(cid)
        result["sent" if ok else "drafted"] += 1
        result[f"stage{next_stage}"] += 1

    if final_items:
        try:
            await bot.send_message(owner_id, _final_report(final_items))
            result["final"] = len(final_items)
        except Exception:  # noqa: BLE001
            logger.exception("could not send final outreach report to owner %s", owner_id)

    logger.info(
        "outreach done: stage1=%s stage2=%s sent=%s drafted=%s final=%s",
        result["stage1"], result["stage2"], result["sent"],
        result["drafted"], result["final"],
    )
    return result
