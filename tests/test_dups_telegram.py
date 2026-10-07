from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from paperbot.config import Settings
from paperbot.dups.db import DupsStore
from paperbot.paperless import CustomFieldDef, Document, DocumentMetadata, PaperlessError, Tag
from paperbot.telegram import dups
from paperbot.telegram.deps import DEPS_KEY, Deps
from paperbot.telegram.keyboards import SearchStateStore
from paperbot.telegram.state import DupsSkipStore, PendingInputStore


@dataclass
class FakeTaxonomy:
    correspondent_ids: dict[str, int] = field(default_factory=dict)
    tag_ids: dict[str, int] = field(default_factory=dict)
    document_type_ids: dict[str, int] = field(default_factory=dict)
    custom_fields: list[CustomFieldDef] = field(default_factory=list)
    tags: list[Tag] = field(default_factory=list)
    hidden: list[Tag] = field(default_factory=list)

    async def resolve_tag_id(self, name: str) -> int | None:
        return self.tag_ids.get(name)

    async def resolve_correspondent_id(self, name: str) -> int | None:
        return self.correspondent_ids.get(name)

    async def resolve_document_type_id(self, name: str) -> int | None:
        return self.document_type_ids.get(name)

    async def all_custom_fields(self) -> list[CustomFieldDef]:
        return self.custom_fields

    async def all_tags(self) -> list[Tag]:
        return self.tags

    async def hidden_tags(self) -> list[Tag]:
        return self.hidden


@dataclass
class FakePaperless:
    docs: dict[int, Document] = field(default_factory=dict)
    metadata: dict[int, DocumentMetadata] = field(default_factory=dict)
    all_docs: list[Document] = field(default_factory=list)
    taxonomy: FakeTaxonomy = field(default_factory=FakeTaxonomy)
    download_calls: list[tuple[int, bool]] = field(default_factory=list)
    bulk_modify_calls: list[tuple[list[int], list[int], list[int]]] = field(default_factory=list)
    update_calls: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    note_calls: list[tuple[int, str]] = field(default_factory=list)
    trash_calls: list[int] = field(default_factory=list)
    restore_calls: list[int] = field(default_factory=list)
    restore_result: bool = True
    update_raises: bool = False
    trash_raises: bool = False

    async def get_document(self, doc_id: int) -> Document | None:
        return self.docs.get(doc_id)

    async def get_document_metadata(self, doc_id: int) -> DocumentMetadata | None:
        return self.metadata.get(doc_id)

    async def list_all_documents(self) -> list[Document]:
        return self.all_docs

    async def download_document(self, doc_id: int, *, original: bool) -> tuple[bytes, str] | None:
        self.download_calls.append((doc_id, original))
        if doc_id not in self.docs:
            return None
        return b"filebytes", f"doc{doc_id}.pdf"

    async def bulk_modify_tags(
        self,
        document_ids: list[int],
        *,
        add_tags: list[int] | None = None,
        remove_tags: list[int] | None = None,
    ) -> None:
        self.bulk_modify_calls.append((document_ids, add_tags or [], remove_tags or []))

    async def update_document(self, doc_id: int, **fields: Any) -> None:
        if self.update_raises:
            raise PaperlessError("boom")
        self.update_calls.append((doc_id, fields))

    async def add_note(self, doc_id: int, text: str) -> None:
        self.note_calls.append((doc_id, text))

    async def trash_document(self, doc_id: int) -> None:
        if self.trash_raises:
            raise PaperlessError("boom")
        self.trash_calls.append(doc_id)

    async def restore_from_trash(self, doc_id: int) -> bool:
        self.restore_calls.append(doc_id)
        return self.restore_result


def make_doc(**overrides: Any) -> Document:
    defaults: dict[str, Any] = dict(
        id=1,
        title="Doc",
        created=date(2026, 1, 1),
        correspondent=None,
        document_type=None,
        tags=[],
        custom_fields={},
        custom_fields_raw=[],
        notes_list=[],
        content="some content",
        page_count=1,
        mime_type="application/pdf",
    )
    defaults.update(overrides)
    return Document(**defaults)


def make_settings() -> Settings:
    return Settings(
        telegram_bot_token="test-token",  # noqa: S106
        telegram_allowed_users=[1],
        paperless_url="http://paperless.test",  # type: ignore[arg-type]
        paperless_token="test-paperless-token",  # noqa: S106
        anthropic_api_key="test-anthropic-key",  # noqa: S106
    )


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
    return update


