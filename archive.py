"""Emergency Resource Archive (نسخة وصول احتياطية) for MEDBOT.

A standalone Telegram channel mirrors every registered resource so students can
still reach the material if MEDBOT itself is down. It is a *disaster-recovery
access layer*, never the source of truth:

    MEDBOT Registry (source of truth)
        -> resource created
        -> archive sync
        -> Telegram archive channel

Design rules held here:

* The channel is configured ONLY through the environment
  (``MEDBOT_ARCHIVE_CHANNEL`` / ``ARCHIVE_CHANNEL_ID``); nothing is hardcoded
  and no channel id is assumed. When it is unset the feature is simply off.
* The bot is expected to be an *administrator* of the channel, so the module
  posts with normal bot permissions and never asks for extra rights.
* Publication is idempotent: the dedupe key is the resource's own content
  identity (title + file_type + file_id), tracked in ``archive_sync``. A restart
  or a retry can therefore never create a second post.
* A failed publication NEVER fails resource creation in MEDBOT; it is recorded
  as ``failed`` and can be retried through the admin resync.
* Deleting a resource in MEDBOT never deletes the archived copy.
* The post reflects MEDBOT's real hierarchy: its path is read from the registry
  (``database.get_resource_snapshot``), never invented.

Isolated like the other admin modules: own handler set, ``can_archive`` gate
(owner always passes), audit entries on every admin action. It does not import
from or write to any other subsystem's tables.
"""

import asyncio
import hashlib
import logging
import os
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, ContextTypes

import audit
import database

logger = logging.getLogger(__name__)

# Environment variables that may carry the archive channel. The operator sets
# one of these next to the other secrets in the hosting platform.
ARCHIVE_CHANNEL_ENV_VARS = ("MEDBOT_ARCHIVE_CHANNEL", "ARCHIVE_CHANNEL_ID")

# Cap a single resync pass so one admin tap cannot stall the polling loop.
MAX_RESYNC_PER_RUN = 200

# Telegram caption limit is 1024 chars; keep the header short.
MAX_CAPTION = 1000

ARCHIVE_HEADER = "🗄 <b>MEDBOT Emergency Resource Archive</b>"

# Per-fingerprint locks, so two overlapping events for the same resource
# serialize instead of both posting.
_locks: dict = {}


def esc(value) -> str:
    import html

    return html.escape(str(value if value is not None else ""))


def btn(text, callback_data):
    return InlineKeyboardButton(text, callback_data=callback_data)


# ------------------------------------------------------------
# Configuration (environment only — never hardcoded)
# ------------------------------------------------------------
def resolve_channel():
    """The configured archive channel id/username, or "" when unset.

    Accepts either a numeric id (``-1001234567890``) or an ``@username``. The
    value is read from the environment on every call so the hosting platform can
    set it without a code change.
    """
    for name in ARCHIVE_CHANNEL_ENV_VARS:
        raw = (os.getenv(name) or "").strip()
        if raw:
            return raw
    return ""


def is_configured() -> bool:
    return bool(resolve_channel())


