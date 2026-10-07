"""`/dups` near-duplicate review commands and callbacks (SPEC-dups §4-5).

Button captions follow the existing `inbox.py` convention: short, untranslated
English + emoji, not routed through `i18n.py` — only message *bodies* are
translated, matching the rest of this module's SPEC-dups counterpart.
"""

from __future__ import annotations

import html
import logging
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from paperbot.config import Settings
from paperbot.dups.db import DupPair
from paperbot.dups.diff import DiffItem, apply_selected_items, build_diff, non_conflict_indices
from paperbot.dups.scan import scan_once
from paperbot.i18n import resolve_language, t
from paperbot.paperless import Document, DocumentMetadata, PaperlessError
from paperbot.telegram.deps import Deps, get_deps
from paperbot.telegram.files import send_document_to_chat

logger = logging.getLogger(__name__)

_PREFIX = "d"
_DL_A = "dA"
_DL_B = "dB"
_KEEP_A = "kA"
_KEEP_B = "kB"
_NOT_DUP = "nd"
_SKIP = "sk"
_TOGGLE = "ti"
_COPY_ALL = "ca"
_COPY_SEL = "cs"
_NO_COPY = "nc"
_CANCEL = "cc"

# No per-user language stored for the background scan job's broadcast
# message, same reasoning as `reminders.py`'s REMINDER_LANG.
_NOTIFY_LANG = "en"


def _lang(update: Update) -> str:
    user = update.effective_user
    return resolve_language(user.language_code if user else None)


def _encode(action: str, *parts: int) -> str:
    data = ":".join([_PREFIX, action, *(str(p) for p in parts)])
    assert len(data.encode()) <= 64, "callback_data exceeds 64 bytes"
    return data


def _decode(action: str, data: str, n: int) -> tuple[int, ...] | None:
    parts = data.split(":")
    if len(parts) != 2 + n or parts[0] != _PREFIX or parts[1] != action:
        return None
    try:
        return tuple(int(p) for p in parts[2:])
    except ValueError:
        return None


# --- formatting --------------------------------------------------------------


