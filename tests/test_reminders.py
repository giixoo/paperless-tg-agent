from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

from paperbot.config import Settings
from paperbot.paperless import Document, PaperlessError
from paperbot.telegram import reminders
from paperbot.telegram.deps import DEPS_KEY, Deps
from paperbot.telegram.keyboards import SearchStateStore


def make_doc(**overrides: Any) -> Document:
    defaults: dict[str, Any] = dict(
        id=412,
        title="Car insurance 2026",
        created=None,
        correspondent="PZU",
        document_type="Insurance policy",
        tags=["Insurance"],
        custom_fields={},
    )
    defaults.update(overrides)
    return Document(**defaults)


@dataclass
class FakePaperless:
    upcoming: list[Document] = field(default_factory=list)
    expired: list[Document] = field(default_factory=list)
    raise_error: bool = False
    calls: list[str] = field(default_factory=list)

    async def find_by_custom_field_query(self, query_json: str, limit: int) -> list[Document]:
        self.calls.append(query_json)
        if self.raise_error:
            raise PaperlessError("boom")
        # distinguish by which range was asked for: a quick heuristic based
        # on call order (upcoming queried first in send_daily_reminders)
        return self.upcoming if len(self.calls) == 1 else self.expired


def make_context(settings: Settings, paperless: FakePaperless) -> MagicMock:
    context = MagicMock()
    deps = Deps(
        settings=settings,
        paperless=paperless,  # type: ignore[arg-type]
        search_store=SearchStateStore(),
        anthropic_client=MagicMock(),
        budget_store=MagicMock(),
        agent_memory=MagicMock(),
        pending_input=MagicMock(),
    )
    context.application.bot_data = {DEPS_KEY: deps}
    context.bot.send_message = AsyncMock()
    return context


async def test_no_message_sent_when_nothing_expiring(settings: Settings) -> None:
    paperless = FakePaperless(upcoming=[], expired=[])
    context = make_context(settings, paperless)

    await reminders.send_daily_reminders(context)

    context.bot.send_message.assert_not_called()


async def test_sends_one_message_per_allowed_user(settings: Settings) -> None:
    today = datetime.now(ZoneInfo(settings.tz)).date()
    doc = make_doc(custom_fields={"Expires": (today + timedelta(days=5)).strftime("%d.%m.%Y")})
    paperless = FakePaperless(upcoming=[doc], expired=[])
    context = make_context(settings, paperless)

    await reminders.send_daily_reminders(context)

    assert context.bot.send_message.await_count == len(settings.telegram_allowed_users)
    text = context.bot.send_message.call_args[0][1]
    assert "#412" in text
    assert "in 5 days" in text


async def test_expired_docs_get_warning_marker(settings: Settings) -> None:
    today = datetime.now(ZoneInfo(settings.tz)).date()
    expired_doc = make_doc(
        id=100, custom_fields={"Expires": (today - timedelta(days=3)).strftime("%d.%m.%Y")}
    )
    paperless = FakePaperless(upcoming=[], expired=[expired_doc])
    context = make_context(settings, paperless)

    await reminders.send_daily_reminders(context)

    text = context.bot.send_message.call_args[0][1]
    assert "⚠️ #100" in text
    assert "expired 3 days ago" in text


async def test_paperless_error_suppresses_send(settings: Settings) -> None:
    paperless = FakePaperless(raise_error=True)
    context = make_context(settings, paperless)

    await reminders.send_daily_reminders(context)

    context.bot.send_message.assert_not_called()


async def test_send_failure_for_one_user_does_not_block_others(settings: Settings) -> None:
    today = datetime.now(ZoneInfo(settings.tz)).date()
    doc = make_doc(custom_fields={"Expires": (today + timedelta(days=5)).strftime("%d.%m.%Y")})
    paperless = FakePaperless(upcoming=[doc], expired=[])
    context = make_context(settings, paperless)
    context.bot.send_message = AsyncMock(side_effect=[Exception("blocked"), None])
    context.application.bot_data[DEPS_KEY].settings.telegram_allowed_users = [1, 2]

    await reminders.send_daily_reminders(context)

    assert context.bot.send_message.await_count == 2
