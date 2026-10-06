"""Daily expiry reminder job (SPEC §4.6). No LLM involved."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from telegram.ext import Application, ContextTypes, JobQueue

from paperbot.config import Settings
from paperbot.paperless import Document, PaperlessError
from paperbot.telegram.commands import format_relative_days, parse_ddmmyyyy
from paperbot.telegram.deps import get_deps

logger = logging.getLogger(__name__)

RECENTLY_EXPIRED_DAYS = 7
EXPIRY_QUERY_LIMIT = 50
REMINDER_LANG = "en"  # no per-user language stored; reminders are a background job


def _format_line(doc: Document, field_name: str, today: date) -> str:
    raw = doc.custom_fields.get(field_name)
    parsed = parse_ddmmyyyy(raw) if raw else None
    marker = "⚠️ " if parsed and parsed < today else ""
    rel = f" — {format_relative_days(parsed, today, REMINDER_LANG)}" if parsed else ""
    return f"{marker}#{doc.id} {doc.title}{rel}"


async def send_daily_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = get_deps(context)
    settings = deps.settings
    today = datetime.now(ZoneInfo(settings.tz)).date()

    upcoming_query = json.dumps(
        [
            settings.expiry_field_name,
            "range",
            [today.isoformat(), (today + timedelta(days=settings.reminder_days)).isoformat()],
        ]
    )
    expired_query = json.dumps(
        [
            settings.expiry_field_name,
            "range",
            [
                (today - timedelta(days=RECENTLY_EXPIRED_DAYS)).isoformat(),
                (today - timedelta(days=1)).isoformat(),
            ],
        ]
    )

    try:
        upcoming = await deps.paperless.find_by_custom_field_query(
            upcoming_query, limit=EXPIRY_QUERY_LIMIT
        )
        expired = await deps.paperless.find_by_custom_field_query(
            expired_query, limit=EXPIRY_QUERY_LIMIT
        )
    except PaperlessError:
        logger.exception("reminder job: failed to fetch expiring documents")
        return

    if not upcoming and not expired:
        return

    def sort_key(doc: Document) -> date:
        raw = doc.custom_fields.get(settings.expiry_field_name)
        parsed = parse_ddmmyyyy(raw) if raw else None
        return parsed or date.max

    all_docs = sorted({d.id: d for d in (*expired, *upcoming)}.values(), key=sort_key)
    lines = [_format_line(doc, settings.expiry_field_name, today) for doc in all_docs]
    text = "\n\n".join(("📅 Document expiry reminder", "\n".join(lines)))

    for user_id in settings.telegram_allowed_users:
        try:
            await context.bot.send_message(user_id, text)
        except Exception:
            logger.exception("failed to send expiry reminder to user %s", user_id)


def register_reminder_job(
    application: Application[Any, Any, Any, Any, Any, Any], settings: Settings
) -> None:
    if not settings.reminder_enabled:
        logger.info("Daily expiry reminder disabled (REMINDER_ENABLED=false)")
        return
    job_queue: JobQueue[Any] | None = application.job_queue
    if job_queue is None:
        logger.warning("No JobQueue available; daily expiry reminder not scheduled")
        return
    hour, minute = (int(p) for p in settings.reminder_time.split(":", 1))
    job_queue.run_daily(
        send_daily_reminders,
        time=time(hour=hour, minute=minute, tzinfo=ZoneInfo(settings.tz)),
        name="daily_expiry_reminder",
    )
    logger.info("Scheduled daily expiry reminder at %s %s", settings.reminder_time, settings.tz)
