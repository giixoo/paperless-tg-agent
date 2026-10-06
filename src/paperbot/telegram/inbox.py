"""Inbox triage: list documents, mark done, and edit metadata first
(tags/correspondent/document_type/title) — SPEC §4.2 + §12 (v0.2 expands
scope beyond the original §11 "no editing metadata" exclusion; see Spec.md).

Editing is "pick from existing values only" for tags/correspondent/type (no
creating brand-new taxonomy entries from Telegram); title is free text,
routed through the shared `PendingInputStore` (see `commands.py`'s
`text_handler`, which calls `handle_rename_input` below).
"""

from __future__ import annotations

import html
import logging
import math
from typing import Any

from telegram import Bot, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from paperbot.i18n import resolve_language, t
from paperbot.paperless import Correspondent, Document, DocumentType, PaperlessError, Tag
from paperbot.telegram.deps import Deps, get_deps
from paperbot.telegram.keyboards import decode_inbox_done, encode_inbox_done

logger = logging.getLogger(__name__)

INBOX_PAGE_SIZE = 5
CLEAR_SENTINEL = 0  # correspondent/type id 0 means "clear" (real ids are >0)

_TAGS_MENU = "it"
_TAG_TOGGLE = "tt"
_CORR_MENU = "ic"
_CORR_SET = "sc"
_TYPE_MENU = "iy"
_TYPE_SET = "sy"
_AI_MENU = "ia"
_AI_TOGGLE = "ta"
_RENAME = "ir"
_BACK = "ba"
_PAGE = "ip"


def _lang(update: Update) -> str:
    user = update.effective_user
    return resolve_language(user.language_code if user else None)


def _encode(prefix: str, *parts: int) -> str:
    data = ":".join([prefix, *(str(p) for p in parts)])
    assert len(data.encode()) <= 64, "callback_data exceeds 64 bytes"
    return data


def _decode(prefix: str, data: str, n: int) -> tuple[int, ...] | None:
    parts = data.split(":")
    if len(parts) != n + 1 or parts[0] != prefix:
        return None
    try:
        return tuple(int(p) for p in parts[1:])
    except ValueError:
        return None


def _format_inbox_header(doc: Document) -> str:
    line = f"<b>#{doc.id} {html.escape(doc.title)}</b>"
    details = [html.escape(v) for v in (doc.correspondent, doc.document_type) if v]
    if details:
        line += "\n" + " · ".join(details)
    if doc.tags:
        line += f"\n🏷 {html.escape(', '.join(doc.tags))}"
    return line


def _main_keyboard(doc_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🏷 Tags", callback_data=_encode(_TAGS_MENU, doc_id)),
                InlineKeyboardButton("🏢 Correspondent", callback_data=_encode(_CORR_MENU, doc_id)),
            ],
            [
                InlineKeyboardButton("📁 Type", callback_data=_encode(_TYPE_MENU, doc_id)),
                InlineKeyboardButton("✏️ Title", callback_data=_encode(_RENAME, doc_id)),
            ],
            [InlineKeyboardButton("🤖 AI", callback_data=_encode(_AI_MENU, doc_id))],
            [InlineKeyboardButton("✅ Done", callback_data=encode_inbox_done(doc_id))],
        ]
    )


def _tags_menu_keyboard(doc_id: int, tags: list[Tag], current: set[str]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{'✅' if tag.name in current else '⬜'} {tag.name}",
                callback_data=_encode(_TAG_TOGGLE, doc_id, tag.id),
            )
        ]
        for tag in tags
    ]
    rows.append([InlineKeyboardButton("↩ Back", callback_data=_encode(_BACK, doc_id))])
    return InlineKeyboardMarkup(rows)


def _ai_menu_keyboard(
    doc_id: int, hidden_tags: list[Tag], current_ids: set[int]
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{'✅' if tag.id in current_ids else '⬜'} {tag.name}",
                callback_data=_encode(_AI_TOGGLE, doc_id, tag.id),
            )
        ]
        for tag in hidden_tags
    ]
    rows.append([InlineKeyboardButton("↩ Back", callback_data=_encode(_BACK, doc_id))])
    return InlineKeyboardMarkup(rows)


def _single_select_keyboard(
    doc_id: int,
    callback_prefix: str,
    items: list[Correspondent] | list[DocumentType],
    current: str | None,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{'●' if item.name == current else '○'} {item.name}",
                callback_data=_encode(callback_prefix, doc_id, item.id),
            )
        ]
        for item in items
    ]
    clear_data = _encode(callback_prefix, doc_id, CLEAR_SENTINEL)
    rows.append([InlineKeyboardButton("✖ Clear", callback_data=clear_data)])
    rows.append([InlineKeyboardButton("↩ Back", callback_data=_encode(_BACK, doc_id))])
    return InlineKeyboardMarkup(rows)