def _format_date(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else "—"


def _format_size(num_bytes: int | None) -> str:
    if not num_bytes:
        return "—"
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def _recommend(
    doc_a: Document,
    doc_b: Document,
    meta_a: DocumentMetadata | None,
    meta_b: DocumentMetadata | None,
) -> str:
    """Tie-break rule for the ⭐ hint (SPEC-dups §5.1): more OCR characters
    → larger original file → newer. A hint only; the owner decides."""
    len_a, len_b = len(doc_a.content or ""), len(doc_b.content or "")
    if len_a != len_b:
        return "A" if len_a > len_b else "B"
    size_a = (meta_a.original_size if meta_a else None) or 0
    size_b = (meta_b.original_size if meta_b else None) or 0
    if size_a != size_b:
        return "A" if size_a > size_b else "B"
    created_a = doc_a.created or date.min
    created_b = doc_b.created or date.min
    if created_a != created_b:
        return "A" if created_a > created_b else "B"
    return "A"


def _format_doc_block(
    doc: Document, meta: DocumentMetadata | None, label: str, starred: bool, public_url: str
) -> str:
    star = " ⭐" if starred else ""
    header = f"<b>{label}{star}: #{doc.id} {html.escape(doc.title)}</b>"
    corr = f" · {html.escape(doc.correspondent)}" if doc.correspondent else ""
    pages = doc.page_count if doc.page_count is not None else "—"
    size = _format_size(meta.original_size if meta else None)
    mime = html.escape(doc.mime_type) if doc.mime_type else "—"
    base = public_url.rstrip("/")
    link = f'<a href="{base}/documents/{doc.id}/">{base}/documents/{doc.id}/</a>'
    return "\n".join(
        [
            header,
            f"{_format_date(doc.created)}{corr}",
            f"{pages} pages · {size} · {mime} · OCR {len(doc.content or '')} chars",
            link,
        ]
    )


def _format_pair_card(
    doc_a: Document,
    doc_b: Document,
    meta_a: DocumentMetadata | None,
    meta_b: DocumentMetadata | None,
    pair: DupPair,
    lang: str,
    public_url: str,
) -> str:
    star = _recommend(doc_a, doc_b, meta_a, meta_b)
    blocks = [
        _format_doc_block(doc_a, meta_a, "A", star == "A", public_url),
        "",
        _format_doc_block(doc_b, meta_b, "B", star == "B", public_url),
        "",
        t("dups_similarity", lang, text=round(pair.text_sim * 100), num=round(pair.num_sim * 100)),
    ]
    warnings = []
    if doc_a.page_count != doc_b.page_count:
        warnings.append(t("dups_warn_pages", lang))
    if doc_a.created != doc_b.created:
        warnings.append(t("dups_warn_dates", lang))
    if pair.num_sim < 0.9:
        warnings.append(t("dups_warn_numbers", lang))
    if warnings:
        blocks.append("⚠ " + "; ".join(warnings))
    return "\n".join(blocks)


def _format_selection(items: list[DiffItem], checked: set[int], loser_id: int, lang: str) -> str:
    lines = [t("dups_selection_header", lang, loser=loser_id)]
    for idx, item in enumerate(items):
        box = "☑" if idx in checked else "☐"
        warn = "⚠ " if item.is_conflict else ""
        lines.append(f"{box} {warn}{html.escape(item.label)}")
    return "\n".join(lines)


def _pair_keyboard(pair_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📄 A", callback_data=_encode(_DL_A, pair_id)),
                InlineKeyboardButton("📄 B", callback_data=_encode(_DL_B, pair_id)),
            ],
            [
                InlineKeyboardButton("Keep A", callback_data=_encode(_KEEP_A, pair_id)),
                InlineKeyboardButton("Keep B", callback_data=_encode(_KEEP_B, pair_id)),
            ],
            [InlineKeyboardButton("Not duplicates", callback_data=_encode(_NOT_DUP, pair_id))],
            [InlineKeyboardButton("Skip", callback_data=_encode(_SKIP, pair_id))],
        ]
    )


