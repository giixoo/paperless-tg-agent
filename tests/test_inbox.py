from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from paperbot.config import Settings
from paperbot.paperless import Correspondent, Document, DocumentType, Tag
from paperbot.telegram import inbox
from paperbot.telegram.deps import DEPS_KEY, Deps
from paperbot.telegram.keyboards import SearchStateStore
from paperbot.telegram.state import PendingInputStore


class FakeTaxonomy:
    def __init__(
        self,
        inbox_ids: list[int] | None = None,
        tags: list[Tag] | None = None,
        correspondents: list[Correspondent] | None = None,
        document_types: list[DocumentType] | None = None,
    ) -> None:
        self._inbox_ids = inbox_ids or []
        self._tags = tags or []
        self._correspondents = correspondents or []
        self._document_types = document_types or []

    async def inbox_tag_ids(self) -> list[int]:
        return self._inbox_ids

    async def all_tags(self) -> list[Tag]:
        return self._tags

    async def all_correspondents(self) -> list[Correspondent]:
        return self._correspondents

    async def all_document_types(self) -> list[DocumentType]:
        return self._document_types

    async def tag_name(self, tag_id: int) -> str | None:
        for tag in self._tags:
            if tag.id == tag_id:
                return tag.name
        return None


@dataclass
class FakePaperless:
    detail: Document | None = None
    inbox_docs: list[Document] = field(default_factory=list)
    inbox_tag_ids: list[int] = field(default_factory=lambda: [2])
    tags: list[Tag] = field(default_factory=list)
    correspondents: list[Correspondent] = field(default_factory=list)
    document_types: list[DocumentType] = field(default_factory=list)
    bulk_modify_calls: list[tuple[list[int], list[int], list[int]]] = field(default_factory=list)
    update_calls: list[tuple[int, dict[str, Any]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.taxonomy = FakeTaxonomy(
            self.inbox_tag_ids, self.tags, self.correspondents, self.document_types
        )

    async def get_document(self, doc_id: int) -> Document | None:
        return self.detail

    async def list_by_tag_ids(self, tag_ids: list[int], limit: int = 50) -> list[Document]:
        return self.inbox_docs

    async def bulk_modify_tags(
        self,
        document_ids: list[int],
        *,
        add_tags: list[int] | None = None,
        remove_tags: list[int] | None = None,
    ) -> None:
        self.bulk_modify_calls.append((document_ids, add_tags or [], remove_tags or []))

    async def update_document(self, doc_id: int, **fields: Any) -> None:
        self.update_calls.append((doc_id, fields))


def make_doc(**overrides: Any) -> Document:
    defaults: dict[str, Any] = dict(
        id=412,
        title="Car insurance 2026",
        created=date(2026, 1, 14),
        correspondent="PZU",
        document_type="Insurance policy",
        tags=["Insurance"],
        custom_fields={},
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


def make_context(settings: Settings, paperless: FakePaperless) -> MagicMock:
    context = MagicMock()
    context.bot.send_message = AsyncMock()
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


# --- inbox_command / Done ----------------------------------------------------


async def test_inbox_command_no_inbox_tags_reports_no_results(settings: Settings) -> None:
    update = make_update()
    paperless = FakePaperless(inbox_tag_ids=[])
    context = make_context(settings, paperless)

    await inbox.inbox_command(update, context)

    update.message.reply_text.assert_awaited_once_with("No documents found.")


async def test_inbox_command_sends_one_message_per_doc_with_full_keyboard(
    settings: Settings,
) -> None:
    update = make_update()
    docs = [make_doc(id=1, title="A"), make_doc(id=2, title="B")]
    paperless = FakePaperless(inbox_docs=docs)
    context = make_context(settings, paperless)

    await inbox.inbox_command(update, context)

    assert update.message.reply_text.await_count == 2
    kwargs = update.message.reply_text.await_args_list[0].kwargs
    keyboard = kwargs["reply_markup"]
    button_texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert any("Tags" in b for b in button_texts)
    assert any("Correspondent" in b for b in button_texts)
    assert any("Type" in b for b in button_texts)
    assert any("Title" in b for b in button_texts)
    assert any("Done" in b for b in button_texts)


async def test_inbox_done_callback_removes_tags_and_clears_keyboard(settings: Settings) -> None:
    update = make_callback_update("ib:412")
    paperless = FakePaperless(inbox_tag_ids=[2])
    context = make_context(settings, paperless)

    await inbox.inbox_done_callback(update, context)

    assert paperless.bulk_modify_calls == [([412], [], [2])]
    update.callback_query.answer.assert_awaited_once_with("Removed from inbox.")
    update.callback_query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


async def test_inbox_done_callback_ignores_malformed_data(settings: Settings) -> None:
    update = make_callback_update("sp:x:1")
    paperless = FakePaperless()
    context = make_context(settings, paperless)

    await inbox.inbox_done_callback(update, context)

    assert paperless.bulk_modify_calls == []


# --- tags submenu -------------------------------------------------------------


async def test_tags_menu_shows_checkmarks_for_current_tags(settings: Settings) -> None:
    update = make_callback_update("it:412")
    tags = [Tag(id=1, name="Insurance"), Tag(id=2, name="Car")]
    paperless = FakePaperless(detail=make_doc(tags=["Insurance"]), tags=tags)
    context = make_context(settings, paperless)

    await inbox.tags_menu_callback(update, context)

    keyboard = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
    labels = {b.text for row in keyboard.inline_keyboard for b in row}
    assert "✅ Insurance" in labels
    assert "⬜ Car" in labels


async def test_tag_toggle_adds_tag_not_present(settings: Settings) -> None:
    update = make_callback_update("tt:412:2")
    tags = [Tag(id=1, name="Insurance"), Tag(id=2, name="Car")]
    paperless = FakePaperless(detail=make_doc(tags=["Insurance"]), tags=tags)
    context = make_context(settings, paperless)

    await inbox.tag_toggle_callback(update, context)

    assert paperless.bulk_modify_calls == [([412], [2], [])]


async def test_tag_toggle_removes_tag_present(settings: Settings) -> None:
    update = make_callback_update("tt:412:1")
    tags = [Tag(id=1, name="Insurance"), Tag(id=2, name="Car")]
    paperless = FakePaperless(detail=make_doc(tags=["Insurance"]), tags=tags)
    context = make_context(settings, paperless)

    await inbox.tag_toggle_callback(update, context)

    assert paperless.bulk_modify_calls == [([412], [], [1])]


# --- correspondent / type submenus --------------------------------------------


async def test_corr_set_callback_updates_document(settings: Settings) -> None:
    update = make_callback_update("sc:412:20")
    paperless = FakePaperless(detail=make_doc())
    context = make_context(settings, paperless)

    await inbox.corr_set_callback(update, context)

    assert paperless.update_calls == [(412, {"correspondent": 20})]


async def test_corr_set_callback_clear_sets_none(settings: Settings) -> None:
    update = make_callback_update("sc:412:0")
    paperless = FakePaperless(detail=make_doc())
    context = make_context(settings, paperless)

    await inbox.corr_set_callback(update, context)

    assert paperless.update_calls == [(412, {"correspondent": None})]


async def test_type_set_callback_updates_document(settings: Settings) -> None:
    update = make_callback_update("sy:412:10")
    paperless = FakePaperless(detail=make_doc())
    context = make_context(settings, paperless)

    await inbox.type_set_callback(update, context)

    assert paperless.update_calls == [(412, {"document_type": 10})]


# --- rename flow ---------------------------------------------------------------


async def test_rename_prompt_sets_pending_input(settings: Settings) -> None:
    chat_id = 123
    update = make_callback_update("ir:412", chat_id=chat_id)
    context = make_context(settings, FakePaperless())

    await inbox.rename_prompt_callback(update, context)

    deps = context.application.bot_data[DEPS_KEY]
    assert deps.pending_input.pop(chat_id) == "rename:412"
    context.bot.send_message.assert_awaited_once()


async def test_handle_rename_input_updates_title(settings: Settings) -> None:
    update = make_update()
    paperless = FakePaperless(detail=make_doc(title="New title"))
    context = make_context(settings, paperless)

    await inbox.handle_rename_input(update, context, 412, "New title")

    assert paperless.update_calls == [(412, {"title": "New title"})]
    assert update.message.reply_text.await_count == 2
