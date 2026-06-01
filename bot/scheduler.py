"""Daily background job that runs proactive outreach."""

import logging

import pytz
from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import settings
from bot.services.outreach import run_outreach

logger = logging.getLogger(__name__)


def start_scheduler(bot: Bot) -> AsyncIOScheduler:
    tz = pytz.timezone(settings.bot_timezone)
    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(
        run_outreach,
        trigger="cron",
        hour=settings.proactive_hour,
        minute=0,
        args=[bot],
        id="daily_outreach",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    logger.info(
        "Scheduler started: outreach daily at %02d:00 %s (mode=%s)",
        settings.proactive_hour, settings.bot_timezone, settings.proactive_mode,
    )
    return scheduler
