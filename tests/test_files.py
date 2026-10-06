from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from paperbot.config import Settings
from paperbot.paperless import Document, PaperlessError, TaskResult
from paperbot.telegram import commands, files
from paperbot.telegram.keyboards import SearchStateStore

PUBLIC_URL = "http://paperless.local:8000"


@dataclass
class FakePaperless:
    downloads: dict[tuple[int, bool], tuple[bytes, str] | None] = field(default_factory=dict)
    download_error: Exception | None = None
    upload_task_id: str = "task-1"
    upload_error: Exception | None = None
    task_results: list[TaskResult | None] = field(default_factory=list)
    detail: Document | None = None

    async def download_document(self, doc_id: int, *, original: bool) -> tuple[bytes, str] | None:
        if self.download_error is not None:
            raise self.download_error
        return self.downloads.get((doc_id, original))

    async def upload_document(
        self, content: bytes, filename: str, *, title: str | None = None
    ) -> str:
        if self.upload_error is not None:
            raise self.upload_error
        return self.upload_task_id

    async def get_task(self, task_id: str) -> TaskResult | None:
        if not self.task_results:
            return None
        return self.task_results.pop(0)

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


def make_context(settings: Settings, paperless: FakePaperless) -> MagicMock:
    context = MagicMock()
    deps = commands.Deps(settings=settings, paperless=paperless, search_store=SearchStateStore())  # type: ignore[arg-type]
    context.application.bot_data = {commands.DEPS_KEY: deps}
    context.bot.send_chat_action = AsyncMock()
    return context


def make_upload_update(
    *, filename: str = "scan.pdf", caption: str | None = None, media_group_id: str | None = None
) -> MagicMock:
    update = MagicMock()
    update.effective_user.language_code = "en"
    update.message.document.file_id = "file-123"
    update.message.document.file_unique_id = "uniq-123"
    update.message.document.file_name = filename
    update.message.photo = []
    update.message.caption = caption
    update.message.media_group_id = media_group_id
    update.message.chat_id = 42
    update.message.reply_text = AsyncMock()
    return update


# --- send_document_to_chat ---------------------------------------------------


async def test_send_document_to_chat_sends_small_file() -> None:
    bot = AsyncMock()
    paperless = FakePaperless(downloads={(412, True): (b"small", "car.pdf")})

    await files.send_document_to_chat(bot, 1, paperless, PUBLIC_URL, 412, "en")

    bot.send_document.assert_awaited_once_with(1, document=b"small", filename="car.pdf")


async def test_send_document_to_chat_falls_back_to_archive_when_too_large() -> None:
    bot = AsyncMock()
    big = b"x" * (files.MAX_TELEGRAM_FILE_BYTES + 1)
    paperless = FakePaperless(
        downloads={
            (412, True): (big, "car.pdf"),
            (412, False): (b"small-archive", "car.pdf"),
        }
    )

    await files.send_document_to_chat(bot, 1, paperless, PUBLIC_URL, 412, "en")

    bot.send_document.assert_awaited_once_with(1, document=b"small-archive", filename="car.pdf")


async def test_send_document_to_chat_sends_link_when_both_too_large() -> None:
    bot = AsyncMock()
    big = b"x" * (files.MAX_TELEGRAM_FILE_BYTES + 1)
    paperless = FakePaperless(
        downloads={(412, True): (big, "car.pdf"), (412, False): (big, "car.pdf")}
    )

    await files.send_document_to_chat(bot, 1, paperless, PUBLIC_URL, 412, "en")

    bot.send_document.assert_not_awaited()
    bot.send_message.assert_awaited_once()
    assert "412" in bot.send_message.call_args[0][1]


async def test_send_document_to_chat_not_found() -> None:
    bot = AsyncMock()
    paperless = FakePaperless(downloads={})

    await files.send_document_to_chat(bot, 1, paperless, PUBLIC_URL, 999, "en")

    bot.send_document.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(1, "Document #999 not found.")


async def test_send_document_to_chat_handles_paperless_error() -> None:
    bot = AsyncMock()
    paperless = FakePaperless(download_error=PaperlessError("boom"))

    await files.send_document_to_chat(bot, 1, paperless, PUBLIC_URL, 412, "en")

    bot.send_document.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(
        1, "Something went wrong talking to Paperless. Please try again."
    )


# --- upload_handler -----------------------------------------------------------


async def test_upload_handler_success(settings: Settings) -> None:
    update = make_upload_update(caption="My scan")
    paperless = FakePaperless(
        task_results=[TaskResult(status="SUCCESS", result="ok", related_document=412)],
        detail=make_doc(),
    )
    context = make_context(settings, paperless)
    context.bot.get_file = AsyncMock(
        return_value=MagicMock(download_as_bytearray=AsyncMock(return_value=bytearray(b"bytes")))
    )

    await files.upload_handler(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "#412" in text
    assert "Car insurance 2026" in text


async def test_upload_handler_success_without_related_document(settings: Settings) -> None:
    update = make_upload_update()
    paperless = FakePaperless(
        task_results=[TaskResult(status="SUCCESS", result="ok", related_document=None)]
    )
    context = make_context(settings, paperless)
    context.bot.get_file = AsyncMock(
        return_value=MagicMock(download_as_bytearray=AsyncMock(return_value=bytearray(b"bytes")))
    )

    await files.upload_handler(update, context)

    update.message.reply_text.assert_awaited_once_with(
        "✅ Document added. OCR + classification will run in background."
    )


async def test_upload_handler_duplicate_with_id(settings: Settings) -> None:
    update = make_upload_update()
    paperless = FakePaperless(
        task_results=[
            TaskResult(
                status="FAILURE",
                result="Not consuming scan.pdf: It is a duplicate of Car insurance 2026 (#412)",
                related_document=None,
            )
        ]
    )
    context = make_context(settings, paperless)
    context.bot.get_file = AsyncMock(
        return_value=MagicMock(download_as_bytearray=AsyncMock(return_value=bytearray(b"bytes")))
    )

    await files.upload_handler(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "412" in text


async def test_upload_handler_generic_failure(settings: Settings) -> None:
    update = make_upload_update()
    paperless = FakePaperless(
        task_results=[TaskResult(status="FAILURE", result="disk full", related_document=None)]
    )
    context = make_context(settings, paperless)
    context.bot.get_file = AsyncMock(
        return_value=MagicMock(download_as_bytearray=AsyncMock(return_value=bytearray(b"bytes")))
    )

    await files.upload_handler(update, context)

    update.message.reply_text.assert_awaited_once_with("Upload failed: disk full")


async def test_upload_handler_timeout(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files, "TASK_POLL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(files, "TASK_POLL_INTERVAL_SECONDS", 0.01)
    update = make_upload_update()
    paperless = FakePaperless(task_results=[])  # get_task always returns None (still pending)
    context = make_context(settings, paperless)
    context.bot.get_file = AsyncMock(
        return_value=MagicMock(download_as_bytearray=AsyncMock(return_value=bytearray(b"bytes")))
    )

    await files.upload_handler(update, context)

    update.message.reply_text.assert_awaited_once_with(
        "Still processing after 5 minutes — check Paperless later."
    )


async def test_upload_handler_ignores_text_only_messages(settings: Settings) -> None:
    update = MagicMock()
    update.message.document = None
    update.message.photo = []
    context = make_context(settings, FakePaperless())

    await files.upload_handler(update, context)

    context.bot.get_file.assert_not_called()
