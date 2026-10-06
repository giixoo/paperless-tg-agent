from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from paperbot.config import Settings
from paperbot.paperless import Document
from paperbot.telegram import commands
from paperbot.telegram.deps import DEPS_KEY, Deps
from paperbot.telegram.keyboards import SearchStateStore
from paperbot.telegram.state import PendingInputStore


@dataclass
class FakePaperless:
    search_results: tuple[list[Document], int] = field(default_factory=lambda: ([], 0))
    recent_results: list[Document] = field(default_factory=list)
    detail: Document | None = None
    custom_field_results: list[Document] = field(default_factory=list)

    async def search_documents(self, query: str, **kwargs: Any) -> tuple[list[Document], int]:
        return self.search_results

    async def recent_documents(self, limit: int = 10) -> list[Document]:
        return self.recent_results

    async def get_document(self, doc_id: int) -> Document | None:
        return self.detail

    async def find_by_custom_field_query(self, query_json: str, limit: int) -> list[Document]:
        return self.custom_field_results


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


def make_update(chat_id: int = 999) -> MagicMock:
    update = MagicMock()
    update.effective_user.language_code = "en"
    update.effective_chat.id = chat_id
    update.message.chat_id = chat_id
    update.message.reply_text = AsyncMock()
    return update


def make_callback_update(data: str, chat_id: int = 999) -> MagicMock:
    update = MagicMock()
    update.effective_user.language_code = "en"
    update.effective_chat.id = chat_id
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.edit_message_reply_markup = AsyncMock()
    return update


def make_context(
    settings: Settings, paperless: FakePaperless, args: list[str] | None = None
) -> MagicMock:
    context = MagicMock()
    context.args = args or []
    context.bot.send_message = AsyncMock()
    context.bot.send_chat_action = AsyncMock()
    deps = Deps(
        settings=settings,
        paperless=paperless,  # type: ignore[arg-type]
        search_store=SearchStateStore(),
        anthropic_client=MagicMock(),
        budget_store=MagicMock(),
        agent_memory=MagicMock(),
        pending_input=PendingInputStore(),
    )
    context.application.bot_data = {DEPS_KEY: deps}
    return context


async def test_help_command_replies_with_help_text(settings: Settings) -> None:
    update = make_update()
    context = make_context(settings, FakePaperless())

    await commands.help_command(update, context)

    update.message.reply_text.assert_awaited_once()
    assert "search" in update.message.reply_text.call_args[0][0]


# --- /search --------------------------------------------------------------


async def test_search_command_without_query_prompts_for_input(settings: Settings) -> None:
    update = make_update()
    context = make_context(settings, FakePaperless(), args=[])

    await commands.search_command(update, context)

    update.message.reply_text.assert_awaited_once()
    args, kwargs = update.message.reply_text.call_args
    assert args[0] == "What would you like to search for?"
    assert "reply_markup" in kwargs
    deps = context.application.bot_data[DEPS_KEY]
    assert deps.pending_input.pop(update.message.chat_id) == "search"


async def test_search_command_no_results(settings: Settings) -> None:
    update = make_update()
    paperless = FakePaperless(search_results=([], 0))
    context = make_context(settings, paperless, args=["insurance"])

    await commands.search_command(update, context)

    context.bot.send_message.assert_awaited_once_with(update.message.chat_id, "No documents found.")


async def test_search_command_sends_one_card_per_result(settings: Settings) -> None:
    update = make_update()
    doc = make_doc()
    paperless = FakePaperless(search_results=([doc], 1))
    context = make_context(settings, paperless, args=["insurance"])

    await commands.search_command(update, context)

    context.bot.send_message.assert_awaited_once()
    args, kwargs = context.bot.send_message.call_args
    assert args[0] == update.message.chat_id
    assert "<b>#412 Car insurance 2026</b>" in args[1]
    assert "PZU" in args[1]
    assert "🏷 Insurance" in args[1]
    assert kwargs["parse_mode"] is not None
    assert "reply_markup" in kwargs


async def test_search_command_sends_pagination_message_when_multiple_pages(
    settings: Settings,
) -> None:
    update = make_update()
    doc = make_doc()
    paperless = FakePaperless(search_results=([doc], 10))  # SEARCH_PAGE_SIZE=5 -> 2 pages
    context = make_context(settings, paperless, args=["insurance"])

    await commands.search_command(update, context)

    assert context.bot.send_message.await_count == 2
    page_text = context.bot.send_message.await_args_list[1].args[1]
    assert "1" in page_text and "2" in page_text


async def test_search_page_callback_sends_next_page_as_new_messages(settings: Settings) -> None:
    update = make_update()
    doc = make_doc()
    paperless = FakePaperless(search_results=([doc], 10))
    context = make_context(settings, paperless, args=["insurance"])
    await commands.search_command(update, context)  # page 1: seeds the SearchStateStore
    context.bot.send_message.reset_mock()

    deps = context.application.bot_data[DEPS_KEY]
    token = next(iter(deps.search_store._states))  # only one token exists at this point
    page_update = make_callback_update(f"sp:{token}:2")

    await commands.search_page_callback(page_update, context)

    page_update.callback_query.answer.assert_awaited_once()
    # 1 doc card + 1 pagination-control message (still sent since total_pages > 1)
    assert context.bot.send_message.await_count == 2


async def test_search_expand_callback_shows_full_card(settings: Settings) -> None:
    update = make_callback_update("xd:412")
    doc = make_doc()
    paperless = FakePaperless(detail=doc)
    context = make_context(settings, paperless)

    await commands.search_expand_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    args, kwargs = update.callback_query.edit_message_text.call_args
    assert "Open in Paperless" in args[0]
    assert "▲ Collapse" in [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]


