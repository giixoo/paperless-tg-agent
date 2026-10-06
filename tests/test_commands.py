from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from paperbot.config import Settings
from paperbot.paperless import Document
from paperbot.telegram import commands
from paperbot.telegram.deps import DEPS_KEY, Deps
from paperbot.telegram.keyboards import SearchStateStore


@dataclass
class FakePaperless:
    search_results: tuple[list[Document], int] = field(default_factory=lambda: ([], 0))
    recent_results: list[Document] = field(default_factory=list)
    detail: Document | None = None

    async def search_documents(self, query: str, **kwargs: Any) -> tuple[list[Document], int]:
        return self.search_results

    async def recent_documents(self, limit: int = 10) -> list[Document]:
        return self.recent_results

    async def get_document(self, doc_id: int) -> Document | None:
        return self.detail


def make_doc(**overrides: Any) -> Document:
    defaults: dict[str, Any] = dict(
        id=412,
        title="Car insurance 2026",
        created=date(2026, 1, 14),
        correspondent="PZU",
        document_type="Insurance policy",
        tags=["Insurance"],
        custom_fields={"Expires": "14.12.2026"},
    )
    defaults.update(overrides)
    return Document(**defaults)


def make_update(args_text: str | None = None) -> MagicMock:
    update = MagicMock()
    update.effective_user.language_code = "en"
    update.message.reply_text = AsyncMock()
    return update


def make_context(
    settings: Settings, paperless: FakePaperless, args: list[str] | None = None
) -> MagicMock:
    context = MagicMock()
    context.args = args or []
    deps = Deps(
        settings=settings,
        paperless=paperless,  # type: ignore[arg-type]
        search_store=SearchStateStore(),
        anthropic_client=MagicMock(),
        budget_store=MagicMock(),
        agent_memory=MagicMock(),
    )
    context.application.bot_data = {DEPS_KEY: deps}
    return context


async def test_help_command_replies_with_help_text(settings: Settings) -> None:
    update = make_update()
    context = make_context(settings, FakePaperless())

    await commands.help_command(update, context)

    update.message.reply_text.assert_awaited_once()
    assert "search" in update.message.reply_text.call_args[0][0]


async def test_search_command_without_query_replies_usage(settings: Settings) -> None:
    update = make_update()
    context = make_context(settings, FakePaperless(), args=[])

    await commands.search_command(update, context)

    update.message.reply_text.assert_awaited_once_with("Usage: /search <text>")


async def test_search_command_no_results(settings: Settings) -> None:
    update = make_update()
    paperless = FakePaperless(search_results=([], 0))
    context = make_context(settings, paperless, args=["insurance"])

    await commands.search_command(update, context)

    update.message.reply_text.assert_awaited_once_with("No documents found.")


async def test_search_command_formats_results(settings: Settings) -> None:
    update = make_update()
    doc = make_doc()
    paperless = FakePaperless(search_results=([doc], 1))
    context = make_context(settings, paperless, args=["insurance"])

    await commands.search_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "#412" in text
    assert "Car insurance 2026" in text
    assert "14.01.2026" in text
    assert "PZU" in text


async def test_doc_command_without_id_replies_usage(settings: Settings) -> None:
    update = make_update()
    context = make_context(settings, FakePaperless(), args=[])

    await commands.doc_command(update, context)

    update.message.reply_text.assert_awaited_once_with("Usage: /doc <id>")


async def test_doc_command_not_found(settings: Settings) -> None:
    update = make_update()
    paperless = FakePaperless(detail=None)
    context = make_context(settings, paperless, args=["999"])

    await commands.doc_command(update, context)

    update.message.reply_text.assert_awaited_once_with("Document #999 not found.")


async def test_doc_command_renders_card(settings: Settings) -> None:
    update = make_update()
    doc = make_doc()
    paperless = FakePaperless(detail=doc)
    context = make_context(settings, paperless, args=["412"])

    await commands.doc_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "#412 Car insurance 2026" in text
    assert "Created: 14.01.2026" in text
    assert "Correspondent: PZU" in text
    assert "Expires: 14.12.2026" in text
    assert "Open in Paperless" in text


async def test_recent_command_defaults_to_ten(settings: Settings) -> None:
    update = make_update()
    paperless = FakePaperless(recent_results=[make_doc()])
    context = make_context(settings, paperless, args=[])

    await commands.recent_command(update, context)

    update.message.reply_text.assert_awaited_once()
    assert "#412" in update.message.reply_text.call_args[0][0]


async def test_recent_command_clamps_n(settings: Settings) -> None:
    update = make_update()
    paperless = FakePaperless(recent_results=[])
    context = make_context(settings, paperless, args=["9999"])

    await commands.recent_command(update, context)

    update.message.reply_text.assert_awaited_once_with("No documents found.")