def make_context(settings: Settings, paperless: FakePaperless, dups_store: DupsStore) -> MagicMock:
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot.send_document = AsyncMock()
    context.args = []
    deps = Deps(
        settings=settings,
        paperless=paperless,  # type: ignore[arg-type]
        search_store=SearchStateStore(),
        anthropic_client=MagicMock(),
        budget_store=MagicMock(),
        agent_memory=MagicMock(),
        pending_input=PendingInputStore(),
        dups_store=dups_store,
        dups_skip_store=DupsSkipStore(),
    )
    context.application.bot_data = {DEPS_KEY: deps}
    return context


def make_store(tmp_path: Path) -> DupsStore:
    return DupsStore(tmp_path / "dups.sqlite3")


# --- /dups (no open pairs / next pair) ------------------------------------------


async def test_dups_command_no_open_pairs(tmp_path: Path) -> None:
    settings = make_settings()
    store = make_store(tmp_path)
    context = make_context(settings, FakePaperless(), store)
    update = make_update()

    await dups.dups_command(update, context)

    context.bot.send_message.assert_awaited_once_with(update.message.chat_id, "No open pairs.")


async def test_dups_command_shows_next_pair_card(tmp_path: Path) -> None:
    settings = make_settings()
    store = make_store(tmp_path)
    await store.upsert_pair(1, 2, 0.9, 0.95)
    paperless = FakePaperless(
        docs={1: make_doc(id=1, title="Invoice A"), 2: make_doc(id=2, title="Invoice B")},
    )
    context = make_context(settings, paperless, store)
    update = make_update()

    await dups.dups_command(update, context)

    context.bot.send_message.assert_awaited_once()
    args, kwargs = context.bot.send_message.call_args
    text = args[1]
    assert "Invoice A" in text
    assert "Invoice B" in text
    keyboard = kwargs["reply_markup"]
    button_texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "Keep A" in button_texts
    assert "Keep B" in button_texts
    assert "Not duplicates" in button_texts
    assert "Skip" in button_texts


async def test_dups_command_drops_pair_when_a_document_is_missing(tmp_path: Path) -> None:
    settings = make_settings()
    store = make_store(tmp_path)
    await store.upsert_pair(1, 2, 0.9, 0.95)
    paperless = FakePaperless(docs={1: make_doc(id=1)})  # doc 2 missing
    context = make_context(settings, paperless, store)
    update = make_update()

    await dups.dups_command(update, context)

    context.bot.send_message.assert_awaited_once_with(update.message.chat_id, "No open pairs.")
    pair = await store.get_pair(1)
    assert pair is not None
    assert pair.status == "not_dup"


# --- /dups scan / stats / undo --------------------------------------------------


async def test_dups_scan_subcommand_reports_count(tmp_path: Path) -> None:
    settings = make_settings()
    store = make_store(tmp_path)
    paperless = FakePaperless(all_docs=[])
    context = make_context(settings, paperless, store)
    context.args = ["scan"]
    update = make_update()

    await dups.dups_command(update, context)

    context.bot.send_message.assert_awaited_once_with(
        update.message.chat_id, "Scan done: 0 new possible duplicate pairs."
    )


async def test_dups_stats_subcommand_reports_counts(tmp_path: Path) -> None:
    settings = make_settings()
    store = make_store(tmp_path)
    await store.upsert_pair(1, 2, 0.9, 0.9)
    context = make_context(settings, FakePaperless(), store)
    context.args = ["stats"]
    update = make_update()

    await dups.dups_command(update, context)

    text = context.bot.send_message.call_args.args[1]
    assert "Open: 1" in text
    assert "never" in text


async def test_dups_undo_subcommand_nothing_to_undo(tmp_path: Path) -> None:
    settings = make_settings()
    store = make_store(tmp_path)
    context = make_context(settings, FakePaperless(), store)
    context.args = ["undo"]
    update = make_update()

    await dups.dups_command(update, context)

    context.bot.send_message.assert_awaited_once_with(update.message.chat_id, "Nothing to undo.")


async def test_dups_undo_subcommand_restores_and_reports_copied_items(tmp_path: Path) -> None:
    settings = make_settings()
    store = make_store(tmp_path)
    await store.record_action(1, survivor_id=10, loser_id=20, applied=["tag: Car"])
    paperless = FakePaperless(restore_result=True)
    context = make_context(settings, paperless, store)
    context.args = ["undo"]
    update = make_update()

    await dups.dups_command(update, context)

    assert paperless.restore_calls == [20]
    text = context.bot.send_message.call_args.args[1]
    assert "#20" in text
    assert "#10" in text
    assert "tag: Car" in text