def _selection_keyboard(
    pair_id: int, items: list[DiffItem], checked: set[int]
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{'☑' if idx in checked else '☐'} {item.label[:40]}",
                callback_data=_encode(_TOGGLE, pair_id, idx),
            )
        ]
        for idx, item in enumerate(items)
    ]
    rows.append(
        [
            InlineKeyboardButton("✅ Copy all", callback_data=_encode(_COPY_ALL, pair_id)),
            InlineKeyboardButton(
                f"Copy selected ({len(checked)})", callback_data=_encode(_COPY_SEL, pair_id)
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("Skip copy", callback_data=_encode(_NO_COPY, pair_id)),
            InlineKeyboardButton("Cancel", callback_data=_encode(_CANCEL, pair_id)),
        ]
    )
    return InlineKeyboardMarkup(rows)


# --- pair card flow ------------------------------------------------------------


async def _show_next_pair(chat_id: int, bot: Any, deps: Deps, lang: str) -> None:
    if deps.dups_store is None:
        await bot.send_message(chat_id, t("generic_error", lang))
        return
    skip_ids = deps.dups_skip_store.get(chat_id)
    pair = await deps.dups_store.next_open_pair(skip_ids)
    if pair is None:
        await bot.send_message(chat_id, t("dups_no_open_pairs", lang))
        return
    await _render_pair_card(chat_id, bot, deps, lang, pair)


async def _render_pair_card(chat_id: int, bot: Any, deps: Deps, lang: str, pair: DupPair) -> None:
    assert deps.dups_store is not None
    try:
        doc_a = await deps.paperless.get_document(pair.a_id)
        doc_b = await deps.paperless.get_document(pair.b_id)
        meta_a = await deps.paperless.get_document_metadata(pair.a_id) if doc_a else None
        meta_b = await deps.paperless.get_document_metadata(pair.b_id) if doc_b else None
    except PaperlessError:
        logger.exception("dups: failed to load pair #%d", pair.id)
        await bot.send_message(chat_id, t("generic_error", lang))
        return

    if doc_a is None or doc_b is None:
        # One side vanished (e.g. trashed outside the bot) - this pair can
        # never be resolved through the review flow, so drop it and move on.
        await deps.dups_store.mark_not_dup(pair.id)
        await _show_next_pair(chat_id, bot, deps, lang)
        return

    public_url = str(deps.settings.paperless_public_url_or_default)
    text = _format_pair_card(doc_a, doc_b, meta_a, meta_b, pair, lang, public_url)
    await bot.send_message(
        chat_id,
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=_pair_keyboard(pair.id),
        disable_web_page_preview=True,
    )


async def dups_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return
    deps = get_deps(context)
    deps.pending_input.clear(message.chat_id)
    lang = _lang(update)

    sub = context.args[0].lower() if context.args else None
    if sub == "scan":
        await _run_scan_and_report(message.chat_id, context.bot, deps, lang)
        return
    if sub == "stats":
        await _send_stats(message.chat_id, context.bot, deps, lang)
        return
    if sub == "undo":
        await _run_undo(message.chat_id, context.bot, deps, lang)
        return

    deps.dups_skip_store.clear(message.chat_id)
    await _show_next_pair(message.chat_id, context.bot, deps, lang)


async def _run_scan_and_report(chat_id: int, bot: Any, deps: Deps, lang: str) -> None:
    if deps.dups_store is None:
        await bot.send_message(chat_id, t("generic_error", lang))
        return
    new_count = await scan_once(deps.paperless, deps.dups_store, deps.settings)
    await deps.dups_store.set_last_scan_at(datetime.now(UTC).isoformat())
    await bot.send_message(chat_id, t("dups_scan_result", lang, n=new_count))


async def _send_stats(chat_id: int, bot: Any, deps: Deps, lang: str) -> None:
    if deps.dups_store is None:
        await bot.send_message(chat_id, t("generic_error", lang))
        return
    stats = await deps.dups_store.stats()
    last_scan = stats.last_scan_at or t("dups_stats_never", lang)
    await bot.send_message(
        chat_id,
        t(
            "dups_stats",
            lang,
            open=stats.open_count,
            resolved=stats.resolved_count,
            not_dup=stats.not_dup_count,
            last_scan=last_scan,
        ),
    )


async def _run_undo(chat_id: int, bot: Any, deps: Deps, lang: str) -> None:
    if deps.dups_store is None:
        await bot.send_message(chat_id, t("generic_error", lang))
        return
    action = await deps.dups_store.last_action()
    if action is None:
        await bot.send_message(chat_id, t("dups_undo_none", lang))
        return
    try:
        restored = await deps.paperless.restore_from_trash(action.loser_id)
    except PaperlessError:
        logger.exception("dups undo: restore_from_trash failed for #%d", action.loser_id)
        await bot.send_message(chat_id, t("generic_error", lang))
        return
    if not restored:
        await bot.send_message(chat_id, t("dups_undo_failed", lang, id=action.loser_id))
        return
    items_text = ", ".join(action.applied) if action.applied else "—"
    await bot.send_message(
        chat_id,
        t(
            "dups_undo_restored",
            lang,
            loser=action.loser_id,
            survivor=action.survivor_id,
            items=items_text,
        ),
    )


# --- pair card callbacks -------------------------------------------------------


async def download_a_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _download_callback(update, context, _DL_A, side="A")


async def download_b_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _download_callback(update, context, _DL_B, side="B")


async def _download_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, *, side: str
) -> None:
    query = update.callback_query
    chat = update.effective_chat
    if query is None or query.data is None or chat is None:
        return
    decoded = _decode(action, query.data, 1)
    if decoded is None:
        await query.answer()
        return
    (pair_id,) = decoded
    deps = get_deps(context)
    if deps.dups_store is None:
        await query.answer()
        return
    pair = await deps.dups_store.get_pair(pair_id)
    await query.answer()
    if pair is None:
        return
    doc_id = pair.a_id if side == "A" else pair.b_id
    public_url = str(deps.settings.paperless_public_url_or_default)
    await send_document_to_chat(
        context.bot, chat.id, deps.paperless, public_url, doc_id, _lang(update)
    )


