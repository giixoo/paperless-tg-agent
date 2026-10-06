"""/help /search /recent /doc /usage /clear command handlers + free-text
agent routing (SPEC §4.2, §4.5)."""

from __future__ import annotations

import logging
import math
from datetime import date
from typing import Any

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from paperbot.agent.agent import run_agent
from paperbot.i18n import resolve_language, t
from paperbot.paperless import Document, PaperlessError
from paperbot.telegram.deps import get_deps
from paperbot.telegram.files import send_document_to_chat
from paperbot.telegram.keyboards import (
    SearchState,
    decode_search_page,
    doc_card_keyboard,
    search_results_keyboard,
)

logger = logging.getLogger(__name__)

SEARCH_PAGE_SIZE = 5
RECENT_DEFAULT = 10
RECENT_MAX = 50
USAGE_HISTORY_DAYS = 7


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
    keyboard = search_results_keyboard(docs, token, page=1, total_pages=total_pages)
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
    keyboard = search_results_keyboard(docs, token, page=page, total_pages=total_pages)
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
    await update.message.reply_text(
        _format_doc_card(doc, lang, public_url), reply_markup=doc_card_keyboard(doc.id)
    )


async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    lang = _lang(update)
    deps = get_deps(context)
    today = await deps.budget_store.today_usage(deps.settings.tz)
    lines = [t("usage_today", lang, cost=today.cost_usd, budget=deps.settings.daily_budget_usd)]

    history = await deps.budget_store.last_n_days(USAGE_HISTORY_DAYS)
    if history:
        lines.append("")
        lines.append(t("usage_history_header", lang))
        lines.extend(f"{day.day}: ${day.cost_usd:.4f}" for day in history)

    await update.message.reply_text("\n".join(lines))


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    lang = _lang(update)
    deps = get_deps(context)
    deps.agent_memory.clear(update.message.chat_id)
    await update.message.reply_text(t("memory_cleared", lang))


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route any non-command text message to the agent (SPEC §4.5)."""
    message = update.message
    if message is None or not message.text:
        return
    lang = _lang(update)
    deps = get_deps(context)
    chat_id = message.chat_id
    public_url = str(deps.settings.paperless_public_url_or_default)

    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

    async def _send_document_callback(doc_id: int) -> bool:
        try:
            await send_document_to_chat(
                context.bot, chat_id, deps.paperless, public_url, doc_id, lang
            )
        except Exception:
            logger.exception("send_document tool callback failed for #%d", doc_id)
            return False
        return True

    try:
        answer = await run_agent(
            chat_id=chat_id,
            user_text=message.text,
            lang=lang,
            anthropic_client=deps.anthropic_client,
            settings=deps.settings,
            paperless=deps.paperless,
            budget_store=deps.budget_store,
            memory=deps.agent_memory,
            send_document_callback=_send_document_callback,
        )
    except Exception:
        logger.exception("agent run failed")
        await message.reply_text(t("generic_error", lang))
        return

    if answer:
        await message.reply_text(answer)


def register_handlers(application: Application[Any, Any, Any, Any, Any, Any]) -> None:
    application.add_handler(CommandHandler(["start", "help"], help_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("recent", recent_command))
    application.add_handler(CommandHandler("doc", doc_command))
    application.add_handler(CommandHandler("usage", usage_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CallbackQueryHandler(search_page_callback, pattern=r"^sp:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