# --- download buttons ------------------------------------------------------------


async def test_download_a_callback_sends_document(tmp_path: Path) -> None:
    settings = make_settings()
    store = make_store(tmp_path)
    pair_id = await store.upsert_pair(1, 2, 0.9, 0.9)
    assert pair_id is not None
    paperless = FakePaperless(docs={1: make_doc(id=1)})
    context = make_context(settings, paperless, store)
    update = make_callback_update(f"d:dA:{pair_id}")

    await dups.download_a_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    assert paperless.download_calls == [(1, True)]
    context.bot.send_document.assert_awaited_once()


# --- not duplicates / skip -------------------------------------------------------


async def test_not_dup_callback_marks_not_dup_and_advances(tmp_path: Path) -> None:
    settings = make_settings()
    store = make_store(tmp_path)
    pair_id = await store.upsert_pair(1, 2, 0.9, 0.9)
    assert pair_id is not None
    context = make_context(settings, FakePaperless(), store)
    update = make_callback_update(f"d:nd:{pair_id}")

    await dups.not_dup_callback(update, context)

    pair = await store.get_pair(pair_id)
    assert pair is not None
    assert pair.status == "not_dup"
    context.bot.send_message.assert_awaited_once_with(update.effective_chat.id, "No open pairs.")


async def test_skip_callback_shows_different_pair(tmp_path: Path) -> None:
    settings = make_settings()
    store = make_store(tmp_path)
    high_id = await store.upsert_pair(1, 2, 0.95, 0.9)
    low_id = await store.upsert_pair(3, 4, 0.81, 0.9)
    assert high_id is not None and low_id is not None
    paperless = FakePaperless(
        docs={
            1: make_doc(id=1, title="A1"),
            2: make_doc(id=2, title="A2"),
            3: make_doc(id=3, title="B1"),
            4: make_doc(id=4, title="B2"),
        }
    )
    context = make_context(settings, paperless, store)
    update = make_callback_update(f"d:sk:{high_id}")

    await dups.skip_callback(update, context)

    text = context.bot.send_message.call_args.args[1]
    assert "B1" in text
    assert "A1" not in text
    # the skipped pair is still open - it comes back on a fresh /dups
    pair = await store.get_pair(high_id)
    assert pair is not None
    assert pair.status == "open"


# --- Keep A / selection screen / finalize ---------------------------------------


async def test_keep_callback_with_no_diff_finalizes_immediately(tmp_path: Path) -> None:
    settings = make_settings()
    store = make_store(tmp_path)
    pair_id = await store.upsert_pair(1, 2, 0.9, 0.9)
    assert pair_id is not None
    paperless = FakePaperless(docs={1: make_doc(id=1), 2: make_doc(id=2)})
    context = make_context(settings, paperless, store)
    update = make_callback_update(f"d:kA:{pair_id}")

    await dups.keep_a_callback(update, context)

    assert paperless.trash_calls == [2]
    pair = await store.get_pair(pair_id)
    assert pair is not None
    assert pair.status == "resolved"
    text = context.bot.send_message.call_args.args[1]
    assert "Kept #1" in text
    assert "trash" in text


async def test_keep_callback_with_diff_shows_selection_screen(tmp_path: Path) -> None:
    settings = make_settings()
    store = make_store(tmp_path)
    pair_id = await store.upsert_pair(1, 2, 0.9, 0.9)
    assert pair_id is not None
    paperless = FakePaperless(
        docs={
            1: make_doc(id=1, correspondent=None),
            2: make_doc(id=2, correspondent="PZU"),
        },
        taxonomy=FakeTaxonomy(correspondent_ids={"PZU": 20}),
    )
    context = make_context(settings, paperless, store)
    update = make_callback_update(f"d:kA:{pair_id}")

    await dups.keep_a_callback(update, context)

    assert paperless.trash_calls == []  # not finalized yet
    text = context.bot.send_message.call_args.args[1]
    assert "correspondent: PZU" in text
    keyboard = context.bot.send_message.call_args.kwargs["reply_markup"]
    button_texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert any("Copy all" in b for b in button_texts)
    assert any("Copy selected" in b for b in button_texts)

    session = await store.get_session(pair_id)
    assert session is not None
    assert session.survivor_id == 1
    assert session.loser_id == 2