async def not_dup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = update.effective_chat
    if query is None or query.data is None or chat is None:
        return
    decoded = _decode(_NOT_DUP, query.data, 1)
    if decoded is None:
        await query.answer()
        return
    (pair_id,) = decoded
    deps = get_deps(context)
    if deps.dups_store is not None:
        await deps.dups_store.mark_not_dup(pair_id)
    await query.answer()
    await _show_next_pair(chat.id, context.bot, deps, _lang(update))


async def skip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = update.effective_chat
    if query is None or query.data is None or chat is None:
        return
    decoded = _decode(_SKIP, query.data, 1)
    if decoded is None:
        await query.answer()
        return
    (pair_id,) = decoded
    deps = get_deps(context)
    deps.dups_skip_store.add(chat.id, pair_id)
    await query.answer()
    await _show_next_pair(chat.id, context.bot, deps, _lang(update))


async def _keep_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, *, side: str) -> None:
    query = update.callback_query
    chat = update.effective_chat
    if query is None or query.data is None or chat is None:
        return
    action = _KEEP_A if side == "A" else _KEEP_B
    decoded = _decode(action, query.data, 1)
    if decoded is None:
        await query.answer()
        return
    (pair_id,) = decoded
    deps = get_deps(context)
    lang = _lang(update)
    if deps.dups_store is None:
        await query.answer()
        return
    pair = await deps.dups_store.get_pair(pair_id)
    await query.answer()
    if pair is None:
        return

    survivor_id, loser_id = (pair.a_id, pair.b_id) if side == "A" else (pair.b_id, pair.a_id)
    try:
        survivor = await deps.paperless.get_document(survivor_id)
        loser = await deps.paperless.get_document(loser_id)
    except PaperlessError:
        logger.exception("dups: failed to load survivor/loser for pair #%d", pair_id)
        await context.bot.send_message(chat.id, t("generic_error", lang))
        return
    if survivor is None or loser is None:
        await context.bot.send_message(chat.id, t("generic_error", lang))
        return

    items = await build_diff(survivor, loser, deps.paperless.taxonomy)
    if not items:
        await _finalize(chat.id, context.bot, deps, lang, pair, survivor, loser, items, set())
        return

    await deps.dups_store.save_session(
        pair.id, survivor_id, loser_id, [it.to_dict() for it in items], []
    )
    await context.bot.send_message(
        chat.id,
        _format_selection(items, set(), loser_id, lang),
        parse_mode=ParseMode.HTML,
        reply_markup=_selection_keyboard(pair.id, items, set()),
    )


async def keep_a_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _keep_callback(update, context, side="A")


async def keep_b_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _keep_callback(update, context, side="B")


# --- selection screen callbacks -------------------------------------------------


async def toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    decoded = _decode(_TOGGLE, query.data, 2)
    if decoded is None:
        await query.answer()
        return
    pair_id, idx = decoded
    deps = get_deps(context)
    if deps.dups_store is None:
        await query.answer()
        return
    session = await deps.dups_store.get_session(pair_id)
    await query.answer()
    if session is None:
        return

    checked = set(session.checked)
    if idx in checked:
        checked.discard(idx)
    else:
        checked.add(idx)
    await deps.dups_store.update_session_checked(pair_id, sorted(checked))

    items = [DiffItem.from_dict(d) for d in session.items]
    await query.edit_message_text(
        _format_selection(items, checked, session.loser_id, _lang(update)),
        parse_mode=ParseMode.HTML,
        reply_markup=_selection_keyboard(pair_id, items, checked),
    )


async def copy_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _copy_callback(update, context, _COPY_ALL, selected=None)


async def no_copy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _copy_callback(update, context, _NO_COPY, selected=set())