def _int_or_none(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _channel_for_send():
    """Channel value in the form Telegram's send_* methods want.

    A numeric id is sent as an int; an ``@username`` stays a string.
    """
    raw = resolve_channel()
    if not raw:
        return None
    if raw.startswith("@"):
        return raw
    numeric = _int_or_none(raw)
    return numeric if numeric is not None else raw


# ------------------------------------------------------------
# Fingerprint / caption (identity of a resource, never invented)
# ------------------------------------------------------------
def content_fingerprint(snapshot) -> str:
    """Stable dedupe key for a resource, independent of folder location.

    Built from the resource's own registered fields (title + file_type +
    file_id). Moving or renaming its folder does not change the key, so the
    archive can never grow a second copy of the same material.
    """
    _cid, _folder_id, title, file_id, file_type, _path = snapshot
    raw = "|".join(
        (
            re.sub(r"\s+", " ", str(title or "")).strip().lower(),
            str(file_type or "").strip().lower(),
            str(file_id or "").strip(),
        )
    )
    return "content:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def folder_fingerprint(folder_id) -> str:
    return f"folder:{folder_id}"


def build_caption(snapshot) -> str:
    """Human-readable, searchable post text for a resource.

    Names the resource and its real registered path so the post can be found by
    hand in Telegram (Histology / Microbiology / عملي / lecture title ...).
    """
    _cid, _folder_id, title, _file_id, file_type, path = snapshot

    lines = [
        ARCHIVE_HEADER,
        "",
        f"📄 <b>{esc(title)}</b>",
        f"🧭 <b>المسار:</b> {esc(path)}",
    ]

    kind = str(file_type or "").strip()
    if kind:
        lines.append(f"🏷 <b>النوع:</b> <code>{esc(kind)}</code>")

    text = "\n".join(lines)
    return text[:MAX_CAPTION]


def build_folder_caption(folder_name, path) -> str:
    lines = [
        ARCHIVE_HEADER,
        "",
        f"📁 <b>{esc(folder_name)}</b>",
        f"🧭 <b>المسار:</b> {esc(path)}",
    ]
    return "\n".join(lines)[:MAX_CAPTION]


# ------------------------------------------------------------
# Telegram dispatch (bot acts as a channel administrator)
# ------------------------------------------------------------
async def _send_resource_post(bot, channel, snapshot):
    """Send the resource itself, as the media type it was registered with."""
    _cid, _folder_id, _title, file_id, file_type, _path = snapshot
    caption = build_caption(snapshot)
    ft = str(file_type or "").lower()

    if ft in ("photo", "image", "jpg", "jpeg", "png", "webp"):
        return await bot.send_photo(
            chat_id=channel,
            photo=file_id,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
    if ft in ("audio", "mp3", "m4a", "wav", "voice"):
        return await bot.send_audio(
            chat_id=channel,
            audio=file_id,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
    if ft in ("video", "mp4", "mkv", "mov"):
        return await bot.send_video(
            chat_id=channel,
            video=file_id,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
    return await bot.send_document(
        chat_id=channel,
        document=file_id,
        caption=caption,
        parse_mode=ParseMode.HTML,
    )


async def publish_folder_header(bot, folder, path):
    """Post a section header once, so the channel mirrors the hierarchy.

    ``folder`` is a database folder row (id, parent_id, name, ...). A header is
    only ever posted once per folder (tracked in ``archive_sync``).
    """
    if not is_configured() or not folder:
        return None

    folder_id = folder[0]
    name = folder[2] if len(folder) > 2 else folder[1]
    fingerprint = folder_fingerprint(folder_id)

    try:
        existing = await database.get_archive_sync(fingerprint)
    except Exception:
        existing = None

    if existing and existing[7] == "published":
        return None

    await database.upsert_archive_pending(
        fingerprint,
        folder_id=folder_id,
        content_id=None,
        object_type="folder",
    )

    channel = _channel_for_send()
    try:
        sent = await bot.send_message(
            chat_id=channel,
            text=build_folder_caption(name, path),
            parse_mode=ParseMode.HTML,
        )
        await database.mark_archive_published(
            fingerprint, channel, getattr(sent, "message_id", None)
        )
        return sent
    except Exception as exc:
        logger.warning("archive: folder header failed for %s: %s", folder_id, exc)
        await database.mark_archive_failed(fingerprint, exc)
        return None


# ------------------------------------------------------------
# Publication (idempotent, non-fatal)
# ------------------------------------------------------------
def _lock_for(fingerprint: str) -> asyncio.Lock:
    lock = _locks.get(fingerprint)
    if lock is None:
        lock = asyncio.Lock()
        _locks[fingerprint] = lock
    return lock


async def publish_resource(bot, content_id, with_header: bool = False):
    """Mirror one registered resource to the archive channel.

    Returns a dict describing the outcome:
        {"status": "published"|"skipped"|"failed", "fingerprint": str,
         "message_id": int|None, "error": str|None}

    Contract: this function NEVER raises and NEVER blocks or fails the MEDBOT
    resource it mirrors. An already-published resource is left untouched, so a
    restart or retry cannot create a duplicate.
    """
    result = {
        "status": "skipped",
        "fingerprint": None,
        "message_id": None,
        "error": None,
    }

    if not is_configured():
        return result

    snapshot = None
    try:
        snapshot = await database.get_resource_snapshot(content_id)
    except Exception:
        logger.exception("archive: snapshot failed for content id=%s", content_id)

    if not snapshot:
        return result

    fingerprint = content_fingerprint(snapshot)
    result["fingerprint"] = fingerprint

    lock = _lock_for(fingerprint)
    async with lock:
        try:
            existing = await database.get_archive_sync(fingerprint)
        except Exception:
            existing = None

        # Already mirrored: never post the same resource twice.
        if existing and existing[7] == "published":
            result["status"] = "published"
            result["message_id"] = existing[6]
            return result

        try:
            await database.upsert_archive_pending(
                fingerprint,
                folder_id=snapshot[1],
                content_id=snapshot[0],
            )
        except Exception:
            logger.exception("archive: could not register pending for %s", fingerprint)

        if with_header:
            try:
                folder = await database.get_folder(snapshot[1])
                if folder:
                    await publish_folder_header(bot, folder, snapshot[5])
            except Exception:
                logger.debug("archive: header skipped", exc_info=True)

        channel = _channel_for_send()

        try:
            sent = await _send_resource_post(bot, channel, snapshot)
            message_id = getattr(sent, "message_id", None)
            await database.mark_archive_published(fingerprint, channel, message_id)
            result["status"] = "published"
            result["message_id"] = message_id
            return result
        except Exception as exc:
            # A publication failure must never surface as a MEDBOT failure.
            logger.warning(
                "archive: publish failed for content id=%s: %s", content_id, exc
            )
            result["status"] = "failed"
            result["error"] = str(exc)
            try:
                await database.mark_archive_failed(fingerprint, exc)
            except Exception:
                logger.exception("archive: could not record failure")
            return result


# ------------------------------------------------------------
# Resync (retry failures + mirror resources registered earlier)
# ------------------------------------------------------------
async def resync(
    bot, include_published: bool = False, limit: int = MAX_RESYNC_PER_RUN
):
    """Re-mirror existing resources without ever creating duplicates.

    Walks MEDBOT's real registry in hierarchy order. Resources already recorded
    as published are skipped unless ``include_published`` is set. Returns a
    stats dict.
    """
    stats = {"published": 0, "failed": 0, "skipped": 0, "total": 0}

    if not is_configured():
        return stats

    try:
        snapshots = await database.get_all_resource_snapshots()
    except Exception:
        logger.exception("archive: resync could not read resources")
        return stats

    snapshots = list(snapshots)[: max(0, int(limit or 0))]
    stats["total"] = len(snapshots)

    seen_folders = set()

    for snapshot in snapshots:
        fingerprint = content_fingerprint(snapshot)

        try:
            existing = await database.get_archive_sync(fingerprint)
        except Exception:
            existing = None

        if existing and existing[7] == "published" and not include_published:
            stats["skipped"] += 1
            seen_folders.add(snapshot[1])
            continue

        # Emit the section header the first time we touch a folder in this pass.
        if snapshot[1] not in seen_folders:
            seen_folders.add(snapshot[1])
            try:
                folder = await database.get_folder(snapshot[1])
                if folder:
                    await publish_folder_header(bot, folder, snapshot[5])
            except Exception:
                logger.debug("archive: header skipped in resync", exc_info=True)

        outcome = await publish_resource(bot, snapshot[0])
        if outcome["status"] == "published":
            stats["published"] += 1
        elif outcome["status"] == "failed":
            stats["failed"] += 1
        else:
            stats["skipped"] += 1

    return stats


async def retry_failed(bot, limit: int = MAX_RESYNC_PER_RUN) -> dict:
    """Retry only the rows recorded as failed. Idempotent.

    A row whose resource can no longer be read from the registry is left
    untouched (it cannot be reconstructed), so this never invents content.
    """
    stats = {"published": 0, "failed": 0, "skipped": 0, "total": 0}

    if not is_configured():
        return stats

    try:
        rows = await database.get_archive_sync_rows(status="failed", limit=limit)
    except Exception:
        logger.exception("archive: could not list failed rows")
        return stats

    stats["total"] = len(rows)

    for row in rows:
        content_ids = [p for p in str(row[4] or "").split(",") if p.strip()]
        if not content_ids:
            stats["skipped"] += 1
            continue

        outcome = await publish_resource(bot, _int_or_none(content_ids[0]))
        if outcome["status"] == "published":
            stats["published"] += 1
        elif outcome["status"] == "failed":
            stats["failed"] += 1
        else:
            stats["skipped"] += 1

    return stats


# ------------------------------------------------------------
# Admin surface (can_archive; owner always passes)
# ------------------------------------------------------------
def _home_keyboard():
    return InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]])


async def _is_authorized(user_id) -> bool:
    try:
        return await database.user_has_permission(user_id, "can_archive")
    except Exception:
        return False


async def _edit(query, text, markup=None):
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup
        )
    except Exception:
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            logger.warning("archive: edit_message_text failed")


