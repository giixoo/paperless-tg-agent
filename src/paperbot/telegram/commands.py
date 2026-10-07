"""/help /search /recent /doc /expiring /usage /clear command handlers +
free-text agent routing (SPEC §4.2, §4.5). /inbox lives in `inbox.py`."""

from __future__ import annotations

import html
import json
import logging
import math
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from telegram import Bot, BotCommand, ForceReply, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from paperbot.agent.agent import run_agent
from paperbot.i18n import SUPPORTED_LANGUAGES, resolve_language, t
from paperbot.paperless import Document, PaperlessError
from paperbot.telegram.deps import Deps, get_deps
from paperbot.telegram.files import send_document_to_chat
from paperbot.telegram.inbox import handle_rename_input
from paperbot.telegram.keyboards import (
    SearchState,
    agent_doc_actions_keyboard,
    decode_preview,
    decode_search_collapse,
    decode_search_expand,
    decode_search_page,
    doc_card_keyboard,
    search_card_keyboard,
    search_page_nav_keyboard,
)

logger = logging.getLogger(__name__)

SEARCH_PAGE_SIZE = 5
RECENT_DEFAULT = 10
RECENT_MAX = 50
USAGE_HISTORY_DAYS = 7
EXPIRING_DEFAULT_DAYS = 60
EXPIRING_LIMIT = 50

_COMMAND_NAMES = ("help", "search", "recent", "doc", "inbox", "expiring", "usage", "clear")