async def copy_selected_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    decoded = _decode(_COPY_SEL, query.data, 1)
    if decoded is None:
        await query.answer()
        return
    (pair_id,) = decoded
    deps = get_deps(context)
    if deps.dups_store is None:
        await query.answer()
        return
    session = await deps.dups_store.get_session(pair_id)
    if session is None:
        await query.answer()
        return
    await _copy_callback(update, context, _COPY_SEL, selected=set(session.checked))


async def _copy_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    *,
    selected: set[int] | None,
) -> None:
    query = update.callback_query
    chat = update.effective_chat
    if query is None or query.data is None or chat is None:
        return
    decoded = _decode(action, query.data, 1)
    if decoded is None:
        await query.answer()
        return
    (pair_id,) = decoded
    deps = get_deps(context)
    lang = _lang(update)
    if deps.dups_store is None:
        await query.answer()
        return

    session = await deps.dups_store.get_session(pair_id)
    pair = await deps.dups_store.get_pair(pair_id)
    await query.answer()
    if session is None or pair is None:
        return

    items = [DiffItem.from_dict(d) for d in session.items]
    chosen = selected if selected is not None else non_conflict_indices(items)

    try:
        survivor = await deps.paperless.get_document(session.survivor_id)
        loser = await deps.paperless.get_document(session.loser_id)
    except PaperlessError:
        logger.exception("dups: failed to re-fetch survivor/loser for pair #%d", pair_id)
        await query.edit_message_text(t("generic_error", lang))
        return
    if survivor is None or loser is None:
        await query.edit_message_text(t("generic_error", lang))
        return

    await _finalize(
        chat.id, context.bot, deps, lang, pair, survivor, loser, items, chosen, edit_query=query
    )


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    decoded = _decode(_CANCEL, query.data, 1)
    if decoded is None:
        await query.answer()
        return
    (pair_id,) = decoded
    deps = get_deps(context)
    if deps.dups_store is not None:
        await deps.dups_store.delete_session(pair_id)
    await query.answer()
    await query.edit_message_text(t("dups_cancelled", _lang(update)))


async def _finalize(
    chat_id: int,
    bot: Any,
    deps: Deps,
    lang: str,
    pair: DupPair,
    survivor: Document,
    loser: Document,
    items: list[DiffItem],
    selected: set[int],
    *,
    edit_query: Any = None,
) -> None:
    """SPEC-dups §5.4 apply order: metadata first, trash only on success."""
    assert deps.dups_store is not None

    async def _report(text: str) -> None:
        if edit_query is not None:
            await edit_query.edit_message_text(text)
        else:
            await bot.send_message(chat_id, text)

    try:
        applied = await apply_selected_items(deps.paperless, survivor, loser.id, items, selected)
    except PaperlessError:
        logger.exception("dups: applying metadata failed for pair #%d", pair.id)
        await _report(t("dups_apply_failed", lang))
        return

    try:
        await deps.paperless.trash_document(loser.id)
    except PaperlessError:
        logger.exception("dups: trashing loser #%d failed for pair #%d", loser.id, pair.id)
        await _report(t("dups_apply_failed", lang))
        return

    await deps.dups_store.resolve_pair(pair.id, loser.id)
    await deps.dups_store.delete_session(pair.id)
    await deps.dups_store.record_action(pair.id, survivor.id, loser.id, applied)

    if applied:
        text = t("dups_kept", lang, survivor=survivor.id, loser=loser.id, items=", ".join(applied))
    else:
        text = t("dups_kept_nothing", lang, survivor=survivor.id, loser=loser.id)
    await _report(text)


# --- scheduling / registration --------------------------------------------------