async def _render_card(
    query: Any, deps: Deps, doc_id: int, lang: str, keyboard: InlineKeyboardMarkup
) -> None:
    try:
        doc = await deps.paperless.get_document(doc_id)
    except PaperlessError:
        logger.exception("get_document failed (inbox card)")
        await query.answer(t("generic_error", lang), show_alert=True)
        return
    await query.answer()
    if doc is None:
        await query.edit_message_text(t("doc_not_found", lang, id=doc_id))
        return
    await query.edit_message_text(
        _format_inbox_header(doc), parse_mode=ParseMode.HTML, reply_markup=keyboard
    )


async def _render_main_card(query: Any, deps: Deps, doc_id: int, lang: str) -> None:
    await _render_card(query, deps, doc_id, lang, _main_keyboard(doc_id))


def _page_nav_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup | None:
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀", callback_data=_encode(_PAGE, page - 1)))
    if page < total_pages:
        nav.append(InlineKeyboardButton("▶", callback_data=_encode(_PAGE, page + 1)))
    return InlineKeyboardMarkup([nav]) if nav else None


async def _send_inbox_page(chat_id: int, bot: Bot, deps: Deps, lang: str, page: int) -> None:
    try:
        tag_ids = await deps.paperless.taxonomy.inbox_tag_ids()
        docs, total = (
            await deps.paperless.list_by_tag_ids(tag_ids, page=page, limit=INBOX_PAGE_SIZE)
            if tag_ids
            else ([], 0)
        )
    except PaperlessError:
        logger.exception("inbox lookup failed")
        await bot.send_message(chat_id, t("generic_error", lang))
        return

    if not docs:
        await bot.send_message(chat_id, t("no_results", lang))
        return

    for doc in docs:
        await bot.send_message(
            chat_id,
            _format_inbox_header(doc),
            parse_mode=ParseMode.HTML,
            reply_markup=_main_keyboard(doc.id),
        )

    total_pages = max(1, math.ceil(total / INBOX_PAGE_SIZE))
    if total_pages > 1:
        nav = _page_nav_keyboard(page, total_pages)
        await bot.send_message(
            chat_id, t("page_indicator", lang, page=page, pages=total_pages), reply_markup=nav
        )


async def inbox_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    deps = get_deps(context)
    deps.pending_input.clear(update.message.chat_id)
    lang = _lang(update)
    await _send_inbox_page(update.message.chat_id, context.bot, deps, lang, page=1)


async def inbox_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = update.effective_chat
    if query is None or query.data is None or chat is None:
        return
    decoded = _decode(_PAGE, query.data, 1)
    if decoded is None:
        await query.answer()
        return
    (page,) = decoded
    await query.answer()
    await _send_inbox_page(chat.id, context.bot, get_deps(context), _lang(update), page)


async def inbox_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    doc_id = decode_inbox_done(query.data)
    if doc_id is None:
        await query.answer()
        return

    lang = _lang(update)
    deps = get_deps(context)
    try:
        tag_ids = await deps.paperless.taxonomy.inbox_tag_ids()
        if tag_ids:
            await deps.paperless.bulk_modify_tags([doc_id], remove_tags=tag_ids)
    except PaperlessError:
        logger.exception("bulk_modify_tags (done) failed for #%d", doc_id)
        await query.answer(t("generic_error", lang), show_alert=True)
        return

    await query.answer(t("inbox_done", lang))
    await query.edit_message_reply_markup(reply_markup=None)


async def back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    decoded = _decode(_BACK, query.data, 1)
    if decoded is None:
        await query.answer()
        return
    (doc_id,) = decoded
    await _render_main_card(query, get_deps(context), doc_id, _lang(update))


async def tags_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    decoded = _decode(_TAGS_MENU, query.data, 1)
    if decoded is None:
        await query.answer()
        return
    (doc_id,) = decoded
    await _render_tags_menu(query, get_deps(context), doc_id, _lang(update))


async def _render_tags_menu(query: Any, deps: Deps, doc_id: int, lang: str) -> None:
    try:
        doc = await deps.paperless.get_document(doc_id)
        all_tags = await deps.paperless.taxonomy.all_tags()
    except PaperlessError:
        logger.exception("get_document/all_tags failed (inbox tags menu)")
        await query.answer(t("generic_error", lang), show_alert=True)
        return
    await query.answer()
    if doc is None:
        await query.edit_message_text(t("doc_not_found", lang, id=doc_id))
        return
    await query.edit_message_text(
        _format_inbox_header(doc),
        parse_mode=ParseMode.HTML,
        reply_markup=_tags_menu_keyboard(doc_id, all_tags, set(doc.tags)),
    )