async def test_toggle_callback_flips_checked_state(tmp_path: Path) -> None:
    settings = make_settings()
    store = make_store(tmp_path)
    pair_id = await store.upsert_pair(1, 2, 0.9, 0.9)
    assert pair_id is not None
    await store.save_session(
        pair_id, survivor_id=1, loser_id=2, items=[{"kind": "tag", "label": "tag: a"}], checked=[]
    )
    context = make_context(settings, FakePaperless(), store)
    update = make_callback_update(f"d:ti:{pair_id}:0")

    await dups.toggle_callback(update, context)

    session = await store.get_session(pair_id)
    assert session is not None
    assert session.checked == [0]


async def test_copy_all_callback_applies_non_conflict_items_and_trashes_loser(
    tmp_path: Path,
) -> None:
    settings = make_settings()
    store = make_store(tmp_path)
    pair_id = await store.upsert_pair(1, 2, 0.9, 0.9)
    assert pair_id is not None
    paperless = FakePaperless(docs={1: make_doc(id=1), 2: make_doc(id=2)})
    context = make_context(settings, paperless, store)

    # A session with one tag item + one conflict item, to exercise "Copy
    # all" excluding the conflict (SPEC-dups §5.3).
    await store.save_session(
        pair_id,
        survivor_id=1,
        loser_id=2,
        items=[
            {"kind": "tag", "label": "tag: Car", "tag_id": 5},
            {"kind": "conflict", "label": "conflict", "is_conflict": True, "custom_field_id": 30},
        ],
        checked=[],
    )
    update = make_callback_update(f"d:ca:{pair_id}")

    await dups.copy_all_callback(update, context)

    assert paperless.bulk_modify_calls == [([1], [5], [])]
    assert paperless.trash_calls == [2]
    pair = await store.get_pair(pair_id)
    assert pair is not None
    assert pair.status == "resolved"
    assert await store.get_session(pair_id) is None


async def test_no_copy_callback_applies_nothing_but_still_trashes_loser(tmp_path: Path) -> None:
    settings = make_settings()
    store = make_store(tmp_path)
    pair_id = await store.upsert_pair(1, 2, 0.9, 0.9)
    assert pair_id is not None
    paperless = FakePaperless(docs={1: make_doc(id=1), 2: make_doc(id=2)})
    context = make_context(settings, paperless, store)
    await store.save_session(
        pair_id,
        survivor_id=1,
        loser_id=2,
        items=[{"kind": "tag", "label": "tag: Car", "tag_id": 5}],
        checked=[],
    )
    update = make_callback_update(f"d:nc:{pair_id}")

    await dups.no_copy_callback(update, context)

    assert paperless.bulk_modify_calls == []
    assert paperless.trash_calls == [2]


async def test_cancel_callback_leaves_pair_open_and_deletes_session(tmp_path: Path) -> None:
    settings = make_settings()
    store = make_store(tmp_path)
    pair_id = await store.upsert_pair(1, 2, 0.9, 0.9)
    assert pair_id is not None
    await store.save_session(pair_id, survivor_id=1, loser_id=2, items=[], checked=[])
    context = make_context(settings, FakePaperless(), store)
    update = make_callback_update(f"d:cc:{pair_id}")

    await dups.cancel_callback(update, context)

    update.callback_query.edit_message_text.assert_awaited_once_with("Cancelled. Nothing changed.")
    assert await store.get_session(pair_id) is None
    pair = await store.get_pair(pair_id)
    assert pair is not None
    assert pair.status == "open"


async def test_apply_order_does_not_trash_loser_when_metadata_step_fails(tmp_path: Path) -> None:
    """SPEC-dups §5.4.2/§8: the loser must not be trashed when applying
    metadata to the survivor fails."""
    settings = make_settings()
    store = make_store(tmp_path)
    pair_id = await store.upsert_pair(1, 2, 0.9, 0.9)
    assert pair_id is not None
    paperless = FakePaperless(docs={1: make_doc(id=1), 2: make_doc(id=2)}, update_raises=True)
    context = make_context(settings, paperless, store)
    await store.save_session(
        pair_id,
        survivor_id=1,
        loser_id=2,
        items=[{"kind": "correspondent", "label": "correspondent: PZU", "correspondent_id": 20}],
        checked=[],
    )
    update = make_callback_update(f"d:ca:{pair_id}")

    await dups.copy_all_callback(update, context)

    assert paperless.trash_calls == []
    pair = await store.get_pair(pair_id)
    assert pair is not None
    assert pair.status == "open"
    update.callback_query.edit_message_text.assert_awaited_once_with(
        "Something went wrong applying changes. The pair stays open."
    )