def _menu():
    return InlineKeyboardMarkup(
        [
            [btn("🔄 إعادة مزامنة الموارد", "archive_resync")],
            [btn("♻️ إعادة محاولة الفاشلة", "archive_retry")],
            [btn("📊 حالة المزامنة", "archive_status")],
            [btn("⬅️ إدارة المنصة", "admin")],
            [btn("🏠 الرئيسية", "home")],
        ]
    )


def _status_line(counts) -> str:
    return (
        f"✅ منشور: {counts.get('published', 0)} | "
        f"⏳ معلّق: {counts.get('pending', 0)} | "
        f"⚠️ فشل: {counts.get('failed', 0)}"
    )


async def show_archive(query, context=None):
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    try:
        counts = await database.get_archive_sync_counts()
    except Exception:
        counts = {}

    if is_configured():
        channel = resolve_channel()
        channel_line = f"📡 القناة: <code>{esc(channel)}</code>"
        if channel.startswith("@"):
            channel_line += "\n🔗 https://t.me/" + esc(channel.lstrip("@"))
        config_line = "🟢 الأرشيف مُهيّأ."
    else:
        config_line = (
            "🔴 <b>الأرشيف غير مُهيّأ.</b>\n"
            "حدّد متغير البيئة "
            "<code>MEDBOT_ARCHIVE_CHANNEL</code> "
            "في منصة الاستضافة (معرّف القناة أو @username)، "
            "وتأكد أن البوت مشرف فيها."
        )
        channel_line = ""

    await _edit(
        query,
        "🗄 <b>أرشيف الطوارئ للموارد</b>\n\n"
        "نسخة وصول احتياطية للموارد في قناة Telegram مستقلة، تبقى متاحة "
        "حتى إذا توقف MEDBOT. السجل في MEDBOT يظل المصدر الأساسي.\n\n"
        f"{config_line}\n"
        + (f"{channel_line}\n\n" if channel_line else "\n")
        + "📊 <b>حالة المزامنة</b>\n"
        f"{_status_line(counts)}",
        _menu(),
    )