async def tag_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    decoded = _decode(_TAG_TOGGLE, query.data, 2)
    if decoded is None:
        await query.answer()
        return
    doc_id, tag_id = decoded
    deps = get_deps(context)
    lang = _lang(update)
    try:
        tag_name = await deps.paperless.taxonomy.tag_name(tag_id)
        doc = await deps.paperless.get_document(doc_id)
        if doc is None or tag_name is None:
            await query.answer(t("generic_error", lang), show_alert=True)
            return
        if tag_name in doc.tags:
            await deps.paperless.bulk_modify_tags([doc_id], remove_tags=[tag_id])
        else:
            await deps.paperless.bulk_modify_tags([doc_id], add_tags=[tag_id])
    except PaperlessError:
        logger.exception("tag toggle failed for #%d tag=%d", doc_id, tag_id)
        await query.answer(t("generic_error", lang), show_alert=True)
        return
    await _render_tags_menu(query, deps, doc_id, lang)


async def corr_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    decoded = _decode(_CORR_MENU, query.data, 1)
    if decoded is None:
        await query.answer()
        return
    (doc_id,) = decoded
    await _render_corr_menu(query, get_deps(context), doc_id, _lang(update))


async def _render_corr_menu(query: Any, deps: Deps, doc_id: int, lang: str) -> None:
    try:
        doc = await deps.paperless.get_document(doc_id)
        correspondents = await deps.paperless.taxonomy.all_correspondents()
    except PaperlessError:
        logger.exception("get_document/all_correspondents failed")
        await query.answer(t("generic_error", lang), show_alert=True)
        return
    await query.answer()
    if doc is None:
        await query.edit_message_text(t("doc_not_found", lang, id=doc_id))
        return
    await query.edit_message_text(
        _format_inbox_header(doc),
        parse_mode=ParseMode.HTML,
        reply_markup=_single_select_keyboard(doc_id, _CORR_SET, correspondents, doc.correspondent),
    )


async def corr_set_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    decoded = _decode(_CORR_SET, query.data, 2)
    if decoded is None:
        await query.answer()
        return
    doc_id, correspondent_id = decoded
    deps = get_deps(context)
    lang = _lang(update)
    try:
        await deps.paperless.update_document(
            doc_id,
            correspondent=None if correspondent_id == CLEAR_SENTINEL else correspondent_id,
        )
    except PaperlessError:
        logger.exception("update_document (correspondent) failed for #%d", doc_id)
        await query.answer(t("generic_error", lang), show_alert=True)
        return
    await _render_main_card(query, deps, doc_id, lang)


async def type_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    decoded = _decode(_TYPE_MENU, query.data, 1)
    if decoded is None:
        await query.answer()
        return
    (doc_id,) = decoded
    await _render_type_menu(query, get_deps(context), doc_id, _lang(update))


async def _render_type_menu(query: Any, deps: Deps, doc_id: int, lang: str) -> None:
    try:
        doc = await deps.paperless.get_document(doc_id)
        doc_types = await deps.paperless.taxonomy.all_document_types()
    except PaperlessError:
        logger.exception("get_document/all_document_types failed")
        await query.answer(t("generic_error", lang), show_alert=True)
        return
    await query.answer()
    if doc is None:
        await query.edit_message_text(t("doc_not_found", lang, id=doc_id))
        return
    await query.edit_message_text(
        _format_inbox_header(doc),
        parse_mode=ParseMode.HTML,
        reply_markup=_single_select_keyboard(doc_id, _TYPE_SET, doc_types, doc.document_type),
    )


async def type_set_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    decoded = _decode(_TYPE_SET, query.data, 2)
    if decoded is None:
        await query.answer()
        return
    doc_id, type_id = decoded
    deps = get_deps(context)
    lang = _lang(update)
    try:
        await deps.paperless.update_document(
            doc_id, document_type=None if type_id == CLEAR_SENTINEL else type_id
        )
    except PaperlessError:
        logger.exception("update_document (document_type) failed for #%d", doc_id)
        await query.answer(t("generic_error", lang), show_alert=True)
        return
    await _render_main_card(query, deps, doc_id, lang)


async def ai_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    decoded = _decode(_AI_MENU, query.data, 1)
    if decoded is None:
        await query.answer()
        return
    (doc_id,) = decoded
    await _render_ai_menu(query, get_deps(context), doc_id, _lang(update))


