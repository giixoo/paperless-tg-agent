"""Document download (📄 buttons) and upload (SPEC §4.3-4.4, milestone 2)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from telegram import Bot, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from paperbot.i18n import resolve_language, t
from paperbot.paperless import PaperlessClient, PaperlessError, TaskResult
from paperbot.telegram.commands import get_deps
from paperbot.telegram.keyboards import decode_download, doc_card_keyboard

logger = logging.getLogger(__name__)

MAX_TELEGRAM_FILE_BYTES = 50 * 1024 * 1024
TASK_POLL_INTERVAL_SECONDS = 3.0
TASK_POLL_TIMEOUT_SECONDS = 300.0

_DUPLICATE_ID_RE = re.compile(r"#(\d+)")


def _lang(update: Update) -> str:
    user = update.effective_user
    return resolve_language(user.language_code if user else None)


async def send_document_to_chat(
    bot: Bot,
    chat_id: int,
    paperless: PaperlessClient,
    public_url: str,
    doc_id: int,
    lang: str,
) -> None:
    """Send a document's original file to a chat (SPEC §4.3), falling back
    to the archive version or a Paperless link if it's too large.
    """
    try:
        downloaded = await paperless.download_document(doc_id, original=True)
    except PaperlessError:
        logger.exception("download_document (original) failed for #%d", doc_id)
        await bot.send_message(chat_id, t("generic_error", lang))
        return

    if downloaded is None:
        await bot.send_message(chat_id, t("doc_not_found", lang, id=doc_id))
        return

    content, filename = downloaded
    if len(content) > MAX_TELEGRAM_FILE_BYTES:
        try:
            archive = await paperless.download_document(doc_id, original=False)
        except PaperlessError:
            logger.exception("download_document (archive) failed for #%d", doc_id)
            archive = None
        if archive is not None and len(archive[0]) <= MAX_TELEGRAM_FILE_BYTES:
            content, filename = archive
        else:
            link = f"{public_url.rstrip('/')}/documents/{doc_id}/"
            await bot.send_message(chat_id, t("file_too_large", lang, link=link))
            return

    await bot.send_document(chat_id, document=content, filename=filename)


async def download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    doc_id = decode_download(query.data)
    if doc_id is None:
        await query.answer()
        return
    await query.answer()

    chat = update.effective_chat
    if chat is None:
        return
    deps = get_deps(context)
    public_url = str(deps.settings.paperless_public_url_or_default)
    await send_document_to_chat(
        context.bot, chat.id, deps.paperless, public_url, doc_id, _lang(update)
    )


async def _poll_task(paperless: PaperlessClient, task_id: str) -> TaskResult | None:
    deadline = time.monotonic() + TASK_POLL_TIMEOUT_SECONDS
    while True:
        try:
            result = await paperless.get_task(task_id)
        except PaperlessError:
            logger.exception("get_task failed for task_id=%s", task_id)
            result = None
        if result is not None and result.status in ("SUCCESS", "FAILURE"):
            return result
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(TASK_POLL_INTERVAL_SECONDS)


async def upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return
    lang = _lang(update)

    if message.document is not None:
        file_id = message.document.file_id
        filename = message.document.file_name or f"document_{message.document.file_unique_id}"
    elif message.photo:
        largest = message.photo[-1]
        file_id = largest.file_id
        filename = f"{largest.file_unique_id}.jpg"
    else:
        return

    if message.media_group_id:
        logger.info("Upload is part of media group %s", message.media_group_id)

    deps = get_deps(context)
    await context.bot.send_chat_action(message.chat_id, ChatAction.UPLOAD_DOCUMENT)

    tg_file = await context.bot.get_file(file_id)
    raw = await tg_file.download_as_bytearray()

    try:
        task_id = await deps.paperless.upload_document(
            bytes(raw), filename, title=message.caption or None
        )
    except PaperlessError:
        logger.exception("upload_document failed")
        await message.reply_text(t("generic_error", lang))
        return

    result = await _poll_task(deps.paperless, task_id)
    public_url = str(deps.settings.paperless_public_url_or_default)

    if result is None:
        await message.reply_text(t("upload_timeout", lang))
        return

    if result.status == "SUCCESS":
        doc_id = result.related_document
        if doc_id is None:
            await message.reply_text(t("upload_success_no_id", lang))
            return
        doc = await deps.paperless.get_document(doc_id)
        title = doc.title if doc else filename
        link = f"{public_url.rstrip('/')}/documents/{doc_id}/"
        await message.reply_text(
            t("upload_success", lang, id=doc_id, title=title, link=link),
            reply_markup=doc_card_keyboard(doc_id),
        )
        return

    # FAILURE
    error_text = result.result or ""
    if "duplicate" in error_text.lower():
        existing_match = _DUPLICATE_ID_RE.search(error_text)
        if existing_match:
            existing_id = int(existing_match.group(1))
            link = f"{public_url.rstrip('/')}/documents/{existing_id}/"
            await message.reply_text(t("upload_duplicate", lang, id=existing_id, link=link))
        else:
            await message.reply_text(t("upload_duplicate_generic", lang))
        return

    await message.reply_text(t("upload_failed", lang, error=error_text[:200]))


def register_file_handlers(application: Application[Any, Any, Any, Any, Any, Any]) -> None:
    application.add_handler(CallbackQueryHandler(download_callback, pattern=r"^dl:"))
    application.add_handler(
        MessageHandler((filters.Document.ALL | filters.PHOTO) & ~filters.COMMAND, upload_handler)
    )