async def show_status(query):
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    try:
        rows = await database.get_archive_sync_rows(limit=15)
    except Exception:
        rows = []

    lines = ["📊 <b>حالة مزامنة الأرشيف</b>\n"]

    if not rows:
        lines.append("لا توجد سجلات مزامنة بعد.")
    else:
        for row in rows:
            try:
                (
                    _id,
                    obj_type,
                    _fp,
                    _folder,
                    content_ids,
                    _ch,
                    _msg_id,
                    status,
                    attempts,
                    error,
                    _c,
                    updated,
                    _p,
                ) = row[:13]
            except Exception:
                continue
            icon = {
                "published": "✅",
                "pending": "⏳",
                "failed": "⚠️",
                "skipped": "⏭",
            }.get(str(status), "•")
            detail = f" — {esc(str(error)[:60])}" if error else ""
            lines.append(
                f"{icon} <b>{esc(status)}</b> · {esc(obj_type)} "
                f"· id={esc(content_ids or '-')} · {esc(attempts)} محاولة{detail}\n"
                f"   <i>{esc(updated)}</i>"
            )

    await _edit(query, "\n".join(lines), _menu())


async def run_resync(query, include_published=False):
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    if not is_configured():
        await show_archive(query)
        return

    await _edit(
        query,
        "🔄 <i>جارٍ إعادة المزامنة...</i>",
        InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
    )

    stats = await resync(query.get_bot(), include_published=include_published)

    await audit.log_action(
        query.from_user.id,
        "archive_resync",
        target_type="archive",
        details=(
            f"published={stats['published']}, failed={stats['failed']}, "
            f"skipped={stats['skipped']}, total={stats['total']}"
        ),
    )

    await _edit(
        query,
        "✅ <b>اكتملت إعادة المزامنة</b>\n\n"
        f"📦 الإجمالي: {stats['total']}\n"
        f"✅ منشور: {stats['published']}\n"
        f"⚠️ فشل: {stats['failed']}\n"
        f"⏭ تم تخطّيه (منشور مسبقاً): {stats['skipped']}",
        _menu(),
    )


async def run_retry(query):
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    if not is_configured():
        await show_archive(query)
        return

    stats = await retry_failed(query.get_bot())

    await audit.log_action(
        query.from_user.id,
        "archive_retry",
        target_type="archive",
        details=(
            f"published={stats['published']}, failed={stats['failed']}, "
            f"total={stats['total']}"
        ),
    )

    await _edit(
        query,
        "♻️ <b>إعادة محاولة النشر الفاشل</b>\n\n"
        f"📦 الإجمالي: {stats['total']}\n"
        f"✅ نجح: {stats['published']}\n"
        f"⚠️ ما زال فاشلاً: {stats['failed']}",
        _menu(),
    )


async def archive_callback_handler(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    try:
        await query.answer()
    except Exception:
        pass

    data = query.data or ""

    if data == "admin_archive":
        await show_archive(query, context)
        return

    if data == "archive_resync":
        await run_resync(query)
        return

    if data == "archive_retry":
        await run_retry(query)
        return

    if data == "archive_status":
        await show_status(query)
        return

    await _edit(query, "⚠️ إجراء غير معروف.", _home_keyboard())


def register_archive_handlers(app):
    """Register the Emergency Archive admin surface."""
    app.add_handler(
        CallbackQueryHandler(
            archive_callback_handler,
            pattern=r"^(admin_archive|archive_resync|archive_retry|archive_status)",
        )
    )