async def _render_ai_menu(query: Any, deps: Deps, doc_id: int, lang: str) -> None:
    try:
        doc = await deps.paperless.get_document(doc_id)
        hidden_tags = await deps.paperless.taxonomy.hidden_tags()
    except PaperlessError:
        logger.exception("get_document/hidden_tags failed (inbox AI menu)")
        await query.answer(t("generic_error", lang), show_alert=True)
        return
    await query.answer()
    if doc is None:
        await query.edit_message_text(t("doc_not_found", lang, id=doc_id))
        return
    await query.edit_message_text(
        _format_inbox_header(doc),
        parse_mode=ParseMode.HTML,
        reply_markup=_ai_menu_keyboard(doc_id, hidden_tags, set(doc.tag_ids)),
    )


async def ai_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    decoded = _decode(_AI_TOGGLE, query.data, 2)
    if decoded is None:
        await query.answer()
        return
    doc_id, tag_id = decoded
    deps = get_deps(context)
    lang = _lang(update)
    try:
        doc = await deps.paperless.get_document(doc_id)
        if doc is None:
            await query.answer(t("generic_error", lang), show_alert=True)
            return
        if tag_id in doc.tag_ids:
            await deps.paperless.bulk_modify_tags([doc_id], remove_tags=[tag_id])
        else:
            await deps.paperless.bulk_modify_tags([doc_id], add_tags=[tag_id])
    except PaperlessError:
        logger.exception("AI tag toggle failed for #%d tag=%d", doc_id, tag_id)
        await query.answer(t("generic_error", lang), show_alert=True)
        return
    await _render_ai_menu(query, deps, doc_id, lang)


async def rename_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = update.effective_chat
    if query is None or query.data is None or chat is None:
        return
    decoded = _decode(_RENAME, query.data, 1)
    if decoded is None:
        await query.answer()
        return
    (doc_id,) = decoded
    deps = get_deps(context)
    lang = _lang(update)
    deps.pending_input.set(chat.id, f"rename:{doc_id}")
    await query.answer()
    await context.bot.send_message(
        chat.id,
        t("inbox_rename_prompt", lang, id=doc_id),
        reply_markup=ForceReply(
            selective=True, input_field_placeholder=t("inbox_rename_placeholder", lang)
        ),
    )


async def handle_rename_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, doc_id: int, new_title: str
) -> None:
    """Continuation of `rename_prompt_callback`, invoked from
    `commands.py`'s `text_handler` once the user replies with the new
    title."""
    message = update.message
    if message is None:
        return
    deps = get_deps(context)
    lang = _lang(update)
    try:
        await deps.paperless.update_document(doc_id, title=new_title)
        doc = await deps.paperless.get_document(doc_id)
    except PaperlessError:
        logger.exception("update_document (title) failed for #%d", doc_id)
        await message.reply_text(t("generic_error", lang))
        return

    await message.reply_text(t("inbox_renamed", lang))
    if doc is not None:
        await message.reply_text(
            _format_inbox_header(doc),
            parse_mode=ParseMode.HTML,
            reply_markup=_main_keyboard(doc_id),
        )


def register_inbox_handlers(application: Application[Any, Any, Any, Any, Any, Any]) -> None:
    application.add_handler(CommandHandler("inbox", inbox_command))
    application.add_handler(CallbackQueryHandler(inbox_done_callback, pattern=r"^ib:"))
    application.add_handler(CallbackQueryHandler(tags_menu_callback, pattern=rf"^{_TAGS_MENU}:"))
    application.add_handler(CallbackQueryHandler(tag_toggle_callback, pattern=rf"^{_TAG_TOGGLE}:"))
    application.add_handler(CallbackQueryHandler(corr_menu_callback, pattern=rf"^{_CORR_MENU}:"))
    application.add_handler(CallbackQueryHandler(corr_set_callback, pattern=rf"^{_CORR_SET}:"))
    application.add_handler(CallbackQueryHandler(type_menu_callback, pattern=rf"^{_TYPE_MENU}:"))
    application.add_handler(CallbackQueryHandler(type_set_callback, pattern=rf"^{_TYPE_SET}:"))
    application.add_handler(CallbackQueryHandler(ai_menu_callback, pattern=rf"^{_AI_MENU}:"))
    application.add_handler(CallbackQueryHandler(ai_toggle_callback, pattern=rf"^{_AI_TOGGLE}:"))
    application.add_handler(CallbackQueryHandler(rename_prompt_callback, pattern=rf"^{_RENAME}:"))
    application.add_handler(CallbackQueryHandler(back_callback, pattern=rf"^{_BACK}:"))
    application.add_handler(CallbackQueryHandler(inbox_page_callback, pattern=rf"^{_PAGE}:"))
