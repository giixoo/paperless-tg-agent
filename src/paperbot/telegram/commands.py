"""/help /search /recent /doc command handlers (SPEC §4.2, milestone 1)."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date
from typing import Any, cast

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from paperbot.config import Settings
from paperbot.i18n import resolve_language, t
from paperbot.paperless import Document, PaperlessClient, PaperlessError
from paperbot.telegram.keyboards import (
    SearchState,
    SearchStateStore,
    decode_search_page,
    search_pagination_keyboard,
)

logger = logging.getLogger(__name__)

SEARCH_PAGE_SIZE = 5
RECENT_DEFAULT = 10
RECENT_MAX = 50

DEPS_KEY = "deps"


@dataclass(slots=True)
class Deps:
    settings: Settings
    paperless: PaperlessClient
    search_store: SearchStateStore


def get_deps(context: ContextTypes.DEFAULT_TYPE) -> Deps:
    return cast(Deps, context.application.bot_data[DEPS_KEY])


def _lang(update: Update) -> str:
    user = update.effective_user
    return resolve_language(user.language_code if user else None)


def _format_date(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else "—"


def _format_doc_line(doc: Document) -> str:
    parts = [f"#{doc.id}", doc.title, _format_date(doc.created)]
    if doc.correspondent:
        parts.append(doc.correspondent)
    return " · ".join(parts)


def _format_doc_list(docs: list[Document]) -> str:
    return "\n".join(_format_doc_line(d) for d in docs)


def _format_doc_card(doc: Document, lang: str, public_url: str) -> str:
    lines = [f"#{doc.id} {doc.title}", ""]
    lines.append(f"{t('field_created', lang)}: {_format_date(doc.created)}")
    if doc.correspondent:
        lines.append(f"{t('field_correspondent', lang)}: {doc.correspondent}")
    if doc.document_type:
        lines.append(f"{t('field_type', lang)}: {doc.document_type}")
    if doc.tags:
        lines.append(f"{t('field_tags', lang)}: {', '.join(doc.tags)}")
    for name, value in doc.custom_fields.items():
        lines.append(f"{name}: {value}")
    lines.append("")
    base = public_url.rstrip("/")
    lines.append(f"{t('field_link', lang)}: {base}/documents/{doc.id}/")
    return "\n".join(lines)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(t("help_text", _lang(update)))


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    lang = _lang(update)
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text(t("search_usage", lang))
        return

    deps = get_deps(context)
    try:
        docs, total = await deps.paperless.search_documents(
            query, page=1, page_size=SEARCH_PAGE_SIZE
        )
    except PaperlessError:
        logger.exception("search_documents failed")
        await update.message.reply_text(t("generic_error", lang))
        return

    if not docs:
        await update.message.reply_text(t("no_results", lang))
        return

    token = deps.search_store.put(SearchState(query=query))
    total_pages = math.ceil(total / SEARCH_PAGE_SIZE)
    keyboard = search_pagination_keyboard(token, page=1, total_pages=total_pages)
    await update.message.reply_text(_format_doc_list(docs), reply_markup=keyboard)


async def search_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    decoded = decode_search_page(query.data)
    if decoded is None:
        await query.answer()
        return
    token, page = decoded

    deps = get_deps(context)
    state = deps.search_store.get(token)
    if state is None:
        await query.answer(t("generic_error", _lang(update)), show_alert=True)
        return

    try:
        docs, total = await deps.paperless.search_documents(
            state.query, page=page, page_size=SEARCH_PAGE_SIZE, extra_params=state.extra_params
        )
    except PaperlessError:
        logger.exception("search_documents (page) failed")
        await query.answer(t("generic_error", _lang(update)), show_alert=True)
        return

    await query.answer()
    total_pages = max(1, math.ceil(total / SEARCH_PAGE_SIZE))
    keyboard = search_pagination_keyboard(token, page=page, total_pages=total_pages)
    await query.edit_message_text(_format_doc_list(docs), reply_markup=keyboard)


async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    lang = _lang(update)
    n = RECENT_DEFAULT
    if context.args:
        try:
            n = max(1, min(RECENT_MAX, int(context.args[0])))
        except ValueError:
            pass

    deps = get_deps(context)
    try:
        docs = await deps.paperless.recent_documents(limit=n)
    except PaperlessError:
        logger.exception("recent_documents failed")
        await update.message.reply_text(t("generic_error", lang))
        return

    if not docs:
        await update.message.reply_text(t("no_results", lang))
        return
    await update.message.reply_text(_format_doc_list(docs))


async def doc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    lang = _lang(update)
    if not context.args:
        await update.message.reply_text(t("doc_usage", lang))
        return
    try:
        doc_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(t("doc_usage", lang))
        return

    deps = get_deps(context)
    try:
        doc = await deps.paperless.get_document(doc_id)
    except PaperlessError:
        logger.exception("get_document failed")
        await update.message.reply_text(t("generic_error", lang))
        return

    if doc is None:
        await update.message.reply_text(t("doc_not_found", lang, id=doc_id))
        return

    public_url = str(deps.settings.paperless_public_url_or_default)
    await update.message.reply_text(_format_doc_card(doc, lang, public_url))


def register_handlers(application: Application[Any, Any, Any, Any, Any, Any]) -> None:
    application.add_handler(CommandHandler(["start", "help"], help_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("recent", recent_command))
    application.add_handler(CommandHandler("doc", doc_command))
    application.add_handler(CallbackQueryHandler(search_page_callback, pattern=r"^sp:"))