def parse_ddmmyyyy(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%d.%m.%Y").date()
    except ValueError:
        return None


def format_relative_days(target: date, today: date, lang: str) -> str:
    delta = (target - today).days
    if delta == 0:
        return t("rel_today", lang)
    if delta > 0:
        return t("rel_in_days", lang, n=delta)
    return t("rel_expired_days_ago", lang, n=-delta)


def _lang(update: Update) -> str:
    user = update.effective_user
    return resolve_language(user.language_code if user else None)


def _format_date(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else "—"


def _format_doc_line(doc: Document) -> str:
    parts = [f"<b>#{doc.id} {html.escape(doc.title)}</b>", _format_date(doc.created)]
    if doc.correspondent:
        parts.append(html.escape(doc.correspondent))
    return " · ".join(parts)


def _format_doc_list(docs: list[Document]) -> str:
    return "\n".join(_format_doc_line(d) for d in docs)


def _format_doc_summary(doc: Document) -> str:
    """Condensed search-result card: id/title, date, correspondent, tags."""
    line = _format_doc_line(doc)
    if doc.tags:
        line += f"\n🏷 {html.escape(', '.join(doc.tags))}"
    return line


def _format_doc_card(doc: Document, lang: str, public_url: str) -> str:
    lines = [f"<b>#{doc.id} {html.escape(doc.title)}</b>", ""]
    lines.append(f"{t('field_created', lang)}: {_format_date(doc.created)}")
    if doc.correspondent:
        lines.append(f"{t('field_correspondent', lang)}: {html.escape(doc.correspondent)}")
    if doc.document_type:
        lines.append(f"{t('field_type', lang)}: {html.escape(doc.document_type)}")
    if doc.tags:
        lines.append(f"{t('field_tags', lang)}: {html.escape(', '.join(doc.tags))}")
    for name, value in doc.custom_fields.items():
        lines.append(f"{html.escape(name)}: {html.escape(str(value))}")
    lines.append("")
    base = public_url.rstrip("/")
    lines.append(f'<a href="{base}/documents/{doc.id}/">{t("field_link", lang)}</a>')
    return "\n".join(lines)


def build_bot_commands(lang: str) -> list[BotCommand]:
    return [BotCommand(name, t(f"menu_{name}", lang)) for name in _COMMAND_NAMES]


async def register_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(build_bot_commands("en"))
    for lang in SUPPORTED_LANGUAGES:
        await bot.set_my_commands(build_bot_commands(lang), language_code=lang)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    get_deps(context).pending_input.clear(update.message.chat_id)
    await update.message.reply_text(t("help_text", _lang(update)))


async def _send_search_page(
    bot: Bot,
    chat_id: int,
    deps: Deps,
    token: str,
    query: str,
    page: int,
    extra_params: dict[str, Any],
    lang: str,
) -> None:
    try:
        docs, total = await deps.paperless.search_documents(
            query, page=page, page_size=SEARCH_PAGE_SIZE, extra_params=extra_params
        )
    except PaperlessError:
        logger.exception("search_documents failed")
        await bot.send_message(chat_id, t("generic_error", lang))
        return

    if not docs:
        await bot.send_message(chat_id, t("no_results", lang))
        return

    for doc in docs:
        await bot.send_message(
            chat_id,
            _format_doc_summary(doc),
            parse_mode=ParseMode.HTML,
            reply_markup=search_card_keyboard(doc.id, expanded=False),
        )

    total_pages = max(1, math.ceil(total / SEARCH_PAGE_SIZE))
    if total_pages > 1:
        nav = search_page_nav_keyboard(token, page, total_pages)
        await bot.send_message(
            chat_id, t("page_indicator", lang, page=page, pages=total_pages), reply_markup=nav
        )


async def _run_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    message = update.message
    if message is None:
        return
    lang = _lang(update)
    deps = get_deps(context)
    token = deps.search_store.put(SearchState(query=query))
    await _send_search_page(context.bot, message.chat_id, deps, token, query, 1, {}, lang)


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    deps = get_deps(context)
    chat_id = update.message.chat_id
    deps.pending_input.clear(chat_id)
    lang = _lang(update)
    query = " ".join(context.args) if context.args else ""
    if not query:
        deps.pending_input.set(chat_id, "search")
        await update.message.reply_text(
            t("search_prompt", lang),
            reply_markup=ForceReply(
                selective=True, input_field_placeholder=t("search_placeholder", lang)
            ),
        )
        return
    await _run_search(update, context, query)


async def search_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = update.effective_chat
    if query is None or query.data is None or chat is None:
        return
    decoded = decode_search_page(query.data)
    if decoded is None:
        await query.answer()
        return
    token, page = decoded

    deps = get_deps(context)
    state = deps.search_store.get(token)
    lang = _lang(update)
    if state is None:
        await query.answer(t("generic_error", lang), show_alert=True)
        return

    await query.answer()
    await _send_search_page(
        context.bot, chat.id, deps, token, state.query, page, state.extra_params, lang
    )


async def _render_search_card(
    query: Any, deps: Deps, doc_id: int, lang: str, *, expanded: bool
) -> None:
    try:
        doc = await deps.paperless.get_document(doc_id)
    except PaperlessError:
        logger.exception("get_document failed (search card)")
        await query.answer(t("generic_error", lang), show_alert=True)
        return

    await query.answer()
    if doc is None:
        await query.edit_message_text(t("doc_not_found", lang, id=doc_id))
        return

    if expanded:
        public_url = str(deps.settings.paperless_public_url_or_default)
        text = _format_doc_card(doc, lang, public_url)
    else:
        text = _format_doc_summary(doc)
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=search_card_keyboard(doc_id, expanded=expanded),
    )


async def search_expand_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    doc_id = decode_search_expand(query.data)
    if doc_id is None:
        await query.answer()
        return
    await _render_search_card(query, get_deps(context), doc_id, _lang(update), expanded=True)


async def search_collapse_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    doc_id = decode_search_collapse(query.data)
    if doc_id is None:
        await query.answer()
        return
    await _render_search_card(query, get_deps(context), doc_id, _lang(update), expanded=False)


async def preview_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The 👁 button on an agent reply: send the full /doc-style card as a
    new message (the agent's own reply text must stay untouched)."""
    query = update.callback_query
    chat = update.effective_chat
    if query is None or query.data is None or chat is None:
        return
    doc_id = decode_preview(query.data)
    if doc_id is None:
        await query.answer()
        return

    lang = _lang(update)
    deps = get_deps(context)
    try:
        doc = await deps.paperless.get_document(doc_id)
    except PaperlessError:
        logger.exception("get_document failed (preview)")
        await query.answer(t("generic_error", lang), show_alert=True)
        return

    await query.answer()
    if doc is None:
        await context.bot.send_message(chat.id, t("doc_not_found", lang, id=doc_id))
        return

    public_url = str(deps.settings.paperless_public_url_or_default)
    await context.bot.send_message(
        chat.id,
        _format_doc_card(doc, lang, public_url),
        parse_mode=ParseMode.HTML,
        reply_markup=doc_card_keyboard(doc.id),
    )


async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    deps = get_deps(context)
    deps.pending_input.clear(update.message.chat_id)
    lang = _lang(update)
    n = RECENT_DEFAULT
    if context.args:
        try:
            n = max(1, min(RECENT_MAX, int(context.args[0])))
        except ValueError:
            pass

    try:
        docs = await deps.paperless.recent_documents(limit=n)
    except PaperlessError:
        logger.exception("recent_documents failed")
        await update.message.reply_text(t("generic_error", lang))
        return

    if not docs:
        await update.message.reply_text(t("no_results", lang))
        return
    await update.message.reply_text(_format_doc_list(docs), parse_mode=ParseMode.HTML)


async def _run_doc(update: Update, context: ContextTypes.DEFAULT_TYPE, doc_id: int) -> None:
    message = update.message
    if message is None:
        return
    lang = _lang(update)
    deps = get_deps(context)
    try:
        doc = await deps.paperless.get_document(doc_id)
    except PaperlessError:
        logger.exception("get_document failed")
        await message.reply_text(t("generic_error", lang))
        return

    if doc is None:
        await message.reply_text(t("doc_not_found", lang, id=doc_id))
        return

    public_url = str(deps.settings.paperless_public_url_or_default)
    await message.reply_text(
        _format_doc_card(doc, lang, public_url),
        parse_mode=ParseMode.HTML,
        reply_markup=doc_card_keyboard(doc.id),
    )


async def doc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    deps = get_deps(context)
    chat_id = update.message.chat_id
    deps.pending_input.clear(chat_id)
    lang = _lang(update)
    if not context.args:
        deps.pending_input.set(chat_id, "doc")
        await update.message.reply_text(
            t("doc_prompt", lang),
            reply_markup=ForceReply(
                selective=True, input_field_placeholder=t("doc_placeholder", lang)
            ),
        )
        return
    try:
        doc_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(t("doc_usage", lang))
        return
    await _run_doc(update, context, doc_id)


async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    deps = get_deps(context)
    deps.pending_input.clear(update.message.chat_id)
    lang = _lang(update)
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
    deps = get_deps(context)
    chat_id = update.message.chat_id
    deps.pending_input.clear(chat_id)
    lang = _lang(update)
    deps.agent_memory.clear(chat_id)
    await update.message.reply_text(t("memory_cleared", lang))


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route any non-command text message to a pending prompt, or (failing
    that) to the agent (SPEC §4.5)."""
    message = update.message
    if message is None or not message.text:
        return
    lang = _lang(update)
    deps = get_deps(context)
    chat_id = message.chat_id

    pending = deps.pending_input.pop(chat_id)
    if pending == "search":
        await _run_search(update, context, message.text)
        return
    if pending == "doc":
        try:
            doc_id = int(message.text.strip())
        except ValueError:
            await message.reply_text(t("doc_usage", lang))
            return
        await _run_doc(update, context, doc_id)
        return
    if pending is not None and pending.startswith("rename:"):
        doc_id = int(pending.split(":", 1)[1])
        await handle_rename_input(update, context, doc_id, message.text)
        return

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
        result = await run_agent(
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

    if result.text:
        keyboard = agent_doc_actions_keyboard([doc_id for doc_id, _ in result.mentioned_docs])
        await message.reply_text(result.text, reply_markup=keyboard)


async def expiring_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    deps = get_deps(context)
    chat_id = update.message.chat_id
    deps.pending_input.clear(chat_id)
    lang = _lang(update)
    days = EXPIRING_DEFAULT_DAYS
    if context.args:
        try:
            days = max(1, int(context.args[0]))
        except ValueError:
            await update.message.reply_text(t("expiring_usage", lang))
            return

    today = datetime.now(ZoneInfo(deps.settings.tz)).date()
    end_date = today + timedelta(days=days)
    field_query = json.dumps(
        [deps.settings.expiry_field_name, "range", [today.isoformat(), end_date.isoformat()]]
    )

    try:
        docs = await deps.paperless.find_by_custom_field_query(field_query, limit=EXPIRING_LIMIT)
    except PaperlessError:
        logger.exception("find_by_custom_field_query (expiring) failed")
        await update.message.reply_text(t("generic_error", lang))
        return

    if not docs:
        await update.message.reply_text(t("no_results", lang))
        return

    def sort_key(doc: Document) -> date:
        raw = doc.custom_fields.get(deps.settings.expiry_field_name)
        parsed = parse_ddmmyyyy(raw) if raw else None
        return parsed or date.max

    docs.sort(key=sort_key)

    lines = []
    for doc in docs:
        raw = doc.custom_fields.get(deps.settings.expiry_field_name)
        parsed = parse_ddmmyyyy(raw) if raw else None
        rel = f" — {format_relative_days(parsed, today, lang)}" if parsed else ""
        lines.append(f"{_format_doc_line(doc)}{rel}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


def register_handlers(application: Application[Any, Any, Any, Any, Any, Any]) -> None:
    application.add_handler(CommandHandler(["start", "help"], help_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("recent", recent_command))
    application.add_handler(CommandHandler("doc", doc_command))
    application.add_handler(CommandHandler("expiring", expiring_command))
    application.add_handler(CommandHandler("usage", usage_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CallbackQueryHandler(search_page_callback, pattern=r"^sp:"))
    application.add_handler(CallbackQueryHandler(search_expand_callback, pattern=r"^xd:"))
    application.add_handler(CallbackQueryHandler(search_collapse_callback, pattern=r"^cd:"))
    application.add_handler(CallbackQueryHandler(preview_callback, pattern=r"^pv:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