async def test_search_collapse_callback_shows_summary_card(settings: Settings) -> None:
    update = make_callback_update("cd:412")
    doc = make_doc()
    paperless = FakePaperless(detail=doc)
    context = make_context(settings, paperless)

    await commands.search_collapse_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    args, kwargs = update.callback_query.edit_message_text.call_args
    assert "Open in Paperless" not in args[0]
    assert "▼ Details" in [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]


async def test_text_handler_continues_pending_search(settings: Settings) -> None:
    chat_id = 555
    search_update = make_update(chat_id=chat_id)
    paperless = FakePaperless()
    context = make_context(settings, paperless, args=[])
    await commands.search_command(search_update, context)

    doc = make_doc()
    paperless.search_results = ([doc], 1)
    text_update = make_update(chat_id=chat_id)
    text_update.message.text = "insurance"

    await commands.text_handler(text_update, context)

    context.bot.send_message.assert_awaited_once()
    deps = context.application.bot_data[DEPS_KEY]
    assert deps.pending_input.pop(chat_id) is None


async def test_cancel_callback_clears_pending_input(settings: Settings) -> None:
    chat_id = 777
    search_update = make_update(chat_id=chat_id)
    context = make_context(settings, FakePaperless(), args=[])
    await commands.search_command(search_update, context)

    cancel_update = make_callback_update("cx", chat_id=chat_id)
    await commands.cancel_callback(cancel_update, context)

    cancel_update.callback_query.answer.assert_awaited_once()
    cancel_update.callback_query.edit_message_text.assert_awaited_once_with("Cancelled.")
    deps = context.application.bot_data[DEPS_KEY]
    assert deps.pending_input.pop(chat_id) is None


# --- /doc -------------------------------------------------------------------


async def test_doc_command_without_id_prompts_for_input(settings: Settings) -> None:
    update = make_update()
    context = make_context(settings, FakePaperless(), args=[])

    await commands.doc_command(update, context)

    update.message.reply_text.assert_awaited_once()
    args, kwargs = update.message.reply_text.call_args
    assert args[0] == "Which document id?"
    deps = context.application.bot_data[DEPS_KEY]
    assert deps.pending_input.pop(update.message.chat_id) == "doc"


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

    args, kwargs = update.message.reply_text.call_args
    text = args[0]
    assert "<b>#412 Car insurance 2026</b>" in text
    assert "Created: 14.01.2026" in text
    assert "Correspondent: PZU" in text
    assert "Expires: 14.12.2026" in text
    assert "Open in Paperless" in text
    assert kwargs["parse_mode"] is not None
    assert "reply_markup" in kwargs


async def test_text_handler_continues_pending_doc(settings: Settings) -> None:
    chat_id = 333
    doc_update = make_update(chat_id=chat_id)
    paperless = FakePaperless(detail=make_doc())
    context = make_context(settings, paperless, args=[])
    await commands.doc_command(doc_update, context)

    text_update = make_update(chat_id=chat_id)
    text_update.message.text = "412"

    await commands.text_handler(text_update, context)

    text_update.message.reply_text.assert_awaited_once()
    assert "<b>#412" in text_update.message.reply_text.call_args[0][0]


async def test_text_handler_pending_doc_with_invalid_input(settings: Settings) -> None:
    chat_id = 334
    doc_update = make_update(chat_id=chat_id)
    context = make_context(settings, FakePaperless(), args=[])
    await commands.doc_command(doc_update, context)

    text_update = make_update(chat_id=chat_id)
    text_update.message.text = "not-a-number"

    await commands.text_handler(text_update, context)

    text_update.message.reply_text.assert_awaited_once_with("Usage: /doc <id>")


# --- /recent ----------------------------------------------------------------


async def test_recent_command_defaults_to_ten(settings: Settings) -> None:
    update = make_update()
    paperless = FakePaperless(recent_results=[make_doc()])
    context = make_context(settings, paperless, args=[])

    await commands.recent_command(update, context)

    update.message.reply_text.assert_awaited_once()
    assert "<b>#412" in update.message.reply_text.call_args[0][0]


async def test_recent_command_clamps_n(settings: Settings) -> None:
    update = make_update()
    paperless = FakePaperless(recent_results=[])
    context = make_context(settings, paperless, args=["9999"])

    await commands.recent_command(update, context)

    update.message.reply_text.assert_awaited_once_with("No documents found.")


# --- /expiring ---------------------------------------------------------------


async def test_expiring_command_no_results(settings: Settings) -> None:
    update = make_update()
    paperless = FakePaperless(custom_field_results=[])
    context = make_context(settings, paperless)

    await commands.expiring_command(update, context)

    update.message.reply_text.assert_awaited_once_with("No documents found.")


async def test_expiring_command_sorts_and_formats_relative_days(settings: Settings) -> None:
    update = make_update()
    today = date.today()
    soon = make_doc(
        id=1,
        title="Soon",
        custom_fields={"Expires": (today + timedelta(days=5)).strftime("%d.%m.%Y")},
    )
    later = make_doc(
        id=2,
        title="Later",
        custom_fields={"Expires": (today + timedelta(days=20)).strftime("%d.%m.%Y")},
    )
    paperless = FakePaperless(custom_field_results=[later, soon])
    context = make_context(settings, paperless)

    await commands.expiring_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert text.index("<b>#1 Soon</b>") < text.index("<b>#2 Later</b>")
    assert "in 5 days" in text
    assert "in 20 days" in text


async def test_expiring_command_rejects_non_numeric_days(settings: Settings) -> None:
    update = make_update()
    paperless = FakePaperless()
    context = make_context(settings, paperless, args=["abc"])

    await commands.expiring_command(update, context)

    update.message.reply_text.assert_awaited_once_with("Usage: /expiring [days]")