async def scheduled_scan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs at `DUPS_SCAN_TIME` (default 03:30, shortly after Paperless's own
    nightly export). Any new pairs found are *not* notified immediately —
    that would land in the middle of the night — but accumulated for
    `scheduled_notify_job` to send at a waking hour instead."""
    deps = get_deps(context)
    settings = deps.settings
    if deps.dups_store is None:
        return
    new_count = await scan_once(deps.paperless, deps.dups_store, settings)
    await deps.dups_store.set_last_scan_at(datetime.now(UTC).isoformat())
    if settings.dups_notify:
        await deps.dups_store.add_pending_notify(new_count)


async def scheduled_notify_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs at `DUPS_NOTIFY_TIME` (default 09:00): sends one message per
    allowed user for any new pairs accumulated since the last notification,
    then resets the counter."""
    deps = get_deps(context)
    if deps.dups_store is None:
        return
    pending = await deps.dups_store.pop_pending_notify()
    if pending <= 0:
        return
    for user_id in deps.settings.telegram_allowed_users:
        try:
            await context.bot.send_message(
                user_id, t("dups_notify_new_pairs", _NOTIFY_LANG, n=pending)
            )
        except Exception:
            logger.exception("failed to send dups notification to user %s", user_id)


def register_dups_scan_job(
    application: Application[Any, Any, Any, Any, Any, Any], settings: Settings
) -> None:
    if not settings.dups_enabled:
        logger.info("Near-duplicate scan disabled (DUPS_ENABLED=false)")
        return
    job_queue = application.job_queue
    if job_queue is None:
        logger.warning("No JobQueue available; near-duplicate scan not scheduled")
        return
    scan_hour, scan_minute = (int(p) for p in settings.dups_scan_time.split(":", 1))
    job_queue.run_daily(
        scheduled_scan_job,
        time=time(hour=scan_hour, minute=scan_minute, tzinfo=ZoneInfo(settings.tz)),
        name="dups_scan",
    )
    logger.info("Scheduled near-duplicate scan at %s %s", settings.dups_scan_time, settings.tz)

    if not settings.dups_notify:
        return
    notify_hour, notify_minute = (int(p) for p in settings.dups_notify_time.split(":", 1))
    job_queue.run_daily(
        scheduled_notify_job,
        time=time(hour=notify_hour, minute=notify_minute, tzinfo=ZoneInfo(settings.tz)),
        name="dups_notify",
    )
    logger.info(
        "Scheduled near-duplicate notification at %s %s", settings.dups_notify_time, settings.tz
    )


def register_dups_handlers(application: Application[Any, Any, Any, Any, Any, Any]) -> None:
    application.add_handler(CommandHandler("dups", dups_command))
    application.add_handler(
        CallbackQueryHandler(download_a_callback, pattern=rf"^{_PREFIX}:{_DL_A}:")
    )
    application.add_handler(
        CallbackQueryHandler(download_b_callback, pattern=rf"^{_PREFIX}:{_DL_B}:")
    )
    application.add_handler(
        CallbackQueryHandler(keep_a_callback, pattern=rf"^{_PREFIX}:{_KEEP_A}:")
    )
    application.add_handler(
        CallbackQueryHandler(keep_b_callback, pattern=rf"^{_PREFIX}:{_KEEP_B}:")
    )
    application.add_handler(
        CallbackQueryHandler(not_dup_callback, pattern=rf"^{_PREFIX}:{_NOT_DUP}:")
    )
    application.add_handler(CallbackQueryHandler(skip_callback, pattern=rf"^{_PREFIX}:{_SKIP}:"))
    application.add_handler(
        CallbackQueryHandler(toggle_callback, pattern=rf"^{_PREFIX}:{_TOGGLE}:")
    )
    application.add_handler(
        CallbackQueryHandler(copy_all_callback, pattern=rf"^{_PREFIX}:{_COPY_ALL}:")
    )
    application.add_handler(
        CallbackQueryHandler(copy_selected_callback, pattern=rf"^{_PREFIX}:{_COPY_SEL}:")
    )
    application.add_handler(
        CallbackQueryHandler(no_copy_callback, pattern=rf"^{_PREFIX}:{_NO_COPY}:")
    )
    application.add_handler(
        CallbackQueryHandler(cancel_callback, pattern=rf"^{_PREFIX}:{_CANCEL}:")
    )
