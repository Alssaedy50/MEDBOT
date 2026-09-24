# MEDBOT: force hostname resolution to IPv4 for Telegram/httpx.
import socket as _medbot_socket

_MEDBOT_ORIGINAL_GETADDRINFO = _medbot_socket.getaddrinfo


def _medbot_ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host and family in (
        0,
        _medbot_socket.AF_UNSPEC,
        _medbot_socket.AF_INET,
    ):
        try:
            return _MEDBOT_ORIGINAL_GETADDRINFO(
                host,
                port,
                _medbot_socket.AF_INET,
                type,
                proto,
                flags,
            )
        except _medbot_socket.gaierror:
            pass

    return _MEDBOT_ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)


_medbot_socket.getaddrinfo = _medbot_ipv4_getaddrinfo

import os
import asyncio
import logging
from datetime import datetime, timedelta
from html import escape
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode, ChatAction
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import database
import messaging
import audit
import admin_management
import i18n
import platform_settings
import topics
import notifications
import visibility
import workflow
from ai import (
    generate_medbot_unified_result,
    generate_medbot_unified_response,
    warm_ai_pool,
)
from search_engine import search_library_summary

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DAILY_LIMIT = 25

# The assistant answers general and medical questions. In CI there are no keys,
# so the deterministic grounded path is what the tests exercise.
ASSISTANT_PURPOSE = (
    "يجيب عن أسئلتك الطبية والعامة من مصادر موثوقة، ويوصلك مباشرة إلى "
    "الموارد والأقسام داخل MEDBOT."
)


def _quota_reset_text() -> str:
    """Short, honest note about when the daily allowance refills.

    The day counter rolls over at midnight, so the wait is until the next local
    day begins — no numbers about the limit itself are ever shown.
    """
    now = datetime.now()
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    total_minutes = max(1, int((tomorrow - now).total_seconds() // 60))
    hours = total_minutes // 60
    minutes = total_minutes % 60

    if hours and minutes:
        wait = f"خلال {hours} ساعة و{minutes} دقيقة"
    elif hours:
        wait = f"خلال {hours} ساعة"
    else:
        wait = f"خلال {minutes} دقيقة"

    return (
        "⏳ *توقف مؤقت لا خطأ عندك.*\n"
        "وصلت إلى الحد اليومي لاستخدام المساعد. "
        f"سيتجدد العداد تلقائياً {wait} (منتصف الليل بتوقيت الخادم)، "
        "ثم يمكنك المتابعة كالمعتاد."
    )


def polling_allowed_updates():
    """Update types MEDBOT subscribes to on every long-poll.

    Telegram remembers the last `allowed_updates` it was given and reuses it
    when the parameter is omitted ("If not specified, the previous setting
    will be used"). A restricted set persisted by an earlier run or webhook
    therefore keeps filtering out `callback_query` forever. Returning the full
    set here is what makes `/start` messages and inline-button presses arrive
    together.
    """
    return Update.ALL_TYPES


def configured_admin_id() -> int:
    """Return the configured owner/admin Telegram ID, or 0 if unset.

    Security: admin identity must come from explicit configuration. A
    missing/blank/zero ADMIN_ID returns 0 and never promotes any user,
    especially not the first user who happens to send /start.
    """
    raw = os.getenv("ADMIN_ID", "").strip()

    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0

    return value if value > 0 else 0


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ============================================================
# SAFE TELEGRAM HELPERS
# ============================================================


async def send_safe_message(update: Update, text: str, reply_markup=None):
    """Send long messages safely.

    Delivered as a NEW message and registered as persistent content, so it is
    never overwritten in place by a later navigation action.
    """
    if not text:
        text = "لا توجد بيانات متاحة حالياً."

    message = update.effective_message
    if not message:
        return

    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)

    chunk_size = 4000
    chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]

    for index, chunk in enumerate(chunks):
        for attempt in range(2):
            try:
                sent = await message.reply_text(
                    chunk,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup if index == len(chunks) - 1 else None,
                )
                _register_content_message(user_id, getattr(sent, "message_id", None))
                break
            except Exception:
                try:
                    sent = await message.reply_text(
                        chunk,
                        parse_mode=None,
                        reply_markup=reply_markup if index == len(chunks) - 1 else None,
                    )
                    _register_content_message(
                        user_id, getattr(sent, "message_id", None)
                    )
                    break
                except Exception as exc:
                    if attempt == 1:
                        logger.error("Failed to send message: %s", exc)
                    await asyncio.sleep(1)


# Telegram rejects messages longer than this; leave markdown headroom.
TELEGRAM_TEXT_LIMIT = 4000


def _clamp_text(text, limit=TELEGRAM_TEXT_LIMIT):
    """Keep an inline-edit payload inside Telegram's hard 4096-char limit.

    Exceeding it makes `edit_message_text` raise, which would otherwise leave
    the caller staring at a stale screen with no hint of what happened.
    """
    if len(text) <= limit:
        return text

    logger.warning("Clamped oversized message (%d chars) to %d", len(text), limit)
    body = text[:limit]
    if "\n" in body:
        # Never end mid-line: a dangling markdown entity fails to parse.
        body = body.rsplit("\n", 1)[0]
    return body + "\n\n… (تم اختصار العرض لطوله)"


# ============================================================
# MESSAGE LIFECYCLE
# ============================================================
# Educational content (AI answers, search results, resource messages) is
# delivered as NEW messages and its id is remembered. Navigation screens are
# edited in place, but `edit_safe` refuses to edit a message that is
# registered content and sends a fresh one instead — so a later callback can
# never wipe out something the user is meant to keep.

# Module-level mirror of delivered content message ids, keyed by user id. It
# lets `edit_safe` (which only receives a query) recognise that the message a
# button lives on is *delivered content* and must not be overwritten in place.
# Bounded on both axes so it can never grow without limit.
_CONTENT_MESSAGES_BY_USER = {}
_CONTENT_REGISTRY_LIMIT = 200
_CONTENT_REGISTRY_MAX_USERS = 5000


def _register_content_message(user_id, message_id):
    if user_id is None or message_id is None:
        return
    try:
        uid = int(user_id)
        tracked = _CONTENT_MESSAGES_BY_USER.setdefault(uid, [])
        tracked.append(int(message_id))
        del tracked[:-_CONTENT_REGISTRY_LIMIT]
        # Keep the per-user map bounded: drop the oldest users when it grows.
        while len(_CONTENT_MESSAGES_BY_USER) > _CONTENT_REGISTRY_MAX_USERS:
            _CONTENT_MESSAGES_BY_USER.pop(next(iter(_CONTENT_MESSAGES_BY_USER)))
    except Exception:
        pass


def _is_content_message(user_id, message_id) -> bool:
    if user_id is None or message_id is None:
        return False
    try:
        return int(message_id) in _CONTENT_MESSAGES_BY_USER.get(int(user_id), [])
    except Exception:
        return False


# ============================================================
# LOCALIZATION
# ============================================================


async def user_lang(user_id) -> str:
    """Resolve the caller's language for interface strings."""
    try:
        return await database.get_user_language(user_id)
    except Exception:
        return i18n.DEFAULT_LANGUAGE


async def _platform_name() -> str:
    try:
        return await database.get_platform_setting("platform_name")
    except Exception:
        return "MEDBOT"



async def edit_safe(query, text, reply_markup=None, parse_mode=ParseMode.MARKDOWN):
    """Safely edit an inline message.

    Falls back to plain text if the requested parse mode fails, so a
    malformed entity can never leave the caller stuck on a stale screen.
    Oversized text is clamped rather than dropped.

    If the message being edited is *delivered content* (an AI answer, search
    results or a resource message), the screen is sent as a NEW message
    instead. This is the fix for the reported bug where navigating away from a
    delivered answer wiped it out: a later tap can never overwrite content the
    user is meant to keep.
    """
    text = _clamp_text(text or "")

    message = getattr(query, "message", None)
    user = getattr(query, "from_user", None)
    message_id = getattr(message, "message_id", None)
    user_id = getattr(user, "id", None)

    if _is_content_message(user_id, message_id):
        try:
            await message.reply_text(
                text, parse_mode=None, reply_markup=reply_markup
            )
        except Exception as exc:
            logger.warning("Failed to send navigation message: %s", exc)
        # Delivered content is never edited in place, whatever happens above.
        return

    for mode in (parse_mode, None):
        try:
            await query.edit_message_text(
                text=text,
                parse_mode=mode,
                reply_markup=reply_markup,
            )
            return
        except Exception as exc:
            # Re-tapping the same button is not a failure: Telegram rejects an
            # edit that would not change the message. Treat it as handled.
            if "not modified" in str(exc).lower():
                return
            logger.warning("Failed to edit callback message: %s", exc)

    # Editing failed for a real reason (e.g. the message is too old to edit).
    # Fall back to a brand-new message so the tap is never a silent no-op.
    if message is not None:
        try:
            await message.reply_text(
                text, parse_mode=None, reply_markup=reply_markup
            )
            return
        except Exception as exc:
            logger.warning("Failed to send fallback message: %s", exc)


def btn(text, callback):
    return InlineKeyboardButton(text, callback_data=callback)


# ============================================================
# MAIN HOME
# ============================================================


def _home_rows():
    """Declarative home-page layout: (feature key, i18n label, callback)."""
    return (
        (("resources", "menu_resources", "library:0"),),
        (
            ("assistant", "menu_assistant", "assistant"),
            ("contributions", "menu_contributions", "contribute"),
        ),
        (
            ("my_contributions", "menu_my_contributions", "my_contributions"),
            ("account", "menu_account", "account"),
        ),
        (
            ("topics", "menu_topics", "topics"),
            ("language", "menu_language", "language"),
        ),
        (
            ("contact", "menu_contact", "contact"),
            ("about", "menu_about", "about"),
        ),
    )


def home_keyboard(lang: str = None, hidden=None):
    """Start keyboard for a regular (non-privileged) user.

    Shows only the student-facing options; the admin entry point is added by
    `home_for` for admins, so a normal user never sees privileged buttons.
    Labels are rendered in the caller's language (see `i18n`); the Topics and
    Resources entries are deliberately distinct: Resources is the full
    registered hierarchy, Topics is the curated high-level academic index.

    `hidden` is the set of feature keys the operator has taken offline (see
    `database.get_hidden_features`); those entries are omitted so an update or a
    fault can hide a section instantly.
    """
    lang = lang or i18n.DEFAULT_LANGUAGE
    hidden = hidden or frozenset()

    rows = []
    for row in _home_rows():
        buttons = [
            btn(i18n.t(label, lang), callback)
            for feature, label, callback in row
            if feature not in hidden
        ]
        if buttons:
            rows.append(buttons)

    if not rows:
        rows.append([btn("🔄 تحديث", "home")])

    return InlineKeyboardMarkup(rows)


def admin_home_keyboard(lang: str = None, hidden=None, show_admin=True):
    """Start keyboard with the admin entry point appended for admins."""
    lang = lang or i18n.DEFAULT_LANGUAGE
    rows = list(home_keyboard(lang, hidden).inline_keyboard)
    if show_admin:
        rows.append([btn(i18n.t("menu_admin", lang), "admin")])
    return InlineKeyboardMarkup(rows)


async def home_for(update):
    """Role-aware start keyboard for the caller of `update`.

    Regular users get the public keyboard (minus any hidden features); admins
    additionally get the Admin Panel, which itself enumerates only the surfaces
    they are permitted to use. The keyboard is rendered in the caller's stored
    language.
    """
    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)

    try:
        hidden = await database.get_hidden_features()
    except Exception:
        hidden = set()

    is_admin = False
    if user_id is not None:
        try:
            is_admin = await database.is_user_admin(user_id)
        except Exception:
            is_admin = False

    lang = await user_lang(user_id) if user_id is not None else i18n.DEFAULT_LANGUAGE

    if not is_admin:
        return home_keyboard(lang, hidden)

    # Hiding the admin entry is a soft hide: the owner always keeps it so a
    # mistake can never lock the platform's owner out of the panel.
    try:
        is_owner = await database.is_owner(user_id)
    except Exception:
        is_owner = False
    show_admin = not ("admin_panel" in hidden and not is_owner)

    return admin_home_keyboard(lang, hidden, show_admin=show_admin)


async def show_home(update: Update):
    user = update.effective_user

    await database.register_user(
        user.id,
        user.username,
        user.full_name,
    )

    lang = await user_lang(user.id)
    platform = await _platform_name()

    welcome = await database.get_platform_setting("welcome_message")

    text = (
        f"🩺 *{platform}*\n\n"
        + i18n.t("welcome_greeting", lang, name=user.first_name) + "\n\n"
        + f"{welcome}\n\n"
        + i18n.t("choose_service", lang)
    )

    if update.callback_query:
        await edit_safe(
            update.callback_query,
            text,
            await home_for(update),
        )
    else:
        await send_safe_message(
            update,
            text,
            await home_for(update),
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_home(update)


# ============================================================
# LIBRARY
# ============================================================


def resource_icon(node_type):
    """Return a stable icon for a MEDBOT folder/resource type."""
    value = str(node_type or "").strip().lower()

    return {
        "book": "📚",
        "books": "📚",
        "video": "🎥",
        "audio": "🎧",
        "mcq": "📝",
        "summary": "📑",
        "summaries": "📑",
        "image": "🖼",
        "photo": "🖼",
        "document": "📄",
        "doc": "📄",
        "general": "📁",
    }.get(value, "📁")


def content_icon(file_type):
    """Return a Telegram-friendly icon for a registered resource."""
    value = str(file_type or "").strip().lower()

    if value in ("photo", "image", "jpg", "jpeg", "png", "webp"):
        return "🖼"
    if value in ("video", "mp4", "mkv", "mov"):
        return "🎥"
    if value in ("audio", "mp3", "m4a", "wav"):
        return "🎧"
    if value in ("mcq", "quiz"):
        return "📝"
    if value in ("pdf",):
        return "📕"
    return "📄"


def folder_keyboard(folders, parent_id=0, back_target=None, lang=None):
    lang = lang or i18n.DEFAULT_LANGUAGE
    rows = []

    for folder in folders:
        try:
            folder_id, name, node_type, accepts = folder[:4]
        except Exception:
            continue

        icon = resource_icon(node_type)

        rows.append(
            [
                btn(
                    f"{icon} {str(name)[:40]}",
                    f"folder:{folder_id}",
                )
            ]
        )

    if parent_id != 0:
        target = parent_id if back_target is None else back_target
        rows.append([btn(i18n.t("back", lang), f"library:{target}")])

    rows.append([btn(i18n.t("home", lang), "home")])

    return InlineKeyboardMarkup(rows)


async def show_library(query, parent_id=0):
    try:
        folders = await database.get_folders(parent_id)
    except Exception as exc:
        logger.exception("get_folders failed")
        await edit_safe(
            query,
            f"⚠️ تعذر فتح الموارد حالياً.\n\n`{exc}`",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    lang = await user_lang(query.from_user.id)

    # The back button must target the real parent of the folder being viewed,
    # not the folder itself.
    back_target = 0

    if parent_id:
        try:
            parent = await database.get_parent_id(parent_id)
            back_target = int(parent) if parent else 0
        except Exception:
            back_target = 0

    if parent_id == 0:
        title = (
            i18n.t("library_title", lang) + "\n\n"
            + i18n.t("library_pick_year", lang)
        )
    else:
        try:
            breadcrumb = await database.get_breadcrumbs(parent_id)
        except Exception:
            breadcrumb = i18n.t("library_title", lang)

        title = (
            i18n.t("library_title", lang) + "\n\n"
            f"📍 {breadcrumb}\n\n"
            + i18n.t("library_pick_section", lang)
        )

    if not folders:
        title += "\n\n" + i18n.t("library_empty", lang)

    await edit_safe(
        query,
        title,
        folder_keyboard(folders, parent_id, back_target, lang),
    )


async def show_folder(query, folder_id):
    try:
        folder, folders, files, parent_id, breadcrumb = (
            await database.get_folder_view(folder_id)
        )
    except Exception:
        logger.exception("Folder loading failed")
        await edit_safe(
            query,
            "⚠️ تعذر تحميل محتوى القسم حالياً.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    if not folder:
        await edit_safe(
            query,
            "⚠️ القسم غير موجود.",
            InlineKeyboardMarkup(
                [
                    [btn("📚 MEDBOT Resources", "library:0")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    rows = []

    # Child folders
    for folder_item in folders:
        try:
            child_id, name, node_type, accepts = folder_item[:4]
            icon = resource_icon(node_type)

            rows.append(
                [
                    btn(
                        f"{icon} {str(name)[:40]}",
                        f"folder:{child_id}",
                    )
                ]
            )
        except Exception:
            continue

    # Registered resources
    for item in files:
        try:
            content_id = item[0]
            title = item[1]
            file_type = item[3]
            icon = content_icon(file_type)

            rows.append(
                [
                    btn(
                        f"{icon} {str(title)[:40]}",
                        f"file:{content_id}",
                    )
                ]
            )
        except Exception:
            continue

    folder_name = folder[2] if folder else "القسم"

    lang = await user_lang(query.from_user.id)

    if not rows:
        body = (
            f"📂 *{str(folder_name)[:80]}*\n\n"
            f"📍 {breadcrumb}\n\n"
            + i18n.t("library_area_empty", lang)
        )
    else:
        body = (
            f"📂 *{str(folder_name)[:80]}*\n\n"
            f"📍 {breadcrumb}\n\n"
            + i18n.t("folder_pick", lang)
        )

    # Correct parent-aware navigation.
    rows.append([btn(i18n.t("back", lang), f"library:{parent_id or 0}")])
    rows.append([btn(i18n.t("home", lang), "home")])

    await edit_safe(
        query,
        body,
        InlineKeyboardMarkup(rows),
    )


async def _send_registered_media(context, chat_id, file_id, file_type, title):
    """Send a registered MEDBOT resource using its Telegram file_id."""
    ft = str(file_type or "").lower()

    if ft in ("photo", "image", "jpg", "jpeg", "png", "webp"):
        await context.bot.send_photo(chat_id=chat_id, photo=file_id, caption=f"🖼 {title}")
    elif ft in ("audio", "mp3", "m4a", "wav"):
        await context.bot.send_audio(chat_id=chat_id, audio=file_id, caption=f"🎧 {title}")
    elif ft in ("video", "mp4", "mkv", "mov"):
        await context.bot.send_video(chat_id=chat_id, video=file_id, caption=f"🎥 {title}")
    else:
        await context.bot.send_document(chat_id=chat_id, document=file_id, caption=f"📄 {title}")


async def open_file(query, context, content_id):
    try:
        record = await database.get_file_record(content_id)
    except Exception:
        logger.exception("get_file_record failed")
        record = None

    if not record:
        await edit_safe(
            query,
            "⚠️ المورد غير موجود أو لم يعد مسجلاً في MEDBOT.",
            InlineKeyboardMarkup(
                [
                    [btn("📚 MEDBOT Resources", "library:0")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    try:
        (
            _record_id,
            folder_id,
            title,
            file_id,
            file_type,
            _source_type,
            _source_contribution_id,
            _created_by,
        ) = record
    except Exception:
        logger.exception("Malformed content record id=%s", content_id)
        await edit_safe(
            query,
            "⚠️ تعذر قراءة سجل المورد.",
            InlineKeyboardMarkup(
                [
                    [btn("📚 MEDBOT Resources", "library:0")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    back_target = int(folder_id) if folder_id else 0

    if not file_id:
        await edit_safe(
            query,
            f"📄 *{title}*\n\n"
            "المورد مسجل في قاعدة بيانات MEDBOT، "
            "لكن لا يوجد ملف قابل للإرسال حالياً.",
            InlineKeyboardMarkup(
                [
                    [btn("⬅️ رجوع", f"folder:{back_target}")],
                    [btn("📚 MEDBOT Resources", "library:0")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    chat_id = query.message.chat_id
    user_id = getattr(query.from_user, "id", None)

    try:
        await _send_registered_media(context, chat_id, file_id, file_type, title)

        sent = await context.bot.send_message(
            chat_id=chat_id,
            text="📚 يمكنك العودة إلى المكتبة من هنا:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [btn("⬅️ رجوع", f"folder:{back_target}")],
                    [btn("📚 MEDBOT Resources", "library:0")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        # The resource message is educational content: keep it out of reach of
        # in-place navigation edits.
        _register_content_message(user_id, getattr(sent, "message_id", None))

    except Exception:
        logger.exception("File send failed for content id=%s", content_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ تعذر إرسال هذا المورد من Telegram حالياً.\n\n"
                "المورد نفسه ما زال مسجلاً في MEDBOT."
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [btn("⬅️ رجوع", f"folder:{back_target}")],
                    [btn("📚 MEDBOT Resources", "library:0")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )


# ============================================================
# SEARCH
# ============================================================


async def search_content(query_text):
    """
    Canonical MEDBOT resource search entry point.

    search_engine.search_library_summary() returns a stable
    response object. This wrapper preserves the legacy main.py
    contract by returning only the result list.
    """
    response = await search_library_summary(query_text, limit=15)

    if not isinstance(response, dict):
        raise TypeError(
            f"Unexpected search response type: {type(response).__name__}"
        )

    results = response.get("results", [])

    if not isinstance(results, list):
        raise TypeError(
            f"Unexpected search results type: {type(results).__name__}"
        )

    return results


async def _user_is_admin_safe(user_id) -> bool:
    try:
        return await database.is_user_admin(user_id)
    except Exception:
        return False


async def _feature_offline_notice(update, feature) -> bool:
    """Reply with an 'unavailable' notice when `feature` is hidden for this user.

    Returns True when the notice was sent and the caller should stop. Admins
    bypass the check so they can still use and restore a hidden feature.
    """
    try:
        if await _user_is_admin_safe(update.effective_user.id):
            return False
        if not await database.is_feature_hidden(feature):
            return False
    except Exception:
        return False

    await update.message.reply_text(
        "🛠 هذا القسم غير متاح مؤقتاً للصيانة أو التحديث.\n"
        "جرّب مرة أخرى لاحقاً.",
        reply_markup=await home_for(update),
    )
    return True


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _feature_offline_notice(update, "assistant"):
        return

    context.user_data["search_mode"] = True

    await update.message.reply_text(
        "🔎 *MEDBOT Search*\n\n"
        "اكتب اسم الكتاب أو المحاضرة أو الملف الذي تريد البحث عنه.\n\n"
        "سيتم البحث فقط داخل الموارد المسجلة في MEDBOT.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[btn("❌ إلغاء البحث", "home")]]),
    )


async def run_search(update: Update, query_text):
    try:
        results = await search_content(query_text)
    except Exception:
        logger.exception("Search failed")
        await update.message.reply_text(
            "⚠️ حدث خطأ أثناء البحث.",
            reply_markup=await home_for(update),
        )
        return

    if not results:
        await update.message.reply_text(
            "🔎 *نتيجة البحث*\n\n"
            "المورد المطلوب غير مسجل حالياً في MEDBOT.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=await home_for(update),
        )
        return

    lines = ["🔎 *نتائج البحث داخل MEDBOT*\n"]
    buttons = []

    for item in results:
        result_type = item.get("result_type")
        item_id = item.get("id")
        title = item.get("title") or item.get("name") or "بدون عنوان"
        path = item.get("path") or "بدون مسار"
        file_type = item.get("file_type")
        content_count = item.get("content_count", 0)

        if result_type == "FOLDER":
            icon = "📁"
            lines.append(
                f"{icon} *{title}*\n"
                f"   🧭 {path}\n"
                f"   📄 الموارد: {content_count}"
            )
            buttons.append(
                [btn(f"📁 {str(title)[:35]}", f"folder:{item_id}")]
            )

        elif result_type == "CONTENT":
            icon = content_icon(file_type)
            lines.append(
                f"{icon} *{title}*\n"
                f"   🧭 {path}"
            )
            buttons.append(
                [btn(f"{icon} {str(title)[:35]}", f"file:{item_id}")]
            )

        elif result_type == "EMPTY_FOLDER":
            lines.append(
                f"📁 *{title}*\n"
                f"   🧭 {path}\n"
                f"   لا توجد موارد مسجلة حالياً."
            )
            buttons.append(
                [btn(f"📁 {str(title)[:35]}", f"folder:{item_id}")]
            )

    buttons.append([btn("🤖 العودة للمساعد", "assistant")])
    buttons.append([btn("🏠 الرئيسية", "home")])

    sent = await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    # Search results are persistent educational content: register them so a
    # later navigation tap cannot overwrite them in place.
    user = getattr(update, "effective_user", None)
    _register_content_message(
        getattr(user, "id", None), getattr(sent, "message_id", None)
    )


async def show_account(query):
    user = query.from_user

    lang = await user_lang(user.id)
    language_label = database.LANGUAGE_LABELS.get(lang, lang)

    text = (
        f"{i18n.t('account_title', lang)}\n\n"
        f"👤 الاسم: {user.full_name}\n"
        f"🆔 Telegram ID: `{user.id}`\n\n"
        f"{i18n.t('account_language', lang)}: {language_label}"
    )

    await edit_safe(
        query,
        text,
        InlineKeyboardMarkup(
            [
                [btn(i18n.t("menu_language", lang), "language")],
                [btn(i18n.t("home", lang), "home")],
            ]
        ),
    )


async def show_language(query):
    user = query.from_user
    lang = await user_lang(user.id)

    rows = []
    for code in database.SUPPORTED_LANGUAGES:
        mark = "✅" if code == lang else "▫️"
        rows.append(
            [btn(f"{mark} {database.LANGUAGE_LABELS.get(code, code)}",
                 f"lang_set:{code}")]
        )
    rows.append([btn(i18n.t("home", lang), "home")])

    await edit_safe(
        query,
        i18n.t("language_title", lang),
        InlineKeyboardMarkup(rows),
    )


async def set_language(query, context, code):
    user = query.from_user

    ok = await database.set_user_language(user.id, code)

    if not ok:
        await edit_safe(
            query,
            "⚠️ لغة غير مدعومة.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    await audit.log_action(
        user.id, "language_set", target_type="user",
        target_id=user.id, details=code,
    )

    lang = await user_lang(user.id)
    await edit_safe(
        query,
        i18n.t("language_saved", lang),
        InlineKeyboardMarkup(
            [
                [btn(i18n.t("menu_language", lang), "language")],
                [btn(i18n.t("home", lang), "home")],
            ]
        ),
    )


async def show_about(query):
    try:
        about = await database.get_platform_setting("platform_about")
    except Exception:
        about = None

    if not about:
        about = (
            "MEDBOT هو نظام أكاديمي طبي يعمل من خلال Telegram "
            "لتنظيم والوصول إلى الموارد التعليمية الطبية المسجلة."
        )

    platform = await _platform_name()
    lang = await user_lang(query.from_user.id)

    await edit_safe(
        query,
        f"ℹ️ *{platform}*\n\n{about}",
        InlineKeyboardMarkup(
            [
                [btn(i18n.t("home", lang), "home")],
            ]
        ),
    )


# ============================================================
# STUDENT CONTRIBUTIONS
# ============================================================


CONTRIBUTION_STATUS_LABELS = {
    "pending": "⏳ قيد المراجعة",
    "approved": "✅ معتمدة ومنشورة",
    "rejected": "❌ مرفوضة",
    "needs_revision": "✏️ بحاجة إلى تعديل",
}


async def show_my_contributions(query):
    """List the caller's own contributions with a resubmit option."""
    user = query.from_user

    try:
        items = await database.get_user_contributions(user.id)
    except Exception:
        logger.exception("Loading user contributions failed")
        items = []

    if not items:
        text = (
            "📄 *مساهماتي*\n\n"
            "لم ترسل أي مساهمة بعد.\n\n"
            "استخدم «📤 Student Contributions» لإرسال مورد."
        )
        await edit_safe(
            query,
            text,
            InlineKeyboardMarkup(
                [
                    [btn("📤 Student Contributions", "contribute")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    buttons = []
    lines = ["📄 *مساهماتي*\n"]

    for item in items:
        try:
            (
                contribution_id,
                title,
                _file_type,
                status,
                created_at,
                rejection_reason,
                review_note,
            ) = item[:7]
            folder_path = item[8] if len(item) > 8 else None
        except Exception:
            continue

        label = CONTRIBUTION_STATUS_LABELS.get(status, status)
        lines.append(f"🆔 `{contribution_id}` — {label}\n📄 {title}")

        if folder_path:
            lines.append(f"📍 {folder_path}")

        if status == "needs_revision" and review_note:
            lines.append(f"✏️ الملاحظات: {review_note}")

        if status == "rejected" and rejection_reason:
            lines.append(f"❌ السبب: {rejection_reason}")

        lines.append("")

        if status == "needs_revision":
            buttons.append(
                [
                    btn(
                        f"✏️ إعادة إرسال {str(title)[:20]}",
                        f"resubmit:{contribution_id}",
                    )
                ]
            )

    buttons.append([btn("📤 Student Contributions", "contribute")])
    buttons.append([btn("🏠 الرئيسية", "home")])

    await edit_safe(
        query,
        "\n".join(lines),
        InlineKeyboardMarkup(buttons),
    )


async def begin_resubmission(query, context, contribution_id):
    """Verify ownership/status, then ask the contributor for new media."""
    try:
        record = await database.get_contribution(contribution_id)
    except Exception:
        record = None

    if not record:
        await edit_safe(
            query,
            "⚠️ المساهمة غير موجودة.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    owner_id, status, folder_id = record[1], record[7], record[3]

    if int(owner_id) != int(query.from_user.id):
        await edit_safe(
            query,
            "🔒 يمكن لصاحب المساهمة فقط إعادة إرسالها.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    if status != "needs_revision":
        await edit_safe(
            query,
            "ℹ️ هذه المساهمة ليست بحاجة إلى تعديل.",
            InlineKeyboardMarkup(
                [
                    [btn("📄 مساهماتي", "my_contributions")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    if not await database.folder_accepts_contributions(folder_id):
        await edit_safe(
            query,
            "⚠️ القسم لم يعد يستقبل مساهمات.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    context.user_data["contribution_resubmit_id"] = int(contribution_id)
    context.user_data.pop("contribution_folder", None)

    await edit_safe(
        query,
        "✏️ *إعادة إرسال المساهمة*\n\n"
        f"رقم المساهمة: `{contribution_id}`\n\n"
        "أرسل الآن النسخة المعدّلة كـ Document أو Audio أو Video أو Photo.",
        InlineKeyboardMarkup(
            [
                [btn("❌ إلغاء", "my_contributions")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


async def _contribution_browse_buttons(parent_id):
    """Buttons for one level of the contribution wizard.

    Only folders that can lead to a contribution target (or are themselves a
    target) are shown, so students are never led into a dead branch. Each
    button is prefixed with its path so identically named subjects in
    different blocks are distinguishable.
    """
    try:
        children = await database.get_folders(parent_id)
    except Exception:
        logger.exception("Contribution browse failed")
        children = []

    buttons = []
    name_counts = {}
    rows = []
    for folder in children:
        try:
            folder_id, name, _node_type = folder[:3]
        except Exception:
            continue

        try:
            is_target = await database.folder_accepts_contributions(folder_id)
            has_targets = await database.folder_has_contribution_target(folder_id)
        except Exception:
            is_target, has_targets = False, False

        if not (is_target or has_targets):
            continue

        key = str(name).strip()
        name_counts[key] = name_counts.get(key, 0) + 1
        rows.append((folder_id, key, is_target))

    for folder_id, name, is_target in rows:
        label = name[:35]
        # Same-named siblings in one block are genuinely ambiguous; mark them.
        if name_counts[name] > 1:
            label += f" (#{folder_id})"

        icon = "📤" if is_target else "📁"
        if is_target:
            # Target leaves start the upload directly, with no empty submenu.
            callback = f"contrib_folder:{folder_id}"
        else:
            callback = f"contrib_browse:{folder_id}"
        buttons.append([btn(f"{icon} {label}", callback)])

    if parent_id:
        try:
            parent = await database.get_folder(parent_id)
            parent_id_value = parent[1] if parent else None
        except Exception:
            parent_id_value = None
        buttons.append(
            [btn("⬅️ رجوع", f"contrib_browse:{parent_id_value or 0}")]
        )

    buttons.append([btn("🏠 الرئيسية", "home")])
    return buttons


async def show_contribute(query, parent_id=0):
    """Entry point / current level of the contribution wizard."""
    buttons = await _contribution_browse_buttons(parent_id)

    has_options = any(
        "contrib_" in (b.callback_data or "")
        for row in buttons
        for b in row
    )

    if has_options:
        if parent_id:
            try:
                breadcrumb = await database.get_breadcrumbs(parent_id)
            except Exception:
                breadcrumb = ""
            text = (
                "📤 *Student Contributions*\n\n"
                f"📍 {breadcrumb}\n\n"
                "اختر القسم الذي تريد إرسال المورد إليه:"
            )
        else:
            text = (
                "📤 *Student Contributions*\n\n"
                "تنقّل حتى تصل إلى المادة المطلوبة، ثم أرسل الملف.\n\n"
                "اختر السنة أو القسم:"
            )
    else:
        text = (
            "📤 *Student Contributions*\n\n"
            "لا توجد حالياً مجلدات مفتوحة لاستقبال مساهمات الطلاب."
        )

    await edit_safe(query, text, InlineKeyboardMarkup(buttons))


async def select_contribution_folder(query, context, folder_id):
    try:
        accepts = await database.folder_accepts_contributions(folder_id)
    except Exception:
        accepts = False

    if accepts:
        return await _begin_contribution_upload(query, context, folder_id)

    # Not a target itself: descend if it leads somewhere useful.
    try:
        has_targets = await database.folder_has_contribution_target(folder_id)
    except Exception:
        has_targets = False

    if has_targets:
        return await show_contribute(query, folder_id)

    _clear_contribution_state(context)
    await edit_safe(
        query,
        "⚠️ هذا القسم غير متاح لاستقبال المساهمات حالياً.",
        InlineKeyboardMarkup(
            [
                [btn("📤 Student Contributions", "contribute")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


async def _begin_contribution_upload(query, context, folder_id):
    """Final step: confirm the chosen destination and ask for the file."""
    context.user_data["contribution_folder"] = folder_id

    try:
        breadcrumb = await database.get_breadcrumbs(folder_id)
    except Exception:
        breadcrumb = str(folder_id)

    await edit_safe(
        query,
        "📤 *إرسال مساهمة*\n\n"
        f"📍 سيتم الإرسال إلى: {breadcrumb}\n\n"
        "أرسل الآن الملف كـ Document أو Audio أو Video أو Photo.\n\n"
        "سيتم تسجيله كمساهمة *pending* ولن يظهر في المكتبة حتى تتم مراجعته واعتماده.",
        InlineKeyboardMarkup(
            [
                [btn("❌ إلغاء", "contribute")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


def _clear_contribution_state(context):
    context.user_data.pop("contribution_folder", None)
    context.user_data.pop("contribution_resubmit_id", None)


async def notify_admins_new_contribution(bot, contribution_id: int, title: str, contributor: str):
    """Ping every admin about a new/reopened contribution.

    Failures are per-recipient and non-fatal: a blocked admin must never
    prevent the contribution from being recorded.
    """
    try:
        admins = await database.get_admins_with_permission("can_contributions")
    except Exception:
        logger.exception("Could not load admins for contribution notification")
        return 0

    text = (
        "📥 *مساهمة جديدة بانتظار المراجعة*\n\n"
        f"🆔 المساهمة: `{contribution_id}`\n"
        f"📄 العنوان: <b>{escape(str(title))}</b>\n"
        f"👤 من: {escape(str(contributor or 'طالب'))}\n\n"
        "افتح لوحة الإدارة للمراجعة."
    )

    delivered = 0

    for row in admins:
        admin_id = row[0]
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
            delivered += 1
        except Exception:
            logger.warning(
                "Contribution notification failed for admin %s", admin_id
            )

    return delivered


async def contribution_media_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    folder_id = context.user_data.get("contribution_folder")
    resubmit_id = context.user_data.get("contribution_resubmit_id")

    if not folder_id and not resubmit_id:
        return False

    user = update.effective_user
    file_id = None
    file_type = None
    title = None

    if update.message.document:
        file_id = update.message.document.file_id
        file_type = "document"
        title = update.message.document.file_name or "Student Contribution"

    elif update.message.audio:
        file_id = update.message.audio.file_id
        file_type = "audio"
        title = update.message.audio.file_name or "Student Audio"

    elif update.message.video:
        file_id = update.message.video.file_id
        file_type = "video"
        title = update.message.video.file_name or "Student Video"

    elif update.message.photo:
        file_id = update.message.photo[-1].file_id
        file_type = "photo"
        title = "Student Image"

    else:
        await update.message.reply_text(
            "⚠️ أرسل ملفاً من الأنواع المدعومة: Document / Audio / Video / Photo."
        )
        return True

    # --- Resubmission of a needs_revision contribution ---------------
    if resubmit_id:
        try:
            ok, result = await database.resubmit_contribution(
                int(resubmit_id),
                user.id,
                title,
                file_id,
                file_type,
            )
        except Exception:
            logger.exception("Contribution resubmission failed")
            ok, result = False, "⚠️ تعذر إعادة إرسال المساهمة حالياً."

        if not ok:
            await update.message.reply_text(
                result,
                reply_markup=await home_for(update),
            )
            return True

        _clear_contribution_state(context)

        await update.message.reply_text(
            "✅ *تم استلام التعديل وإعادة إرسال المساهمة.*\n\n"
            f"رقم المساهمة: `{resubmit_id}`\n"
            "الحالة الحالية: `pending`\n\n"
            "ستتم مراجعتها من جديد.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=await home_for(update),
        )

        await notify_admins_new_contribution(
            context.bot,
            int(resubmit_id),
            result,
            user.full_name,
        )
        return True

    # --- New submission ----------------------------------------------
    try:
        contribution_id = await database.add_contribution(
            user.id,
            user.full_name,
            folder_id,
            title,
            file_id,
            file_type,
        )
    except database.ContributionValidationError as exc:
        await update.message.reply_text(
            str(exc),
            reply_markup=await home_for(update),
        )
        return True
    except Exception:
        logger.exception("Contribution failed")
        await update.message.reply_text(
            "⚠️ تعذر تسجيل المساهمة حالياً. لم يتم تأكيد نجاح الإضافة.",
            reply_markup=await home_for(update),
        )
        return True

    _clear_contribution_state(context)

    await update.message.reply_text(
        "✅ *تم استلام مساهمتك بنجاح.*\n\n"
        f"رقم المساهمة: `{contribution_id}`\n"
        "الحالة الحالية: `pending`\n\n"
        "سيتمكن المشرفون من مراجعتها قبل نشرها في المكتبة.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=await home_for(update),
    )

    await notify_admins_new_contribution(
        context.bot,
        contribution_id,
        title,
        user.full_name,
    )

    return True


# ============================================================
# ADMIN
# ============================================================


async def _admin_check(query):
    """Return True only for an authenticated MEDBOT admin."""
    try:
        return await database.is_user_admin(query.from_user.id)
    except Exception:
        return False


async def _require_permission(query, permission) -> bool:
    """Capability gate for a specific admin surface.

    Sends the standard denial screen and returns False when the caller lacks
    `permission`. Existing admins default to full permissions, so this is a
    no-op for pre-RBAC admins.
    """
    try:
        allowed = await database.user_has_permission(query.from_user.id, permission)
    except Exception:
        allowed = False

    if allowed:
        return True

    await edit_safe(
        query,
        "🔒 غير مصرح.",
        InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
    )
    return False


async def _audit(query, action, target_type=None, target_id=None, details=None):
    """Best-effort audit write. Never raises into the calling handler."""
    try:
        await audit.log_action(
            query.from_user.id,
            action,
            target_type=target_type,
            target_id=target_id,
            details=details,
        )
    except Exception:
        pass


async def show_admin_folder(query, folder_id: int):
    """Show management actions for one existing folder."""
    if not await _admin_check(query):
        await edit_safe(
            query,
            "🔒 غير مصرح.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    # Capability gate (RBAC).
    if not await _require_permission(query, "can_folders"):
        return

    try:
        folder = await database.get_folder(folder_id)
    except Exception:
        folder = None

    if not folder:
        await edit_safe(
            query,
            "⚠️ القسم غير موجود.",
            InlineKeyboardMarkup([
                [btn("🗂 إدارة الأقسام", "admin_folders")],
                [btn("🏠 الرئيسية", "home")],
            ]),
        )
        return

    _, parent_id, name, node_type, accepts = folder

    try:
        files = await database.get_files(folder_id)
    except Exception:
        files = []

    try:
        children = await database.get_folders(folder_id)
    except Exception:
        children = []

    accepts_text = "مفعّلة ✅" if accepts else "متوقفة ⛔"

    rows = [
        [btn("➕ إضافة قسم فرعي", f"admin_folder_child:{folder_id}")],
        [btn("📤 رفع مورد (Resource)", f"admin_upload:{folder_id}")],
        [btn("✏️ إعادة تسمية", f"admin_folder_rename:{folder_id}")],
        [btn("📦 تغيير النوع", f"admin_folder_retype_existing:{folder_id}")],
        [btn("📤 تغيير قبول المساهمات", f"admin_folder_toggle:{folder_id}")],
        [btn("🚚 نقل القسم", f"admin_folder_move:{folder_id}")],
    ]

    # Direct navigation into this branch's real sub-sections, so the admin
    # never has to leave the panel to walk the tree.
    for child in children:
        try:
            child_id, child_name, child_type, _child_accepts = child[:4]
        except Exception:
            continue

        rows.append(
            [
                btn(
                    f"{resource_icon(child_type)} {str(child_name)[:35]}",
                    f"admin_folder:{child_id}",
                )
            ]
        )

    rows.append([btn("🗑 حذف القسم", f"admin_folder_delete:{folder_id}")])

    if parent_id:
        rows.append([btn("⬅️ القسم الأب", f"admin_folder:{parent_id}")])
    else:
        rows.append([btn("⬅️ إدارة الأقسام", "admin_folders")])

    rows.append([btn("🏠 الرئيسية", "home")])

    await edit_safe(
        query,
        "🗂 *إدارة القسم*\n\n"
        f"📁 *الاسم:* {name}\n"
        f"🏷 *النوع:* {node_type}\n"
        f"📤 *المساهمات:* {accepts_text}\n"
        f"📄 *الموارد:* {len(files)}\n"
        f"📂 *الأقسام الفرعية:* {len(children)}\n\n"
        "اختر الإجراء المطلوب أو ادخل إلى قسم فرعي:",
        InlineKeyboardMarkup(rows),
    )


async def show_admin_folders(query):
    """Admin-only folder management menu."""
    if not await _admin_check(query):
        await edit_safe(
            query,
            "🔒 غير مصرح.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    # Capability gate (RBAC).
    if not await _require_permission(query, "can_folders"):
        return

    try:
        folders = await database.get_folders(0)
    except Exception:
        folders = []

    rows = [[btn("➕ إنشاء قسم جديد", "admin_folder_create")]]

    for folder in folders:
        try:
            folder_id, name, node_type, _accepts = folder[:4]
        except Exception:
            continue

        try:
            children = await database.get_folder_children_count(folder_id)
            files = await database.get_files(folder_id)
        except Exception:
            children, files = 0, []

        rows.append(
            [
                btn(
                    f"{resource_icon(node_type)} {str(name)[:30]} "
                    f"({len(files)} مورد · {children} قسم فرعي)",
                    f"admin_folder:{folder_id}",
                )
            ]
        )

    if not folders:
        rows.append([btn("ℹ️ لا توجد أقسام بعد", "noop")])

    rows.extend(
        [
            [btn("🏠 الرئيسية", "home")],
        ]
    )

    await edit_safe(
        query,
        "🗂 *إدارة الأقسام*\n\n"
        "هذه الأقسام الرئيسية. اضغط على قسم للدخول إلى فرعه وإدارة "
        "الأقسام الفرعية أو رفع الموارد داخله.\n\n"
        "أو أنشئ قسماً جديداً:",
        InlineKeyboardMarkup(rows),
    )


async def show_admin_folder_parents(query, parent_id=0):
    """Choose the parent folder for a new folder."""
    if not await _admin_check(query):
        await edit_safe(
            query,
            "🔒 غير مصرح.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    # Capability gate (RBAC).
    if not await _require_permission(query, "can_folders"):
        return

    try:
        folders = await database.get_folders(parent_id)
    except Exception:
        folders = []

    rows = []

    # Current location can be selected as the parent.
    rows.append([btn("✅ إنشاء القسم هنا", f"admin_folder_select_parent:{parent_id}")])

    for folder in folders:
        try:
            folder_id, name, node_type, accepts = folder[:4]
            rows.append(
                [
                    btn(
                        f"📁 {str(name)[:35]}",
                        f"admin_folder_parent:{folder_id}",
                    )
                ]
            )
        except Exception:
            continue

    if parent_id != 0:
        try:
            real_parent = await database.get_parent_id(parent_id)
        except Exception:
            real_parent = 0

        if real_parent is None:
            real_parent = 0

        rows.append([btn("⬅️ رجوع", f"admin_folder_parent:{real_parent}")])

    rows.append([btn("⬅️ إدارة الأقسام", "admin_folders")])

    await edit_safe(
        query,
        "📂 *اختيار القسم الأب*\n\n"
        "ادخل إلى القسم الذي تريد وضع القسم الجديد بداخله، "
        "ثم اضغط «إنشاء القسم هنا».",
        InlineKeyboardMarkup(rows),
    )


async def start_admin_folder_create(query, context):
    """Start creation of a new top-level folder."""
    if not await _admin_check(query):
        await edit_safe(
            query,
            "🔒 غير مصرح.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    # Capability gate (RBAC).
    if not await _require_permission(query, "can_folders"):
        return

    context.user_data["admin_folder_create"] = True
    context.user_data["admin_folder_parent"] = 0
    context.user_data.pop("admin_folder_name", None)
    context.user_data.pop("admin_folder_type", None)
    workflow.begin(context, "admin_folder_create")

    await edit_safe(
        query,
        "✏️ *اسم القسم الجديد*\n\n"
        "أرسل الآن اسم القسم في رسالة نصية.\n\n"
        "مثال:\n"
        "`Anatomy 2`\n\n"
        "للإلغاء استخدم /cancel",
        InlineKeyboardMarkup(
            [
                [btn("❌ إلغاء", "admin_folders")],
            ]
        ),
    )


async def request_admin_folder_rename(update, context):
    """Capture a new name for an existing folder."""
    if not update.message or not update.message.text:
        return False

    if not context.user_data.get("admin_folder_rename"):
        return False

    if not workflow.owns(context, "admin_folder_rename"):
        return False

    try:
        is_admin = await database.is_user_admin(update.effective_user.id)
    except Exception:
        is_admin = False

    if not is_admin:
        context.user_data.pop("admin_folder_rename", None)
        context.user_data.pop("admin_folder_rename_id", None)
        await update.message.reply_text(
            "🔒 غير مصرح.",
            reply_markup=await home_for(update),
        )
        return True

    name = update.message.text.strip()

    if name == "/cancel":
        folder_id = context.user_data.pop("admin_folder_rename_id", None)
        context.user_data.pop("admin_folder_rename", None)

        if folder_id:
            await update.message.reply_text(
                "❌ تم إلغاء إعادة تسمية القسم.",
                reply_markup=InlineKeyboardMarkup([
                    [btn("↩️ العودة إلى القسم", f"admin_folder:{folder_id}")],
                    [btn("🏠 الرئيسية", "home")],
                ]),
            )
        else:
            await update.message.reply_text(
                "❌ تم إلغاء إعادة تسمية القسم.",
                reply_markup=await home_for(update),
            )
        return True

    if not name:
        await update.message.reply_text(
            "⚠️ اسم القسم لا يمكن أن يكون فارغاً. أرسل الاسم مرة أخرى."
        )
        return True

    if len(name) > 100:
        await update.message.reply_text(
            "⚠️ اسم القسم طويل جداً. الحد الأقصى 100 حرف."
        )
        return True

    folder_id = context.user_data.get("admin_folder_rename_id")

    try:
        folder_id = int(folder_id)
    except (TypeError, ValueError):
        context.user_data.pop("admin_folder_rename", None)
        context.user_data.pop("admin_folder_rename_id", None)
        await update.message.reply_text(
            "⚠️ انتهت جلسة إعادة التسمية. ابدأ العملية من جديد.",
            reply_markup=await home_for(update),
        )
        return True

    try:
        folder = await database.get_folder(folder_id)
    except Exception:
        folder = None

    if not folder:
        context.user_data.pop("admin_folder_rename", None)
        context.user_data.pop("admin_folder_rename_id", None)
        await update.message.reply_text(
            "⚠️ القسم غير موجود.",
            reply_markup=await home_for(update),
        )
        return True

    try:
        await database.update_folder_name(folder_id, name)
    except Exception:
        logger.exception("Admin folder rename failed")
        await update.message.reply_text(
            "⚠️ تعذر إعادة تسمية القسم حالياً.",
            reply_markup=InlineKeyboardMarkup([
                [btn("↩️ العودة إلى القسم", f"admin_folder:{folder_id}")],
                [btn("🏠 الرئيسية", "home")],
            ]),
        )
        return True

    context.user_data.pop("admin_folder_rename", None)
    context.user_data.pop("admin_folder_rename_id", None)

    await audit.log_action(
        update.effective_user.id, "folder_rename", target_type="folder",
        target_id=folder_id, details=f"name={name}",
    )

    await update.message.reply_text(
        "✅ تم تغيير اسم القسم بنجاح.\n\n"
        f"📁 الاسم الجديد: <b>{escape(name)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [btn("🗂 إدارة القسم", f"admin_folder:{folder_id}")],
            [btn("🗂 إدارة الأقسام", "admin_folders")],
            [btn("🏠 الرئيسية", "home")],
        ]),
    )
    return True



async def request_admin_folder_name(update, context):
    """Capture the folder name from the admin."""
    if not update.message or not update.message.text:
        return False

    if not context.user_data.get("admin_folder_create"):
        return False

    if not workflow.owns(context, "admin_folder_create"):
        return False

    try:
        is_admin = await database.is_user_admin(update.effective_user.id)
    except Exception:
        is_admin = False

    if not is_admin:
        context.user_data.pop("admin_folder_create", None)
        await update.message.reply_text(
            "🔒 غير مصرح.",
            reply_markup=await home_for(update),
        )
        return True

    name = update.message.text.strip()

    if name == "/cancel":
        context.user_data.pop("admin_folder_create", None)
        await update.message.reply_text(
            "❌ تم إلغاء إنشاء القسم.",
            reply_markup=await home_for(update),
        )
        return True

    if not name:
        await update.message.reply_text(
            "⚠️ اسم القسم لا يمكن أن يكون فارغاً. أرسل الاسم مرة أخرى."
        )
        return True

    if len(name) > 100:
        await update.message.reply_text("⚠️ اسم القسم طويل جداً. الحد الأقصى 100 حرف.")
        return True

    context.user_data["admin_folder_name"] = name
    context.user_data["admin_folder_create"] = True

    await update.message.reply_text(
        f"📁 اسم القسم:\n<b>{escape(name)}</b>\n\n" "اختر نوع القسم:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [btn("📁 عام", "admin_folder_type:general")],
                [btn("📚 كتب", "admin_folder_type:books")],
                [btn("🎧 صوتيات", "admin_folder_type:audio")],
                [btn("🎥 فيديو", "admin_folder_type:video")],
                [btn("📝 MCQ", "admin_folder_type:mcq")],
                [btn("📑 ملخصات", "admin_folder_type:summaries")],
                [btn("❌ إلغاء", "admin_folders")],
            ]
        ),
    )
    return True


async def select_admin_folder_parent(query, context, parent_id):
    """Store selected parent and ask for the folder name."""
    if not await _admin_check(query):
        await edit_safe(
            query,
            "🔒 غير مصرح.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    # Capability gate (RBAC).
    if not await _require_permission(query, "can_folders"):
        return

    context.user_data["admin_folder_create"] = True
    context.user_data["admin_folder_parent"] = int(parent_id)
    context.user_data.pop("admin_folder_name", None)
    context.user_data.pop("admin_folder_type", None)
    workflow.begin(context, "admin_folder_create")

    await edit_safe(
        query,
        "✏️ *اسم القسم الجديد*\n\n"
        "أرسل الآن اسم القسم في رسالة نصية.\n\n"
        "للإلغاء استخدم الزر أدناه.",
        InlineKeyboardMarkup(
            [
                [btn("❌ إلغاء", "admin_folders")],
            ]
        ),
    )


async def show_admin_folder_types(query, context):
    """Show folder type choices without changing the selected parent/name."""
    if not await _admin_check(query):
        await edit_safe(
            query,
            "🔒 غير مصرح.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    # Capability gate (RBAC).
    if not await _require_permission(query, "can_folders"):
        return

    name = context.user_data.get("admin_folder_name")
    parent_id = context.user_data.get("admin_folder_parent")

    if not name or parent_id is None:
        context.user_data.pop("admin_folder_create", None)
        context.user_data.pop("admin_folder_name", None)
        context.user_data.pop("admin_folder_parent", None)
        context.user_data.pop("admin_folder_type", None)

        await edit_safe(
            query,
            "⚠️ انتهت جلسة إنشاء القسم. ابدأ العملية من جديد.",
            InlineKeyboardMarkup(
                [
                    [btn("🗂 إدارة الأقسام", "admin_folders")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    await edit_safe(
        query,
        f"📁 القسم: <b>{escape(name)}</b>\n\n" "اختر نوع القسم:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [btn("📁 عام", "admin_folder_type:general")],
                [btn("📚 كتب", "admin_folder_type:books")],
                [btn("🎧 صوتيات", "admin_folder_type:audio")],
                [btn("🎥 فيديو", "admin_folder_type:video")],
                [btn("📝 MCQ", "admin_folder_type:mcq")],
                [btn("📑 ملخصات", "admin_folder_type:summaries")],
                [btn("❌ إلغاء", "admin_folders")],
            ]
        ),
    )


async def admin_folder_type(query, context, node_type):
    """Select folder type and continue to contribution setting."""
    if not await _admin_check(query):
        await edit_safe(
            query,
            "🔒 غير مصرح.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    # Capability gate (RBAC).
    if not await _require_permission(query, "can_folders"):
        return

    name = context.user_data.get("admin_folder_name")
    parent_id = context.user_data.get("admin_folder_parent")

    if not name or parent_id is None:
        context.user_data.pop("admin_folder_create", None)
        context.user_data.pop("admin_folder_name", None)
        context.user_data.pop("admin_folder_parent", None)

        await edit_safe(
            query,
            "⚠️ انتهت جلسة إنشاء القسم. ابدأ العملية من جديد.",
            InlineKeyboardMarkup(
                [
                    [btn("🗂 إدارة الأقسام", "admin_folders")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    context.user_data["admin_folder_type"] = node_type

    await edit_safe(
        query,
        f"📁 القسم: <b>{escape(name)}</b>\n"
        f"🧩 النوع: <code>{escape(str(node_type))}</code>\n\n"
        "هل يسمح هذا القسم باستقبال مساهمات الطلاب؟",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [btn("✅ نعم", "admin_folder_accepts:1")],
                [btn("❌ لا", "admin_folder_accepts:0")],
                [btn("⬅️ تغيير النوع", "admin_folder_retype")],
                [btn("❌ إلغاء", "admin_folders")],
            ]
        ),
    )


async def finish_admin_folder_create(query, context, accepts):
    """Create the folder after all parameters are selected."""
    if not await _admin_check(query):
        await edit_safe(
            query,
            "🔒 غير مصرح.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    # Capability gate (RBAC).
    if not await _require_permission(query, "can_folders"):
        return

    name = context.user_data.get("admin_folder_name")
    parent_id = context.user_data.get("admin_folder_parent")
    node_type = context.user_data.get("admin_folder_type", "general")

    if not name or parent_id is None:
        context.user_data.pop("admin_folder_create", None)
        context.user_data.pop("admin_folder_name", None)
        context.user_data.pop("admin_folder_parent", None)
        context.user_data.pop("admin_folder_type", None)

        await edit_safe(
            query,
            "⚠️ بيانات إنشاء القسم غير مكتملة. ابدأ العملية من جديد.",
            InlineKeyboardMarkup(
                [
                    [btn("🗂 إدارة الأقسام", "admin_folders")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    try:
        ok = await database.add_folder(
            parent_id=int(parent_id),
            name=name,
            node_type=node_type,
            accepts_contributions=int(accepts),
        )
    except Exception:
        logger.exception("Admin folder creation failed")
        ok = False

    context.user_data.pop("admin_folder_create", None)
    context.user_data.pop("admin_folder_name", None)
    context.user_data.pop("admin_folder_parent", None)
    context.user_data.pop("admin_folder_type", None)

    if not ok:
        await edit_safe(
            query,
            "⚠️ تعذر إنشاء القسم.",
            InlineKeyboardMarkup(
                [
                    [btn("🔄 إدارة الأقسام", "admin_folders")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    new_folder_id = int(ok)

    try:
        new_folder = await database.get_folder(new_folder_id)
    except Exception:
        new_folder = None

    new_name = new_folder[2] if new_folder else name

    await _audit(query, "folder_create", "folder",
                 details=f"name={name}, type={node_type}, parent={parent_id}")

    await edit_safe(
        query,
        "✅ <b>تم إنشاء القسم بنجاح.</b>\n\n"
        f"📁 الاسم: <b>{escape(str(new_name))}</b>\n"
        f"🧩 النوع: <code>{escape(str(node_type))}</code>\n"
        f"📤 استقبال المساهمات: {'نعم' if int(accepts) else 'لا'}\n\n"
        "يمكنك الآن رفع الموارد داخله أو إنشاء قسم فرعي.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [btn("📤 رفع مورد", f"admin_upload:{new_folder_id}")],
                [btn("➕ إضافة قسم فرعي", f"admin_folder_child:{new_folder_id}")],
                [btn("🗂 إدارة هذا القسم", f"admin_folder:{new_folder_id}")],
                [btn("🗂 إدارة الأقسام", "admin_folders")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


# ============================================================
# ADMIN RESOURCE UPLOAD / MANAGEMENT
# ============================================================

FOLDER_TYPE_OPTIONS = [
    ("general", "📁 عام"),
    ("books", "📚 كتب"),
    ("audio", "🎧 صوتيات"),
    ("video", "🎥 فيديو"),
    ("mcq", "📝 MCQ"),
    ("summaries", "📑 ملخصات"),
]

FOLDER_TYPE_KEYS = {key for key, _label in FOLDER_TYPE_OPTIONS}

FILE_TYPE_OPTIONS = [
    ("document", "📄 Document"),
    ("photo", "🖼 Photo"),
    ("audio", "🎧 Audio"),
    ("video", "🎥 Video"),
]

FILE_TYPE_KEYS = {key for key, _label in FILE_TYPE_OPTIONS}


def _folder_type_keyboard(folder_id, cancel_callback=None):
    rows = [
        [btn(label, f"admin_folder_settype:{folder_id}:{key}")]
        for key, label in FOLDER_TYPE_OPTIONS
    ]
    rows.append(
        [
            btn(
                "❌ إلغاء",
                cancel_callback or f"admin_folder:{folder_id}",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _file_type_keyboard(content_id, cancel_callback=None):
    rows = [
        [btn(label, f"admin_file_settype:{content_id}:{key}")]
        for key, label in FILE_TYPE_OPTIONS
    ]
    rows.append(
        [
            btn(
                "❌ إلغاء",
                cancel_callback or f"admin_file:{content_id}",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def _extract_media_info(message):
    """Return (file_id, file_type, suggested_title) for a Telegram media message."""
    if message.document:
        return (
            message.document.file_id,
            "document",
            message.document.file_name or "Document",
        )

    if message.photo:
        return (message.photo[-1].file_id, "photo", "Photo")

    if message.audio:
        return (
            message.audio.file_id,
            "audio",
            message.audio.file_name
            or message.audio.title
            or "Audio",
        )

    if message.video:
        return (
            message.video.file_id,
            "video",
            message.video.file_name or "Video",
        )

    if message.voice:
        return (message.voice.file_id, "audio", "Voice Note")

    return (None, None, None)


async def start_admin_upload(query, context, folder_id):
    """Enter admin resource upload state for a folder."""
    if not await _admin_check(query):
        await edit_safe(
            query,
            "🔒 غير مصرح.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    # Capability gate (RBAC).
    if not await _require_permission(query, "can_content"):
        return

    try:
        folder = await database.get_folder(folder_id)
    except Exception:
        folder = None

    if not folder:
        await edit_safe(
            query,
            "⚠️ القسم غير موجود.",
            InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
        )
        return

    # Clear any other admin workflow state so upload cannot be corrupted.
    _clear_admin_state(context)

    context.user_data["admin_upload"] = True
    context.user_data["admin_upload_folder"] = int(folder_id)
    workflow.begin(context, "admin_upload")

    await edit_safe(
        query,
        "📤 *رفع مورد إلى MEDBOT*\n\n"
        f"📁 القسم: <b>{escape(str(folder[2]))}</b>\n\n"
        "أرسل الآن المورد كـ Document أو Photo أو Audio أو Video.\n"
        "سيتم تسجيله مباشرة في المكتبة (ليس مساهمة طالب).\n\n"
        "لإلغاء العملية اضغط ❌ إلغاء أو أرسل /cancel.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [btn("❌ إلغاء", f"admin_folder:{folder_id}")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


def _clear_admin_state(context, keep=None):
    """Remove all transient admin workflow keys from user_data."""
    keys = [
        "admin_folder_create",
        "admin_folder_parent",
        "admin_folder_name",
        "admin_folder_type",
        "admin_folder_rename",
        "admin_folder_rename_id",
        "admin_folder_retype_id",
        "admin_folder_move",
        "admin_folder_move_id",
        "admin_upload",
        "admin_upload_folder",
        "admin_upload_preview",
        "admin_upload_waiting_title",
        "admin_upload_title",
        "admin_file_rename",
        "admin_file_rename_id",
        "admin_file_rename_waiting",
        "admin_file_move",
        "admin_file_move_id",
    ]

    for key in keys:
        if keep and key in keep:
            continue
        context.user_data.pop(key, None)

    # Release the workflow marker when the cancelled flow was an admin one, so
    # a text consumer from another module no longer sees a stale owner.
    if context.user_data.get(workflow.ACTIVE_KEY) in (
        "admin_upload",
        "admin_file_rename",
        "admin_folder_create",
        "admin_folder_rename",
        "admin_folder_move",
        "admin_file_move",
        "admin_folder_retype",
    ):
        context.user_data.pop(workflow.ACTIVE_KEY, None)


async def admin_upload_media_handler(update, context):
    """Handle media sent while the admin is in upload state."""
    if not context.user_data.get("admin_upload"):
        return False

    folder_id = context.user_data.get("admin_upload_folder")

    try:
        folder_id = int(folder_id)
    except (TypeError, ValueError):
        _clear_admin_state(context)
        await update.message.reply_text(
            "⚠️ انتهت جلسة الرفع. ابدأ العملية من جديد.",
            reply_markup=await home_for(update),
        )
        return True

    try:
        is_admin = await database.is_user_admin(update.effective_user.id)
    except Exception:
        is_admin = False

    if not is_admin:
        _clear_admin_state(context)
        await update.message.reply_text(
            "🔒 غير مصرح.",
            reply_markup=await home_for(update),
        )
        return True

    try:
        folder = await database.get_folder(folder_id)
    except Exception:
        folder = None

    if not folder:
        _clear_admin_state(context)
        await update.message.reply_text(
            "⚠️ القسم لم يعد موجوداً. تم إلغاء الرفع.",
            reply_markup=await home_for(update),
        )
        return True

    file_id, file_type, suggested_title = _extract_media_info(update.message)

    if not file_id:
        await update.message.reply_text(
            "⚠️ أرسل مورداً من الأنواع المدعومة: Document / Photo / Audio / Video.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [btn("❌ إلغاء الرفع", f"admin_folder:{folder_id}")],
                ]
            ),
        )
        return True

    context.user_data["admin_upload_preview"] = {
        "file_id": file_id,
        "file_type": file_type,
        "folder_id": folder_id,
        "title": suggested_title,
    }

    await update.message.reply_text(
        "📥 *تم استلام المورد*\n\n"
        f"📄 العنوان المقترح: <b>{escape(str(suggested_title))}</b>\n"
        f"📎 النوع: <code>{escape(str(file_type))}</code>\n\n"
        "اختر طريقة تسجيل العنوان:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    btn(
                        "✅ تسجيل بالعنوان المقترح",
                        "admin_upload_confirm",
                    )
                ],
                [btn("✏️ إدخال عنوان مخصص", "admin_upload_custom_title")],
                [btn("❌ إلغاء", f"admin_folder:{folder_id}")],
            ]
        ),
    )
    return True


async def admin_upload_confirm(query, context):
    if not await _admin_check(query):
        await edit_safe(
            query,
            "🔒 غير مصرح.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    # Capability gate (RBAC).
    if not await _require_permission(query, "can_content"):
        return

    preview = context.user_data.get("admin_upload_preview")

    if not isinstance(preview, dict):
        _clear_admin_state(context)
        await edit_safe(
            query,
            "⚠️ انتهت جلسة الرفع. ابدأ العملية من جديد.",
            InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
        )
        return

    await _register_admin_upload(query, context, preview)


async def admin_upload_custom_title(query, context, custom_title=None):
    if not await _admin_check(query):
        await edit_safe(
            query,
            "🔒 غير مصرح.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    # Capability gate (RBAC).
    if not await _require_permission(query, "can_content"):
        return

    preview = context.user_data.get("admin_upload_preview")

    if not isinstance(preview, dict):
        await edit_safe(
            query,
            "⚠️ انتهت جلسة الرفع. ابدأ العملية من جديد.",
            InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
        )
        return

    if custom_title is None:
        context.user_data["admin_upload_waiting_title"] = True
        await edit_safe(
            query,
            "✏️ *العنوان المخصص*\n\n"
            "أرسل الآن عنوان المورد في رسالة نصية.\n\n"
            "للإلغاء أرسل /cancel.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        btn(
                            "❌ إلغاء",
                            f"admin_folder:{preview.get('folder_id')}",
                        )
                    ],
                ]
            ),
        )
        return

    title = str(custom_title).strip()

    if not title:
        return

    if len(title) > 150:
        return

    preview["title"] = title

    await _register_admin_upload(query, context, preview)


async def _register_admin_upload(query, context, preview):
    folder_id = preview.get("folder_id")
    title = str(preview.get("title") or "Resource").strip() or "Resource"
    file_id = preview.get("file_id")
    file_type = preview.get("file_type") or "document"

    try:
        folder_id = int(folder_id)
    except (TypeError, ValueError):
        _clear_admin_state(context)
        await edit_safe(
            query,
            "⚠️ القسم الهدف غير صالح.",
            InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
        )
        return

    try:
        folder = await database.get_folder(folder_id)
    except Exception:
        folder = None

    if not folder:
        _clear_admin_state(context)
        await edit_safe(
            query,
            "⚠️ القسم لم يعد موجوداً. لم يتم تسجيل المورد.",
            InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
        )
        return

    if not file_id:
        _clear_admin_state(context)
        await edit_safe(
            query,
            "⚠️ لا يوجد ملف Telegram صالح للتسجيل.",
            InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
        )
        return

    try:
        content_id = await database.add_content(
            folder_id=folder_id,
            title=title,
            file_id=file_id,
            file_type=file_type,
            source_type="direct",
            source_contribution_id=None,
            created_by=query.from_user.id,
        )
    except Exception:
        logger.exception("Admin resource registration failed")
        content_id = None

    _clear_admin_state(context)

    if not content_id:
        await edit_safe(
            query,
            "⚠️ تعذر تسجيل المورد. لم يتم تأكيد الإضافة.",
            InlineKeyboardMarkup(
                [
                    [btn("🗂 إدارة الأقسام", "admin_folders")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    await _audit(query, "content_upload", "content", content_id,
                 details=f"title={title}, folder={folder_id}")

    await edit_safe(
        query,
        "✅ <b>تم تسجيل المورد بنجاح.</b>\n\n"
        f"📄 العنوان: <b>{escape(title)}</b>\n"
        f"📎 النوع: <code>{escape(str(file_type))}</code>\n"
        f"📁 القسم: <b>{escape(str(folder[2]))}</b>\n"
        f"🆔 المورد: <code>{content_id}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [btn("🗂 إدارة المورد", f"admin_file:{content_id}")],
                [btn("📤 رفع مورد آخر", f"admin_upload:{folder_id}")],
                [btn("🗂 إدارة القسم", f"admin_folder:{folder_id}")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


async def show_admin_file(query, content_id):
    """Show management actions for one registered resource."""
    if not await _admin_check(query):
        await edit_safe(
            query,
            "🔒 غير مصرح.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    # Capability gate (RBAC).
    if not await _require_permission(query, "can_content"):
        return

    try:
        record = await database.get_file_record(content_id)
    except Exception:
        record = None

    if not record:
        await edit_safe(
            query,
            "⚠️ المورد غير موجود.",
            InlineKeyboardMarkup(
                [
                    [btn("🗂 إدارة الأقسام", "admin_folders")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    _, folder_id, title, file_id, file_type, source_type, _scid, _cby = record
    folder_id = int(folder_id) if folder_id else 0

    try:
        folder = await database.get_folder(folder_id)
    except Exception:
        folder = None

    folder_name = folder[2] if folder else "غير معروف"

    await edit_safe(
        query,
        "🗂 *إدارة المورد*\n\n"
        f"📄 العنوان: <b>{escape(str(title))}</b>\n"
        f"📎 النوع: <code>{escape(str(file_type))}</code>\n"
        f"📁 القسم: <b>{escape(str(folder_name))}</b>\n"
        f"🔗 المصدر: <code>{escape(str(source_type or 'direct'))}</code>\n"
        f"🆔 file_id: {'موجود ✅' if file_id else 'مفقود ⚠️'}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [btn("✏️ إعادة تسمية", f"admin_file_rename:{content_id}")],
                [btn("📦 تغيير النوع", f"admin_file_retype:{content_id}")],
                [btn("🚚 نقل المورد", f"admin_file_move:{content_id}")],
                [btn("🗑 حذف المورد", f"admin_file_delete:{content_id}")],
                [btn("👁 فتح المورد", f"file:{content_id}")],
                [btn("⬅️ القسم", f"admin_folder:{folder_id}")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


async def admin_file_delete(query, context, content_id):
    if not await _admin_check(query):
        await edit_safe(
            query,
            "🔒 غير مصرح.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    # Capability gate (RBAC).
    if not await _require_permission(query, "can_content"):
        return

    try:
        record = await database.get_file_record(content_id)
    except Exception:
        record = None

    if not record:
        await edit_safe(
            query,
            "⚠️ المورد غير موجود أو تم حذفه مسبقاً.",
            InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
        )
        return

    folder_id = int(record[1]) if record[1] else 0

    try:
        ok = await database.delete_file(content_id)
    except Exception:
        logger.exception("Admin resource delete failed")
        ok = False

    if not ok:
        await edit_safe(
            query,
            "⚠️ تعذر حذف المورد.",
            InlineKeyboardMarkup([[btn("🗂 إدارة المورد", f"admin_file:{content_id}")]]),
        )
        return

    await _audit(query, "content_delete", "content", content_id)

    await edit_safe(
        query,
        "🗑 تم حذف تسجيل المورد بنجاح.",
        InlineKeyboardMarkup(
            [
                [btn("⬅️ القسم", f"admin_folder:{folder_id}")],
                [btn("🗂 إدارة الأقسام", "admin_folders")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


async def admin_folder_move_menu(query, context):
    """Show the hierarchy so the admin can pick any valid destination.

    Offers Root plus the whole tree (with cycle-safe candidates excluded), so a
    branch can be moved from one part of the hierarchy to another without
    losing its children or resources.
    """
    if not await _admin_check(query):
        await edit_safe(
            query,
            "🔒 غير مصرح.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    # Capability gate (RBAC).
    if not await _require_permission(query, "can_folders"):
        return

    try:
        folder_id = int(context.user_data.get("admin_folder_move_id"))
    except (TypeError, ValueError):
        folder_id = None

    if not folder_id:
        await edit_safe(
            query,
            "⚠️ انتهت جلسة النقل. ابدأ العملية من جديد.",
            InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
        )
        return

    try:
        folder = await database.get_folder(folder_id)
    except Exception:
        folder = None

    if not folder:
        _clear_admin_state(context)
        await edit_safe(
            query,
            "⚠️ القسم غير موجود.",
            InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
        )
        return

    context.user_data["admin_folder_move"] = True
    workflow.begin(context, "admin_folder_move")

    await _render_move_targets(query, context, folder_id, folder, parent_id=0)


async def _render_move_targets(query, context, folder_id, folder, parent_id=0):
    """Render one level of the destination picker for moving `folder_id`."""
    try:
        children = await database.get_folders(parent_id)
    except Exception:
        children = []

    if parent_id:
        try:
            breadcrumb = await database.get_breadcrumbs(parent_id)
        except Exception:
            breadcrumb = f"#{parent_id}"
    else:
        breadcrumb = "الجذر 🏠"

    rows = []
    if parent_id == 0:
        rows.append(
            [btn("🏠 نقل إلى الجذر", f"admin_folder_move_to:{folder_id}:0")]
        )

    for item in children:
        try:
            target_id, name, node_type, _acc = item[:4]
        except Exception:
            continue

        # A section cannot be moved into itself or one of its descendants.
        if int(target_id) == int(folder_id):
            continue

        try:
            if await database.is_descendant_of(int(folder_id), target_id):
                # target lies inside the moved folder -> invalid destination.
                continue
        except Exception:
            continue

        rows.append(
            [
                btn(
                    f"{resource_icon(node_type)} {str(name)[:18]}",
                    f"admin_folder_move_browse:{folder_id}:{target_id}",
                ),
                btn("✅ هنا", f"admin_folder_move_to:{folder_id}:{target_id}"),
            ]
        )

    if not children and parent_id == 0:
        rows.append([btn("ℹ️ لا توجد أقسام أخرى", "noop")])

    if parent_id:
        try:
            real_parent = await database.get_parent_id(parent_id) or 0
        except Exception:
            real_parent = 0
        rows.append(
            [btn("⬅️ رجوع", f"admin_folder_move_browse:{folder_id}:{real_parent}")]
        )

    rows.append([btn("❌ إلغاء", f"admin_folder:{folder_id}")])

    await edit_safe(
        query,
        "🚚 *نقل القسم*\n\n"
        f"📁 القسم: <b>{escape(str(folder[2]))}</b>\n"
        f"📍 الموقع الحالي للاختيار: {breadcrumb}\n\n"
        "ادخل بين الأقسام لاختيار القسم الأب الجديد، "
        "ثم اضغط «✅ هنا» بجانب القسم المطلوب، أو «🏠 نقل إلى الجذر».",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def admin_folder_move_to(query, context, folder_id, target_id):
    if not await _admin_check(query):
        await edit_safe(
            query,
            "🔒 غير مصرح.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    # Capability gate (RBAC).
    if not await _require_permission(query, "can_folders"):
        return

    try:
        ok, message = await database.move_folder(
            int(folder_id),
            int(target_id),
        )
    except Exception:
        logger.exception("Admin folder move failed")
        ok, message = False, "تعذر تنفيذ النقل."

    _clear_admin_state(context)

    if not ok:
        await edit_safe(
            query,
            f"⚠️ {escape(str(message))}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [btn("↩️ إدارة القسم", f"admin_folder:{folder_id}")],
                    [btn("🗂 إدارة الأقسام", "admin_folders")],
                ]
            ),
        )
        return

    await _audit(query, "folder_move", "folder", folder_id,
                 details=f"target={target_id}")

    await edit_safe(
        query,
        f"✅ تم نقل القسم بنجاح.\n\n{message}",
        reply_markup=InlineKeyboardMarkup(
            [
                [btn("🗂 إدارة القسم", f"admin_folder:{folder_id}")],
                [btn("🗂 إدارة الأقسام", "admin_folders")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


async def admin_folder_delete(query, context, folder_id):
    if not await _admin_check(query):
        await edit_safe(
            query,
            "🔒 غير مصرح.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    # Capability gate (RBAC).
    if not await _require_permission(query, "can_folders"):
        return

    try:
        folder = await database.get_folder(folder_id)
    except Exception:
        folder = None

    if not folder:
        await edit_safe(
            query,
            "ℹ️ القسم غير موجود أو تم حذفه مسبقاً.",
            InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
        )
        return

    parent_id = folder[1] or 0

    try:
        ok = await database.delete_folder(folder_id)
    except Exception:
        logger.exception("Admin folder delete failed")
        ok = False

    if not ok:
        await edit_safe(
            query,
            "⚠️ لا يمكن حذف هذا القسم.\n\n"
            "الحذف مسموح فقط للأقسام الفارغة تماماً "
            "(بدون أقسام فرعية أو موارد).",
            reply_markup=InlineKeyboardMarkup(
                [
                    [btn("↩️ إدارة القسم", f"admin_folder:{folder_id}")],
                    [btn("🗂 إدارة الأقسام", "admin_folders")],
                ]
            ),
        )
        return

    back_rows = []
    if parent_id:
        back_rows.append([btn("⬅️ القسم الأب", f"admin_folder:{parent_id}")])
    else:
        back_rows.append([btn("🗂 إدارة الأقسام", "admin_folders")])
    back_rows.append([btn("🏠 الرئيسية", "home")])

    await _audit(query, "folder_delete", "folder", folder_id)

    await edit_safe(
        query,
        "🗑 تم حذف القسم بنجاح.",
        reply_markup=InlineKeyboardMarkup(back_rows),
    )


async def admin_file_move_menu(query, context):
    if not await _admin_check(query):
        await edit_safe(
            query,
            "🔒 غير مصرح.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    # Capability gate (RBAC).
    if not await _require_permission(query, "can_content"):
        return

    try:
        content_id = int(context.user_data.get("admin_file_move_id"))
    except (TypeError, ValueError):
        content_id = None

    if not content_id:
        await edit_safe(
            query,
            "⚠️ انتهت جلسة نقل المورد.",
            InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
        )
        return

    try:
        record = await database.get_file_record(content_id)
    except Exception:
        record = None

    if not record:
        _clear_admin_state(context)
        await edit_safe(
            query,
            "⚠️ المورد غير موجود.",
            InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
        )
        return

    try:
        roots = await database.get_folders(0)
    except Exception:
        roots = []

    rows = []

    for item in roots:
        try:
            target_id, name, _node_type, _acc = item[:4]
        except Exception:
            continue

        rows.append(
            [
                btn(
                    f"📁 {str(name)[:35]}",
                    f"admin_file_move_to:{content_id}:{target_id}",
                )
            ]
        )

    rows.append([btn("❌ إلغاء", f"admin_file:{content_id}")])

    await edit_safe(
        query,
        "🚚 *نقل المورد*\n\n"
        f"📄 المورد: <b>{escape(str(record[2]))}</b>\n\n"
        "اختر القسم الهدف:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def admin_file_move_to(query, context, content_id, target_id):
    if not await _admin_check(query):
        await edit_safe(
            query,
            "🔒 غير مصرح.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    # Capability gate (RBAC).
    if not await _require_permission(query, "can_content"):
        return

    try:
        ok, message = await database.move_content(
            int(content_id),
            int(target_id),
        )
    except Exception:
        logger.exception("Admin resource move failed")
        ok, message = False, "تعذر تنفيذ النقل."

    _clear_admin_state(context)

    if not ok:
        await edit_safe(
            query,
            f"⚠️ {escape(str(message))}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[btn("↩️ إدارة المورد", f"admin_file:{content_id}")]]
            ),
        )
        return

    await _audit(query, "content_move", "content", content_id,
                 details=f"target={target_id}")

    await edit_safe(
        query,
        f"✅ {escape(str(message))}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [btn("🗂 إدارة المورد", f"admin_file:{content_id}")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


async def handle_pending_title_input(update, context):
    """Consume custom title input for upload or rename workflows."""
    if not update.message or not update.message.text:
        return False

    custom = context.user_data.get("admin_upload_waiting_title")
    rename_mode = context.user_data.get("admin_file_rename")
    rename_waiting = context.user_data.get("admin_file_rename_waiting")

    if not custom and not (rename_mode and rename_waiting):
        return False

    # Only the workflow the user actually armed may consume this text; a
    # leftover flag from a semi-finished flow would otherwise steal it.
    if custom and not workflow.owns(context, "admin_upload"):
        return False
    if not custom and not workflow.owns(context, "admin_file_rename"):
        return False

    try:
        is_admin = await database.is_user_admin(update.effective_user.id)
    except Exception:
        is_admin = False

    if not is_admin:
        _clear_admin_state(context)
        await update.message.reply_text(
            "🔒 غير مصرح.",
            reply_markup=await home_for(update),
        )
        return True

    text = update.message.text.strip()

    if text == "/cancel":
        _clear_admin_state(context)
        await update.message.reply_text(
            "❌ تم إلغاء العملية.",
            reply_markup=await home_for(update),
        )
        return True

    if not text:
        await update.message.reply_text("⚠️ النص فارغ. أرسل عنواناً صالحاً.")
        return True

    if len(text) > 150:
        await update.message.reply_text("⚠️ العنوان طويل جداً (الحد 150 حرفاً).")
        return True

    if custom:
        preview = context.user_data.get("admin_upload_preview")

        if not isinstance(preview, dict):
            _clear_admin_state(context)
            await update.message.reply_text(
                "⚠️ انتهت جلسة الرفع. ابدأ من جديد.",
                reply_markup=await home_for(update),
            )
            return True

        preview["title"] = text
        context.user_data.pop("admin_upload_waiting_title", None)
        context.user_data.pop("admin_upload_title", None)

        folder_id = preview.get("folder_id")

        await update.message.reply_text(
            "✏️ *تأكيد العنوان*\n\n"
            f"📄 العنوان: <b>{escape(text)}</b>\n\n"
            "هل تريد تسجيل المورد بهذا العنوان؟",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [btn("✅ تسجيل", "admin_upload_confirm")],
                    [btn("❌ إلغاء", f"admin_folder:{folder_id}")],
                ]
            ),
        )
        return True

    # Resource rename state.
    try:
        content_id = int(context.user_data.get("admin_file_rename_id"))
    except (TypeError, ValueError):
        _clear_admin_state(context)
        await update.message.reply_text(
            "⚠️ انتهت جلسة إعادة التسمية.",
            reply_markup=await home_for(update),
        )
        return True

    try:
        record = await database.get_file_record(content_id)
    except Exception:
        record = None

    if not record:
        _clear_admin_state(context)
        await update.message.reply_text(
            "⚠️ المورد غير موجود.",
            reply_markup=await home_for(update),
        )
        return True

    try:
        ok = await database.update_file_title(content_id, text)
    except Exception:
        logger.exception("Admin resource rename failed")
        ok = False

    _clear_admin_state(context)

    if not ok:
        await update.message.reply_text(
            "⚠️ تعذر إعادة تسمية المورد.",
            reply_markup=await home_for(update),
        )
        return True

    await audit.log_action(
        update.effective_user.id, "content_rename", target_type="content",
        target_id=content_id, details=f"title={text}",
    )

    await update.message.reply_text(
        "✅ تم تغيير عنوان المورد بنجاح.\n\n"
        f"📄 العنوان الجديد: <b>{escape(text)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [btn("🗂 إدارة المورد", f"admin_file:{content_id}")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )
    return True



async def show_admin(query):
    user_id = query.from_user.id

    try:
        is_admin = await database.is_user_admin(user_id)
    except Exception:
        is_admin = False

    if not is_admin:
        await edit_safe(
            query,
            "🔒 هذه المنطقة مخصصة للمشرفين المعتمدين فقط.",
            InlineKeyboardMarkup(
                [
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    try:
        pending_count = await database.get_pending_contributions_count()
    except Exception:
        pending_count = 0

    try:
        open_messages = await database.get_open_messages_count()
    except Exception:
        open_messages = 0

    rows = []

    async def _allowed(permission):
        try:
            return await database.user_has_permission(user_id, permission)
        except Exception:
            return False

    # Each admin surface appears once and only when the caller may use it.
    if await _allowed("can_folders"):
        rows.append([btn("🗂 إدارة الأقسام والفروع", "admin_folders")])
    if await _allowed("can_contributions"):
        rows.append([btn("📥 مراجعة المساهمات", "admin_pending")])
    if await _allowed("can_messages"):
        rows.append([btn("📬 رسائل الطلاب", "admin_messages")])
    if await _allowed("can_ai"):
        rows.append([btn("🤖 AI Registry", "admin_ai")])
    if await _allowed("can_notifications"):
        rows.append([btn("🔔 الإشعارات", "admin_notifications")])
    if await _allowed("can_topics"):
        rows.append([btn("🧭 مواضيع البحث", "admin_topics")])
    if await _allowed("can_visibility"):
        rows.append([btn("🙈 إظهار/إخفاء الأقسام", "vis_list")])
    if await _allowed("can_settings"):
        rows.append([btn("⚙️ إعدادات المنصة", "admin_settings")])

    rows.append([btn("📊 Runtime", "admin_runtime")])

    # Admin management + audit trail are for owners / admins granted can_admins.
    if await _allowed("can_admins"):
        rows.append([btn("👥 إدارة المشرفين", "amg_list")])
        rows.append([btn("📜 سجل التدقيق", "audit_log")])

    rows.append([btn("🏠 الرئيسية", "home")])

    await edit_safe(
        query,
        "🛠 *MEDBOT Admin Panel*\n\n"
        f"📤 المساهمات المعلقة: {pending_count}\n"
        f"📬 رسائل الطلاب غير المغلقة: {open_messages}\n\n"
        "اختر الإجراء:",
        InlineKeyboardMarkup(rows),
    )


async def show_pending(query, context):
    try:
        is_admin = await database.is_user_admin(query.from_user.id)
    except Exception:
        is_admin = False

    if not is_admin:
        await edit_safe(
            query,
            "🔒 غير مصرح.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    if not await _require_permission(query, "can_contributions"):
        return

    try:
        items = await database.get_pending_contributions_list()
    except Exception:
        items = []

    if not items:
        await edit_safe(
            query,
            "📥 لا توجد مساهمات معلقة حالياً.",
            InlineKeyboardMarkup(
                [
                    [btn("⬅️ Admin", "admin")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    buttons = []

    for item in items:
        try:
            contribution_id = item[0]
            title = item[3] if len(item) > 3 else f"Contribution {contribution_id}"
        except Exception:
            continue

        buttons.append(
            [
                btn(
                    f"📄 {str(title)[:28]}",
                    f"review:{contribution_id}",
                )
            ]
        )

    buttons.append([btn("⬅️ Admin", "admin")])
    buttons.append([btn("🏠 الرئيسية", "home")])

    await edit_safe(
        query,
        "📥 *Pending Contributions*\n\n" "اختر مساهمة لمراجعتها:",
        InlineKeyboardMarkup(buttons),
    )


async def review_contribution(query, context, contribution_id):
    try:
        is_admin = await database.is_user_admin(query.from_user.id)
    except Exception:
        is_admin = False

    if not is_admin:
        await edit_safe(
            query,
            "🔒 غير مصرح لك بمراجعة المساهمات.",
            InlineKeyboardMarkup(
                [
                    [btn("⬅️ الرئيسية", "home")],
                ]
            ),
        )
        return

    try:
        items = await database.get_pending_contributions_list()
    except Exception:
        items = []

    target = None

    for item in items:
        try:
            if int(item[0]) == int(contribution_id):
                target = item
                break
        except Exception:
            continue

    if not target:
        await edit_safe(
            query,
            "المساهمة غير موجودة أو تمت معالجتها بالفعل.",
            InlineKeyboardMarkup(
                [
                    [btn("📥 Pending", "admin_pending")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    try:
        (
            _,
            user_id,
            user_name,
            title,
            file_id,
            file_type,
            folder_id,
            status,
            created_at,
        ) = target[:9]
    except Exception:
        user_id = "?"
        user_name = "?"
        title = "Contribution"
        file_id = None
        file_type = "document"
        folder_id = "?"
        created_at = "?"

    text = (
        "📥 *Contribution Review*\n\n"
        f"👤 الطالب: {user_name}\n"
        f"🆔 User ID: `{user_id}`\n"
        f"📄 العنوان: {title}\n"
        f"📁 Folder ID: `{folder_id}`\n"
        f"📎 النوع: {file_type}\n"
        f"🏷 الحالة: `{status}`\n"
        f"🕒 التاريخ: {created_at}\n"
    )

    lang = await user_lang(query.from_user.id)

    await edit_safe(
        query,
        text,
        InlineKeyboardMarkup(
            [
                [btn(i18n.t("contrib_preview", lang), f"preview:{contribution_id}")],
                [
                    btn("✅ Approve", f"approve:{contribution_id}"),
                    btn("❌ Reject", f"reject:{contribution_id}"),
                ],
                [btn("✏️ Needs Revision", f"revise:{contribution_id}")],
                [btn("⬅️ Pending", "admin_pending")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


async def preview_contribution(query, context, contribution_id):
    """Send the submitted file to the reviewing admin without changing status.

    Read-only: the contribution row is never mutated, so an admin can inspect
    the exact file a student submitted before deciding. Sending the real media
    to the admin's own chat also avoids Telegram's `file_id` scope problems
    (a file_id is only usable by the bot that owns it, but a copy sent to the
    admin's chat is directly playable).
    """
    try:
        is_admin = await database.is_user_admin(query.from_user.id)
    except Exception:
        is_admin = False

    if not is_admin:
        await edit_safe(
            query,
            i18n.t("unauthorized"),
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    if not await _require_permission(query, "can_contributions"):
        return

    try:
        record = await database.get_contribution(contribution_id)
    except Exception:
        logger.exception("Preview: get_contribution failed")
        record = None

    if not record:
        await edit_safe(
            query,
            i18n.t("contrib_not_found"),
            InlineKeyboardMarkup(
                [
                    [btn("📥 Pending", "admin_pending")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    (_cid, user_id, user_name, folder_id, title, file_id, file_type,
     status, created_at) = record[:9]

    try:
        breadcrumb = await database.get_breadcrumbs(folder_id)
    except Exception:
        breadcrumb = None

    lang = await user_lang(query.from_user.id)

    header = (
        f"🔍 <b>{i18n.t('contrib_preview', lang)} #{contribution_id}</b>\n\n"
        f"{i18n.t('contrib_student', lang)}: "
        f"{escape(str(user_name or '—'))} (<code>{user_id}</code>)\n"
        f"{i18n.t('contrib_title_label', lang)}: "
        f"<b>{escape(str(title or '—'))}</b>\n"
        f"{i18n.t('contrib_type_label', lang)}: "
        f"<code>{escape(str(file_type or '—'))}</code>\n"
    )
    if breadcrumb:
        header += (
            f"{i18n.t('contrib_destination', lang)}: "
            f"{escape(str(breadcrumb))}\n"
        )
    header += (
        f"{i18n.t('contrib_status_label', lang)}: "
        f"<code>{escape(str(status or '—'))}</code>\n"
    )

    chat_id = query.message.chat_id
    back_btn = btn(
        i18n.t("contrib_back_to_review", lang), f"review:{contribution_id}"
    )

    if not file_id:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=header + "\n" + i18n.t("contrib_no_file", lang),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[back_btn]]),
            )
        except Exception:
            logger.warning("Preview: could not send preview header")
        return

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=header,
            parse_mode=ParseMode.HTML,
        )
        await _send_registered_media(
            context, chat_id, file_id, file_type, title or f"#{contribution_id}"
        )
        note = await context.bot.send_message(
            chat_id=chat_id,
            text=i18n.t("contrib_preview_note", lang),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        btn(
                            i18n.t("approve_action", lang),
                            f"approve:{contribution_id}",
                        ),
                        btn(
                            i18n.t("reject_action", lang),
                            f"reject:{contribution_id}",
                        ),
                    ],
                    [back_btn],
                    [btn(i18n.t("home", lang), "home")],
                ]
            ),
        )
        _register_content_message(
            getattr(query.from_user, "id", None),
            getattr(note, "message_id", None),
        )
    except Exception:
        logger.exception("Preview delivery failed for #%s", contribution_id)
        await edit_safe(
            query,
            header + "\n" + i18n.t("contrib_preview_failed", lang),
            InlineKeyboardMarkup([[back_btn]]),
        )


REVIEW_NOTE_MAX_LENGTH = 400


async def _prompt_review_text(query, contribution_id, kind):
    """Ask the admin to type a rejection reason or revision note."""
    if kind == "reject":
        prompt = (
            "❌ *سبب الرفض*\n\n"
            f"اكتب سبب رفض المساهمة رقم `{contribution_id}`.\n"
            "سيُرسل السبب إلى صاحب المساهمة.\n\n"
            "أو اضغط إلغاء للتراجع."
        )
    else:
        prompt = (
            "✏️ *ملاحظات التعديل*\n\n"
            f"اكتب ما يجب تعديله في المساهمة رقم `{contribution_id}`.\n"
            "ستُرسل الملاحظات إلى صاحب المساهمة.\n\n"
            "أو اضغط إلغاء للتراجع."
        )

    await edit_safe(
        query,
        prompt,
        InlineKeyboardMarkup(
            [
                [btn("❌ إلغاء", f"review:{contribution_id}")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


async def process_review_text(update, context):
    """Consume typed review text when an admin is replying to a prompt.

    Returns True when the message was handled here.
    """
    pending_kind = context.user_data.get("review_note_kind")
    pending_id = context.user_data.get("review_note_id")

    if not pending_kind or not pending_id:
        return False

    # Another workflow may have claimed the pending input in the meantime.
    if not workflow.owns(context, "review_note"):
        _clear_review_state(context)
        return False

    try:
        is_admin = await database.is_user_admin(update.effective_user.id)
    except Exception:
        is_admin = False

    if not is_admin:
        _clear_review_state(context)
        await update.message.reply_text(
            "🔒 غير مصرح.",
            reply_markup=await home_for(update),
        )
        return True

    text = (update.message.text or "").strip()

    if text == "/cancel":
        _clear_review_state(context)
        await update.message.reply_text(
            "❌ تم إلغاء المراجعة.",
            reply_markup=await home_for(update),
        )
        return True

    if not text:
        await update.message.reply_text("⚠️ النص فارغ. اكتب سبباً واضحاً.")
        return True

    if len(text) > REVIEW_NOTE_MAX_LENGTH:
        await update.message.reply_text(
            f"⚠️ النص طويل جداً (الحد {REVIEW_NOTE_MAX_LENGTH} حرفاً)."
        )
        return True

    contribution_id = int(pending_id)
    reviewer_id = update.effective_user.id
    kind = pending_kind

    _clear_review_state(context)

    if kind == "reject":
        try:
            result = await database.reject_contribution(
                contribution_id,
                reviewer_id=reviewer_id,
                reason=text,
            )
        except Exception:
            logger.exception("Contribution rejection with reason failed")
            result = None

        if not result:
            await update.message.reply_text(
                "ℹ️ المساهمة غير موجودة أو تمت معالجتها مسبقاً.",
                reply_markup=await home_for(update),
            )
            return True

        contributor_id, title = result[0], result[1]

        await update.message.reply_text(
            f"✅ تم رفض المساهمة رقم `{contribution_id}`.\n"
            "تم إرسال سبب الرفض إلى صاحب المساهمة.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=await home_for(update),
        )

        await _notify_contributor(
            context.bot,
            contributor_id,
            (
                "📢 تحديث مساهمتك في MEDBOT\n\n"
                f"تم رفض المساهمة رقم `{contribution_id}`.\n\n"
                f"السبب:\n{text}"
            ),
        )

        await audit.log_action(
            reviewer_id, "contribution_reject", target_type="contribution",
            target_id=contribution_id, details=f"reason={text}",
        )
        return True

    # kind == "revise"
    try:
        result = await database.request_contribution_revision(
            contribution_id,
            reviewer_id=reviewer_id,
            note=text,
        )
    except Exception:
        logger.exception("Contribution revision request failed")
        result = None

    if not result:
        await update.message.reply_text(
            "ℹ️ المساهمة غير موجودة أو تمت معالجتها مسبقاً.",
            reply_markup=await home_for(update),
        )
        return True

    contributor_id = result[0]

    await update.message.reply_text(
        f"✅ تم طلب تعديل المساهمة رقم `{contribution_id}`.\n"
        "تم إرسال الملاحظات إلى صاحب المساهمة.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=await home_for(update),
    )

    await _notify_contributor(
        context.bot,
        contributor_id,
        (
            "📢 تحديث مساهمتك في MEDBOT\n\n"
            f"المساهمة رقم `{contribution_id}` بحاجة إلى تعديل.\n\n"
            f"الملاحظات:\n{text}\n\n"
            "يمكنك إعادة إرسال المساهمة بعد التعديل من "
            "«📄 مساهماتي»."
        ),
    )

    await audit.log_action(
        reviewer_id, "contribution_revise", target_type="contribution",
        target_id=contribution_id, details=f"note={text}",
    )
    return True


async def _notify_contributor(bot, contributor_id, text):
    """Best-effort contributor notification. Never raises."""
    try:
        await bot.send_message(
            chat_id=contributor_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        logger.warning("Contributor notification failed for %s", contributor_id)


def _clear_review_state(context):
    context.user_data.pop("review_note_kind", None)
    context.user_data.pop("review_note_id", None)
    if context.user_data.get(workflow.ACTIVE_KEY) == "review_note":
        context.user_data.pop(workflow.ACTIVE_KEY, None)


async def request_review_note(query, context, contribution_id, kind):
    """Authorize, then arm the typed-text review flow for this admin."""
    try:
        is_admin = await database.is_user_admin(query.from_user.id)
    except Exception:
        is_admin = False

    if not is_admin:
        await edit_safe(
            query,
            "🔒 غير مصرح لك بمراجعة المساهمات.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    try:
        record = await database.get_contribution(contribution_id)
    except Exception:
        record = None

    if not record or record[7] not in database.REVIEWABLE_STATUSES:
        await edit_safe(
            query,
            "ℹ️ المساهمة غير موجودة أو تمت معالجتها مسبقاً.",
            InlineKeyboardMarkup(
                [
                    [btn("📥 Pending", "admin_pending")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    if context is not None:
        workflow.begin(context, "review_note")
        context.user_data["review_note_kind"] = kind
        context.user_data["review_note_id"] = contribution_id

    await _prompt_review_text(query, contribution_id, kind)


async def process_approval(query, contribution_id, approve):
    try:
        is_admin = await database.is_user_admin(query.from_user.id)
    except Exception:
        is_admin = False

    try:
        can_review = await database.user_has_permission(
            query.from_user.id, "can_contributions"
        )
    except Exception:
        can_review = False

    if not is_admin or not can_review:
        await edit_safe(
            query,
            "🔒 غير مصرح لك بمراجعة المساهمات.",
            InlineKeyboardMarkup(
                [
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    reviewer_id = query.from_user.id

    try:
        if approve:
            result = await database.approve_contribution(
                contribution_id,
                reviewer_id=reviewer_id,
            )
            action = "اعتماد"
        else:
            result = await database.reject_contribution(
                contribution_id,
                reviewer_id=reviewer_id,
                reason=None,
            )
            action = "رفض"

        if not result:
            await edit_safe(
                query,
                "ℹ️ المساهمة غير موجودة أو تمت معالجتها مسبقاً.",
                InlineKeyboardMarkup(
                    [
                        [btn("📥 Pending", "admin_pending")],
                        [btn("🛠 Admin", "admin")],
                        [btn("🏠 الرئيسية", "home")],
                    ]
                ),
            )
            return

        await edit_safe(
            query,
            f"✅ تم {action} المساهمة بنجاح.",
            InlineKeyboardMarkup(
                [
                    [btn("📥 Pending", "admin_pending")],
                    [btn("🛠 Admin", "admin")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )

        await _audit(
            query,
            "contribution_approve" if approve else "contribution_reject",
            "contribution",
            contribution_id,
        )

        # Only approval carries the contributor id at index 4.
        if approve and len(result) >= 5:
            await _notify_contributor(
                query.get_bot(),
                result[4],
                (
                    "📢 تحديث مساهمتك في MEDBOT\n\n"
                    f"تم اعتماد المساهمة رقم `{contribution_id}` "
                    "ونشرها في المكتبة."
                ),
            )

    except Exception as exc:
        logger.exception("Contribution approval/rejection failed")
        await edit_safe(
            query,
            "⚠️ تعذر تنفيذ العملية. لم يتم تأكيد نجاحها.",
            InlineKeyboardMarkup(
                [
                    [btn("⬅️ Pending", "admin_pending")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )


# Telegram rejects messages over 4096 chars; keep well under it.
REGISTRY_MAX_CHARS = 3500


def _shorten(value, limit=40):
    text = str(value) if value is not None else "N/A"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _registry_summary(rows):
    """Bounded, grouped rendering of the AI registry.

    The registry can hold hundreds of discovered models, so the viewer must
    stay under Telegram's message limit and show a count instead of silently
    dropping (or failing to send) the tail.
    """
    by_provider = {}
    for row in rows:
        try:
            by_provider.setdefault(row[1] or "N/A", []).append(row)
        except Exception:
            continue

    lines = [f"🤖 *AI Registry* — {len(rows)} models, {len(by_provider)} providers\n"]
    max_per_provider = 3

    for provider in sorted(by_provider):
        items = by_provider[provider]
        lines.append(f"*{_shorten(provider, 30)}* ({len(items)})")

        for row in items[:max_per_provider]:
            try:
                model = _shorten(row[2])
                availability = row[4] or "N/A"
                auth_status = row[5] or "N/A"
                last_test = row[11] or "never"
            except Exception:
                continue

            lines.append(
                f"  • `{model}` — `{availability}` / auth `{auth_status}`"
                f" / test `{_shorten(last_test, 20)}`"
            )

        if len(items) > max_per_provider:
            lines.append(f"  … +{len(items) - max_per_provider} more")

    text = "\n".join(lines)
    if len(text) > REGISTRY_MAX_CHARS:
        # Cut on a line boundary so markdown entities never end mid-line.
        text = text[:REGISTRY_MAX_CHARS].rsplit("\n", 1)[0] + "\n… (عرض مختصر)"

    return text


async def show_ai_registry(query):
    if not await _require_permission(query, "can_ai"):
        return

    try:
        rows = await database.ai_registry_get_all()
    except Exception:
        rows = []

    if not rows:
        text = "🤖 *AI Registry*\n\nلا توجد نماذج مسجلة حالياً."
    else:
        text = _registry_summary(rows)

    await edit_safe(
        query,
        text,
        InlineKeyboardMarkup(
            [
                [btn("⬅️ Admin", "admin")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


async def show_runtime(query):
    try:
        is_admin = await database.is_user_admin(query.from_user.id)
    except Exception:
        is_admin = False

    if not is_admin:
        await edit_safe(
            query,
            "🔒 غير مصرح لك بالوصول إلى معلومات Runtime.",
            InlineKeyboardMarkup(
                [
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    try:
        users = await database.get_all_user_ids()
        pending = await database.get_pending_contributions_count()
    except Exception:
        users = []
        pending = "?"

    await edit_safe(
        query,
        "📊 *MEDBOT Runtime*\n\n"
        f"👥 Registered users: {len(users)}\n"
        f"📥 Pending contributions: {pending}\n"
        "🟢 Telegram polling: active while this process is running.\n",
        InlineKeyboardMarkup(
            [
                [btn("⬅️ Admin", "admin")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================


async def _notify_feature_hidden(query):
    """Tell the user a feature is temporarily unavailable, with a home button."""
    await edit_safe(
        query,
        "🛠 هذا القسم غير متاح مؤقتاً للصيانة أو التحديث.\n\n"
        "جرّب مرة أخرى لاحقاً.",
        InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
    )


def _feature_for_callback(data: str):
    """Map a callback_data string to the feature key that owns it.

    Only the student-facing entry points are mapped; admin-only callbacks are
    never gated here (they are permission-gated by their own modules).
    """
    if data == "language" or data.startswith("lang_set:"):
        return "language"
    if data in (
        "assistant",
        "assistant_start",
        "assistant_search",
        "assistant_medical",
        "search",
    ):
        return "assistant"
    if (
        data.startswith("library:")
        or data.startswith("library_parent:")
        or data.startswith("folder:")
        or data.startswith("file:")
    ):
        return "resources"
    if data == "account":
        return "account"
    if data == "about":
        return "about"
    if data == "my_contributions" or data.startswith("resubmit:"):
        return "my_contributions"
    if (
        data == "contribute"
        or data.startswith("contrib_browse:")
        or data.startswith("contrib_folder:")
    ):
        return "contributions"
    if data == "topics" or data.startswith("topic_open:"):
        return "topics"
    if data == "contact" or data.startswith("msg_"):
        return "contact"
    if data == "admin":
        return "admin_panel"
    return None


async def _feature_blocked_for(query, feature) -> bool:
    """True when `feature` is hidden and the caller is not an admin/owner.

    Admins bypass the gate so they can verify what is hidden and restore it.
    """
    try:
        if await database.is_user_admin(query.from_user.id):
            return False
    except Exception:
        pass
    try:
        return await database.is_feature_hidden(feature)
    except Exception:
        return False


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    try:
        await query.answer()
    except Exception:
        logger.warning("Failed to answer callback query", exc_info=True)

    data = query.data or ""

    if data == "home":
        context.user_data["search_mode"] = False
        context.user_data["assistant_mode"] = None
        _clear_admin_state(context)
        _clear_review_state(context)
        _clear_contribution_state(context)
        messaging._clear_contact_state(context)
        workflow.clear_all(context)
        await show_home(update)
        return

    if data == "noop":
        return

    # Operator-controlled visibility: a hidden feature is blocked here too, so
    # a stale button (or a deep link) can never re-open something taken offline.
    # Admins and owners bypass the gate so they can still inspect and restore it.
    feature = _feature_for_callback(data)
    if feature and await _feature_blocked_for(query, feature):
        await _notify_feature_hidden(query)
        return

    if data == "language":
        await show_language(query)
        return

    if data.startswith("lang_set:"):
        await set_language(query, context, data.split(":", 1)[1])
        return

    if data.startswith("library:"):
        try:
            parent_id = int(data.split(":", 1)[1])
        except Exception:
            parent_id = 0

        await show_library(query, parent_id)
        return

    if data.startswith("library_parent:"):
        try:
            parent_id = int(data.split(":", 1)[1])
        except Exception:
            parent_id = 0

        await show_library(query, parent_id)
        return

    if data.startswith("folder:"):
        try:
            folder_id = int(data.split(":", 1)[1])
            await show_folder(query, folder_id)
        except Exception:
            await edit_safe(
                query,
                "⚠️ تعذر فتح المجلد.",
                InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
            )
        return

    if data.startswith("file:"):
        try:
            content_id = int(data.split(":", 1)[1])
            await open_file(query, context, content_id)
        except Exception:
            await edit_safe(
                query,
                "⚠️ تعذر فتح المورد.",
                InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
            )
        return

    if data == "search":
        context.user_data["search_mode"] = True
        await edit_safe(
            query,
            "🔎 *Search*\n\n"
            "اكتب الآن اسم الكتاب أو المحاضرة أو الملف.\n\n"
            "سيتم البحث داخل موارد MEDBOT المسجلة فقط.",
            InlineKeyboardMarkup(
                [
                    [btn("❌ إلغاء", "home")],
                ]
            ),
        )
        return

    if data == "assistant":
        context.user_data["search_mode"] = False
        context.user_data["assistant_mode"] = "unified"
        await edit_safe(
            query,
            "🤖 *مساعد MEDBOT*\n\n"
            f"{ASSISTANT_PURPOSE}\n\n"
            "اكتب سؤالك (طبي أو عام) أو اسم مورد، وسأجيب من مصادر موثوقة "
            "وأضع لك زراً للوصول المباشر إلى ما وجدته.\n\n"
            "✍️ اكتب سؤالك الآن.",
            InlineKeyboardMarkup(
                [
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    # Legacy gateway buttons (older messages still carry them): the two
    # former modes are now a single unified assistant.
    if data in ("assistant_search", "assistant_medical", "assistant_start"):
        context.user_data["search_mode"] = False
        context.user_data["assistant_mode"] = "unified"
        await edit_safe(
            query,
            "🤖 *مساعد MEDBOT*\n\n"
            "اسأل عن أي موضوع طبي أو عام، أو اكتب اسم مورد للوصول إليه "
            "داخل المنصة.\n\n"
            "✍️ اكتب سؤالك أو اسم المورد الآن.",
            InlineKeyboardMarkup(
                [
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    if data == "account":
        await show_account(query)
        return

    if data == "about":
        await show_about(query)
        return

    if data == "my_contributions":
        _clear_contribution_state(context)
        await show_my_contributions(query)
        return

    if data.startswith("resubmit:"):
        try:
            contribution_id = int(data.split(":", 1)[1])
            await begin_resubmission(query, context, contribution_id)
        except Exception:
            logger.exception("Resubmission start failed")
        return

    if data == "contribute":
        _clear_contribution_state(context)
        await show_contribute(query)
        return

    if data.startswith("contrib_browse:"):
        try:
            parent_id = int(data.split(":", 1)[1])
        except Exception:
            parent_id = 0
        await show_contribute(query, parent_id)
        return

    if data.startswith("contrib_folder:"):
        try:
            folder_id = int(data.split(":", 1)[1])
            await select_contribution_folder(query, context, folder_id)
        except Exception:
            await edit_safe(
                query,
                "⚠️ تعذر اختيار القسم.",
                InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
            )
        return

    if data == "admin":
        await show_admin(query)
        return

    # ---- Admin resource upload / management ------------------
    if data.startswith("admin_upload:"):
        try:
            folder_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await edit_safe(
                query,
                "⚠️ معرف القسم غير صالح.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return
        await start_admin_upload(query, context, folder_id)
        return

    if data == "admin_upload_confirm":
        await admin_upload_confirm(query, context)
        return

    if data == "admin_upload_custom_title":
        await admin_upload_custom_title(query, context)
        return

    if data.startswith("admin_file_rename:"):
        try:
            content_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await edit_safe(
                query,
                "⚠️ معرف المورد غير صالح.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return

        if not await _admin_check(query):
            await edit_safe(
                query,
                "🔒 غير مصرح.",
                InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
            )
            return

        # Capability gate (RBAC).
        if not await _require_permission(query, "can_content"):
            return

        record = await database.get_file_record(content_id)

        if not record:
            await edit_safe(
                query,
                "⚠️ المورد غير موجود.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return

        context.user_data["admin_file_rename"] = True
        context.user_data["admin_file_rename_id"] = content_id
        context.user_data["admin_file_rename_waiting"] = True
        workflow.begin(context, "admin_file_rename")

        await edit_safe(
            query,
            "✏️ *إعادة تسمية المورد*\n\n"
            f"العنوان الحالي: <b>{escape(str(record[2]))}</b>\n\n"
            "أرسل العنوان الجديد في رسالة نصية.\n"
            "يمكنك إرسال /cancel للإلغاء.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[btn("❌ إلغاء", f"admin_file:{content_id}")]]
            ),
        )
        return

    if data.startswith("admin_file_retype:"):
        try:
            content_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await edit_safe(
                query,
                "⚠️ معرف المورد غير صالح.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return

        if not await _admin_check(query):
            await edit_safe(
                query,
                "🔒 غير مصرح.",
                InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
            )
            return

        # Capability gate (RBAC).
        if not await _require_permission(query, "can_content"):
            return

        record = await database.get_file_record(content_id)

        if not record:
            await edit_safe(
                query,
                "⚠️ المورد غير موجود.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return

        await edit_safe(
            query,
            "📦 *تغيير نوع المورد*\n\n"
            f"📄 العنوان: <b>{escape(str(record[2]))}</b>\n"
            f"📎 النوع الحالي: <code>{escape(str(record[4]))}</code>\n\n"
            "اختر النوع الجديد:",
            parse_mode=ParseMode.HTML,
            reply_markup=_file_type_keyboard(content_id),
        )
        return

    if data.startswith("admin_file_settype:"):
        parts = data.split(":")
        if len(parts) != 3:
            await edit_safe(
                query,
                "⚠️ بيانات غير صالحة.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return

        try:
            content_id = int(parts[1])
        except (TypeError, ValueError):
            await edit_safe(
                query,
                "⚠️ معرف المورد غير صالح.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return

        node_type = parts[2]

        if node_type not in FILE_TYPE_KEYS:
            await edit_safe(
                query,
                "⚠️ نوع غير مدعوم.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return

        if not await _admin_check(query):
            await edit_safe(
                query,
                "🔒 غير مصرح.",
                InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
            )
            return

        # Capability gate (RBAC).
        if not await _require_permission(query, "can_content"):
            return

        record = await database.get_file_record(content_id)

        if not record:
            await edit_safe(
                query,
                "⚠️ المورد غير موجود.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return

        try:
            ok = await database.update_content_type(content_id, node_type)
        except Exception:
            logger.exception("Admin resource type change failed")
            ok = False

        if not ok:
            await edit_safe(
                query,
                "⚠️ تعذر تغيير نوع المورد.",
                InlineKeyboardMarkup(
                    [[btn("🗂 إدارة المورد", f"admin_file:{content_id}")]]
                ),
            )
            return

        await _audit(query, "content_retype", "content", content_id,
                     details=f"type={node_type}")

        await show_admin_file(query, content_id)
        return

    if data.startswith("admin_file_move_to:"):
        parts = data.split(":")
        if len(parts) != 3:
            await edit_safe(
                query,
                "⚠️ بيانات غير صالحة.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return

        try:
            await admin_file_move_to(
                query,
                context,
                int(parts[1]),
                int(parts[2]),
            )
        except (TypeError, ValueError):
            await edit_safe(
                query,
                "⚠️ معرف غير صالح.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
        return

    if data.startswith("admin_file_move:"):
        try:
            content_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await edit_safe(
                query,
                "⚠️ معرف المورد غير صالح.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return

        context.user_data["admin_file_move"] = True
        context.user_data["admin_file_move_id"] = content_id
        workflow.begin(context, "admin_file_move")
        await admin_file_move_menu(query, context)
        return

    if data.startswith("admin_file_delete:"):
        try:
            content_id = int(data.split(":", 1)[1])
            await admin_file_delete(query, context, content_id)
        except (TypeError, ValueError):
            await edit_safe(
                query,
                "⚠️ معرف المورد غير صالح.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
        return

    if data.startswith("admin_file:"):
        try:
            content_id = int(data.split(":", 1)[1])
            await show_admin_file(query, content_id)
        except (TypeError, ValueError):
            await edit_safe(
                query,
                "⚠️ معرف المورد غير صالح.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
        return

    # ---- Folder move / delete / child / type -----------------
    if data.startswith("admin_folder_move_to:"):
        parts = data.split(":")
        if len(parts) != 3:
            await edit_safe(
                query,
                "⚠️ بيانات غير صالحة.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return

        try:
            await admin_folder_move_to(
                query,
                context,
                int(parts[1]),
                int(parts[2]),
            )
        except (TypeError, ValueError):
            await edit_safe(
                query,
                "⚠️ معرف غير صالح.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
        return

    if data.startswith("admin_folder_move_browse:"):
        parts = data.split(":")
        if len(parts) != 3:
            await edit_safe(
                query,
                "⚠️ بيانات غير صالحة.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return
        if not await _admin_check(query):
            await edit_safe(
                query, "🔒 غير مصرح.",
                InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
            )
            return
        if not await _require_permission(query, "can_folders"):
            return
        try:
            folder_id = int(parts[1])
            parent_id = int(parts[2])
        except (TypeError, ValueError):
            await edit_safe(
                query,
                "⚠️ معرف غير صالح.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return
        folder = await database.get_folder(folder_id)
        if not folder:
            _clear_admin_state(context)
            await edit_safe(
                query,
                "⚠️ القسم غير موجود.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return
        context.user_data["admin_folder_move"] = True
        context.user_data["admin_folder_move_id"] = folder_id
        workflow.begin(context, "admin_folder_move")
        await _render_move_targets(query, context, folder_id, folder, parent_id)
        return

    if data.startswith("admin_folder_move:"):
        try:
            folder_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await edit_safe(
                query,
                "⚠️ معرف القسم غير صالح.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return

        context.user_data["admin_folder_move"] = True
        context.user_data["admin_folder_move_id"] = folder_id
        await admin_folder_move_menu(query, context)
        return

    if data.startswith("admin_folder_delete:"):
        try:
            folder_id = int(data.split(":", 1)[1])
            await admin_folder_delete(query, context, folder_id)
        except (TypeError, ValueError):
            await edit_safe(
                query,
                "⚠️ معرف القسم غير صالح.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
        return

    if data.startswith("admin_folder_child:"):
        try:
            parent_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await edit_safe(
                query,
                "⚠️ معرف القسم غير صالح.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return
        await select_admin_folder_parent(query, context, parent_id)
        return

    if data.startswith("admin_folder_retype_existing:"):
        try:
            folder_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await edit_safe(
                query,
                "⚠️ معرف القسم غير صالح.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return

        if not await _admin_check(query):
            await edit_safe(
                query,
                "🔒 غير مصرح.",
                InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
            )
            return

        # Capability gate (RBAC).
        if not await _require_permission(query, "can_folders"):
            return

        folder = await database.get_folder(folder_id)

        if not folder:
            await edit_safe(
                query,
                "⚠️ القسم غير موجود.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return

        context.user_data["admin_folder_retype_id"] = folder_id
        workflow.begin(context, "admin_folder_retype")

        await edit_safe(
            query,
            "📦 *تغيير نوع القسم*\n\n"
            f"📁 الاسم: <b>{escape(str(folder[2]))}</b>\n"
            f"🏷 النوع الحالي: <code>{escape(str(folder[3]))}</code>\n\n"
            "اختر النوع الجديد:",
            parse_mode=ParseMode.HTML,
            reply_markup=_folder_type_keyboard(folder_id),
        )
        return

    if data.startswith("admin_folder_settype:"):
        parts = data.split(":")
        if len(parts) != 3:
            await edit_safe(
                query,
                "⚠️ بيانات غير صالحة.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return

        try:
            folder_id = int(parts[1])
        except (TypeError, ValueError):
            await edit_safe(
                query,
                "⚠️ معرف القسم غير صالح.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return

        node_type = parts[2]

        if node_type not in FOLDER_TYPE_KEYS:
            await edit_safe(
                query,
                "⚠️ نوع غير مدعوم.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return

        if not await _admin_check(query):
            await edit_safe(
                query,
                "🔒 غير مصرح.",
                InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
            )
            return

        # Capability gate (RBAC).
        if not await _require_permission(query, "can_folders"):
            return

        folder = await database.get_folder(folder_id)

        if not folder:
            await edit_safe(
                query,
                "⚠️ القسم غير موجود.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
            return

        try:
            ok = await database.update_folder_type(folder_id, node_type)
        except Exception:
            logger.exception("Admin folder type change failed")
            ok = False

        if not ok:
            await edit_safe(
                query,
                "⚠️ تعذر تغيير نوع القسم.",
                InlineKeyboardMarkup(
                    [[btn("↩️ إدارة القسم", f"admin_folder:{folder_id}")]]
                ),
            )
            return

        await _audit(query, "folder_retype", "folder", folder_id,
                     details=f"type={node_type}")

        await show_admin_folder(query, folder_id)
        return

    if data.startswith("admin_folder:"):
        try:
            folder_id = int(data.split(":", 1)[1])
            await show_admin_folder(query, folder_id)
        except (TypeError, ValueError):
            await edit_safe(
                query,
                "⚠️ معرف القسم غير صالح.",
                InlineKeyboardMarkup([[btn("🗂 إدارة الأقسام", "admin_folders")]]),
            )
        return

    if data.startswith("admin_folder_rename:"):
        try:
            folder_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await edit_safe(
                query,
                "⚠️ معرف القسم غير صالح.",
                InlineKeyboardMarkup([
                    [btn("🗂 إدارة الأقسام", "admin_folders")]
                ]),
            )
            return

        if not await _admin_check(query):
            await edit_safe(
                query,
                "🔒 غير مصرح.",
                InlineKeyboardMarkup([
                    [btn("🏠 الرئيسية", "home")]
                ]),
            )
            return

        # Capability gate (RBAC).
        if not await _require_permission(query, "can_folders"):
            return

        folder = await database.get_folder(folder_id)

        if not folder:
            await edit_safe(
                query,
                "⚠️ القسم غير موجود.",
                InlineKeyboardMarkup([
                    [btn("🗂 إدارة الأقسام", "admin_folders")]
                ]),
            )
            return

        context.user_data["admin_folder_rename"] = True
        context.user_data["admin_folder_rename_id"] = folder_id
        workflow.begin(context, "admin_folder_rename")

        await edit_safe(
            query,
            "✏️ *إعادة تسمية القسم*\n\n"
            f"الاسم الحالي: <b>{escape(str(folder[2]))}</b>\n\n"
            "أرسل الاسم الجديد.\n"
            "يمكنك إرسال /cancel للإلغاء.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [btn("❌ إلغاء", f"admin_folder:{folder_id}")]
            ]),
        )
        return

    if data.startswith("admin_folder_toggle:"):
        try:
            folder_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await edit_safe(
                query,
                "⚠️ معرف القسم غير صالح.",
                InlineKeyboardMarkup([
                    [btn("🗂 إدارة الأقسام", "admin_folders")]
                ]),
            )
            return

        if not await _admin_check(query):
            await edit_safe(
                query,
                "🔒 غير مصرح.",
                InlineKeyboardMarkup([
                    [btn("🏠 الرئيسية", "home")]
                ]),
            )
            return

        # Capability gate (RBAC).
        if not await _require_permission(query, "can_folders"):
            return

        folder = await database.get_folder(folder_id)

        if not folder:
            await edit_safe(
                query,
                "⚠️ القسم غير موجود.",
                InlineKeyboardMarkup([
                    [btn("🗂 إدارة الأقسام", "admin_folders")]
                ]),
            )
            return

        current = int(folder[4] or 0)
        new_value = 0 if current else 1

        try:
            await database.update_folder_accepts_contributions(
                folder_id,
                new_value,
            )
        except Exception:
            logger.exception("Admin folder contribution toggle failed")
            await edit_safe(
                query,
                "⚠️ تعذر تغيير حالة استقبال المساهمات.",
                InlineKeyboardMarkup([
                    [btn("↩️ العودة إلى القسم", f"admin_folder:{folder_id}")]
                ]),
            )
            return

        await _audit(query, "folder_toggle", "folder", folder_id,
                     details=f"accepts={new_value}")

        await show_admin_folder(query, folder_id)
        return

    if data == "admin_folders":
        await show_admin_folders(query)
        return

    if data == "admin_folder_create":
        await start_admin_folder_create(query, context)
        return

    if data.startswith("admin_folder_parent:"):
        try:
            parent_id = int(data.split(":", 1)[1])
            await show_admin_folder_parents(query, parent_id)
        except Exception:
            await edit_safe(
                query,
                "⚠️ تعذر فتح شجرة الأقسام.",
                InlineKeyboardMarkup(
                    [
                        [btn("🗂 إدارة الأقسام", "admin_folders")],
                        [btn("🏠 الرئيسية", "home")],
                    ]
                ),
            )
        return

    if data.startswith("admin_folder_select_parent:"):
        try:
            parent_id = int(data.split(":", 1)[1])
            await select_admin_folder_parent(query, context, parent_id)
        except Exception:
            await edit_safe(
                query,
                "⚠️ تعذر اختيار القسم الأب.",
                InlineKeyboardMarkup(
                    [
                        [btn("🗂 إدارة الأقسام", "admin_folders")],
                    ]
                ),
            )
        return

    if data == "admin_folder_retype":
        await show_admin_folder_types(query, context)
        return

    if data.startswith("admin_folder_type:"):
        node_type = data.split(":", 1)[1]
        await admin_folder_type(query, context, node_type)
        return

    if data.startswith("admin_folder_accepts:"):
        try:
            accepts = int(data.split(":", 1)[1])
            if accepts not in (0, 1):
                raise ValueError
            await finish_admin_folder_create(query, context, accepts)
        except Exception:
            await edit_safe(
                query,
                "⚠️ تعذر إكمال إنشاء القسم.",
                InlineKeyboardMarkup(
                    [
                        [btn("🗂 إدارة الأقسام", "admin_folders")],
                    ]
                ),
            )
        return

    if data == "admin_pending":
        await show_pending(query, context)
        return

    if data.startswith("review:"):
        try:
            contribution_id = int(data.split(":", 1)[1])
            await review_contribution(query, context, contribution_id)
        except Exception:
            pass
        return

    if data.startswith("preview:"):
        try:
            contribution_id = int(data.split(":", 1)[1])
            await preview_contribution(query, context, contribution_id)
        except (TypeError, ValueError):
            await edit_safe(
                query,
                i18n.t("invalid_id"),
                InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
            )
        return

    if data.startswith("approve:"):
        try:
            contribution_id = int(data.split(":", 1)[1])
            await process_approval(query, contribution_id, True)
        except Exception:
            logger.exception("approve callback failed: %s", data)
        return

    if data.startswith("reject:"):
        try:
            contribution_id = int(data.split(":", 1)[1])
            await request_review_note(query, context, contribution_id, "reject")
        except Exception:
            logger.exception("reject callback failed: %s", data)
        return

    if data.startswith("revise:"):
        try:
            contribution_id = int(data.split(":", 1)[1])
            await request_review_note(query, context, contribution_id, "revise")
        except Exception:
            logger.exception("revise callback failed: %s", data)
        return

    if data == "admin_ai":
        await show_ai_registry(query)
        return

    if data == "admin_runtime":
        await show_runtime(query)
        return

    # No handler claimed this callback. Surface it instead of ending in
    # silence, so an unmapped button is visible to the user and in the logs.
    logger.warning("Unhandled callback_data: %r", data)
    await edit_safe(
        query,
        "⚠️ هذا الزر غير مدعوم حالياً.",
        InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
    )


# ============================================================
# AI ASSISTANT
# ============================================================


async def quota_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    await database.register_user(
        update.effective_user.id,
        update.effective_user.username,
        update.effective_user.full_name,
    )

    remaining = await database.get_remaining_quota(
        user_id,
        max_limit=DAILY_LIMIT,
    )

    if remaining > 0:
        message = "✅ يمكنك استخدام المساعد الآن."
    else:
        message = _quota_reset_text()

    await send_safe_message(
        update,
        message,
        await home_for(update),
    )


async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the caller's Telegram ID and current authorization status.

    This does not grant any privilege. It exists so the configured owner can
    discover their numeric ID to place in ADMIN_ID.
    """
    user = update.effective_user

    await database.register_user(user.id, user.username, user.full_name)

    try:
        is_admin = await database.is_user_admin(user.id)
    except Exception:
        is_admin = False

    configured = configured_admin_id()

    if configured == 0:
        status = (
            "ADMIN_ID غير مُهيّأ في البيئة.\n"
            "لن يُرقّى أي مستخدم تلقائياً، حتى أول مستخدم."
        )
    elif configured == user.id:
        status = f"أنت المالك المُهيّأ (ADMIN_ID={configured})."
    else:
        status = f"ADMIN_ID مُهيّأ لمُعرّف آخر ({configured})."

    await send_safe_message(
        update,
        f"🆔 *مُعرّف Telegram الخاص بك:* `{user.id}`\n\n"
        f"🔐 صلاحية مشرف في MEDBOT: "
        f"{'نعم' if is_admin else 'لا'}\n\n"
        f"{status}",
        await home_for(update),
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel any active admin workflow."""
    if not update.message:
        return

    active = any(
        context.user_data.get(key)
        for key in (
            "admin_folder_create",
            "admin_folder_rename",
            "admin_folder_retype_id",
            "admin_folder_move",
            "admin_upload",
            "admin_upload_waiting_title",
            "admin_file_rename",
            "admin_file_move",
        )
    )

    review_active = bool(
        context.user_data.get("review_note_kind")
        or context.user_data.get("contribution_folder")
        or context.user_data.get("contribution_resubmit_id")
    )

    contact_active = bool(
        context.user_data.get("contact_category")
        or context.user_data.get("contact_reply_id")
    )

    other_active = bool(
        context.user_data.get("settings_edit_key")
        or context.user_data.get("topics_create")
        or context.user_data.get("topics_link_id")
        or context.user_data.get("notifications_body")
    )

    _clear_admin_state(context)
    _clear_review_state(context)
    _clear_contribution_state(context)
    messaging._clear_contact_state(context)
    for key in (
        "settings_edit_key",
        "topics_create",
        "topics_link_id",
        "notifications_body",
    ):
        context.user_data.pop(key, None)

    if active or review_active or contact_active or other_active:
        await update.message.reply_text(
            "❌ تم إلغاء العملية الجارية.",
            reply_markup=await home_for(update),
        )
        return

    await update.message.reply_text(
        "ℹ️ لا توجد عملية قيد التنفيذ.",
        reply_markup=await home_for(update),
    )


def _assistant_action_rows(actions):
    """One-button-per-row direct-access keyboard for matched resources.

    Labels are plain text (no markdown) so a ')' inside a resource name can
    never break the send; the callback opens the exact item.
    """
    rows = []

    for action in actions or []:
        callback = action.get("callback")
        label = (action.get("label") or "").strip()
        if not callback or not label:
            continue
        rows.append([InlineKeyboardButton(label, callback_data=callback)])

    return rows


async def ai_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    user = update.effective_user

    await database.register_user(
        user.id,
        user.username,
        user.full_name,
    )

    query = update.message.text.strip()

    # Typed rejection reason / revision note takes absolute priority.
    if await process_review_text(update, context):
        return

    # Contact Admin captures: student message body / admin reply.
    if await messaging.handle_contact_text(update, context):
        return

    if await messaging.handle_admin_reply_text(update, context):
        return

    # Owner adding a sub-admin by ID/@username.
    if await admin_management.handle_add_admin_text(update, context):
        return

    # Platform Settings: admin typing a new setting value.
    if await platform_settings.handle_settings_text(update, context):
        return

    # Search Topics: admin typing a topic name / folder id.
    if await topics.handle_topics_text(update, context):
        return

    # Notifications: admin typing a broadcast body.
    if await notifications.handle_notification_text(update, context):
        return

    # Custom title input (upload / resource rename) has the highest priority.
    if context.user_data.get("admin_upload_waiting_title") or (
        context.user_data.get("admin_file_rename")
        and context.user_data.get("admin_file_rename_waiting")
    ):
        handled = await handle_pending_title_input(update, context)
        if handled:
            return

    # Admin upload awaiting media: text input should not fall through to AI.
    if context.user_data.get("admin_upload"):
        folder_id = context.user_data.get("admin_upload_folder")
        await update.message.reply_text(
            "📤 أنت في وضع رفع مورد.\n\n"
            "أرسل المورد الآن كـ Document / Photo / Audio / Video، "
            "أو اضغط ❌ إلغاء / أرسل /cancel للخروج.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [btn("❌ إلغاء", f"admin_folder:{folder_id}")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    # Admin folder rename state has priority over search/AI.
    if context.user_data.get("admin_folder_rename"):
        handled = await request_admin_folder_rename(update, context)
        if handled:
            return

    # Admin folder creation state has priority over search/AI.
    if context.user_data.get("admin_folder_create"):
        handled = await request_admin_folder_name(update, context)
        if handled:
            return

    # A hidden Assistant feature must not be reachable by typing: the admin
    # workflows above already had their chance, so this only affects students.
    if await _feature_offline_notice(update, "assistant"):
        return

    # Legacy standalone search mode (the /search command) still resolves
    # deterministically; the unified assistant supersedes the two former
    # assistant modes.
    if context.user_data.get("search_mode"):
        context.user_data["search_mode"] = False
        context.user_data["assistant_mode"] = None
        await run_search(update, query)
        return

    # Explicit /ask always means the unified assistant.
    if query.startswith("/ask"):
        query = query.replace("/ask", "", 1).strip()
        context.user_data["assistant_mode"] = "unified"

    assistant_mode = context.user_data.get("assistant_mode")

    # The student is not inside the assistant: do not silently guess intent,
    # just point them at the single assistant entry point.
    if assistant_mode != "unified":
        await update.message.reply_text(
            "🤖 *مساعد MEDBOT*\n\n"
            "اضغط الزر بالأسفل ثم اكتب سؤالك أو اسم المورد.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [
                    [btn("🤖 فتح مساعد MEDBOT", "assistant")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    if not query:
        await update.message.reply_text(
            "يرجى كتابة السؤال بعد الأمر مباشرة.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    allowed, remaining = await database.check_and_increment_quota(
        user.id,
        max_limit=DAILY_LIMIT,
    )

    if not allowed:
        await update.message.reply_text(
            _quota_reset_text(),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=await home_for(update),
        )
        return

    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.TYPING,
        )
    except Exception:
        pass

    try:
        ai_result = await generate_medbot_unified_result(query, user_id=user.id)
    except Exception:
        logger.exception("AI request failed")
        ai_result = {
            "text": (
                "⚠️ تعذر الوصول إلى خدمة الذكاء الاصطناعي حالياً.\n\n"
                "لم يتم إنشاء إجابة غير مؤكدة."
            ),
            "actions": [],
        }

    if not isinstance(ai_result, dict):
        ai_result = {"text": str(ai_result), "actions": []}

    ai_answer = ai_result.get("text", "")
    action_rows = _assistant_action_rows(ai_result.get("actions"))

    # No balance counter is shown. The student is only told when the daily
    # allowance is used up: on the last allowed request, and on the next one.
    if remaining <= 0:
        ai_answer = f"{ai_answer}\n\n—\n{_quota_reset_text()}"

    reply_markup = InlineKeyboardMarkup(
        action_rows + [[btn("🏠 الرئيسية", "home")]]
    )

    await send_safe_message(update, ai_answer, reply_markup)


# ============================================================
# MEDIA / CONTRIBUTION ROUTING
# ============================================================


async def media_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin resource upload has priority over student contributions.
    handled = await admin_upload_media_handler(update, context)

    if handled:
        return

    # A hidden Contributions feature must not accept uploads either.
    if (
        context.user_data.get("contribution_folder")
        or context.user_data.get("contribution_resubmit_id")
    ):
        if await _feature_offline_notice(update, "contributions"):
            _clear_contribution_state(context)
            return

    handled = await contribution_media_handler(update, context)

    if handled:
        return

    # Unrecognized media with no active state: never crash, never corrupt.
    await update.message.reply_text(
        "📚 إذا كنت تريد إرسال مساهمة، افتح:\n"
        "📤 Student Contributions\n\n"
        "أو استخدم /start للعودة إلى الرئيسية.",
        reply_markup=await home_for(update),
    )


# ============================================================
# ERRORS / STARTUP
# ============================================================


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Surface every handler failure in the Runtime Logs.

    Logged with a traceback (not just the message) so a swallowed callback
    exception is diagnosable from the deployment logs. For callback updates it
    also tells the user something went wrong instead of leaving the tap
    silently unanswered.
    """
    logger.error(
        "MEDBOT handler error while processing %s",
        type(update).__name__,
        exc_info=context.error,
    )

    query = getattr(update, "callback_query", None)
    if query is not None:
        try:
            await query.answer(
                "⚠️ حدث خطأ غير متوقع. حاول مرة أخرى.",
                show_alert=True,
            )
        except Exception:
            logger.warning("Failed to answer callback after error", exc_info=True)


async def post_init(application: Application):
    await database.init_db()

    admin_id = configured_admin_id()

    if admin_id:
        try:
            existed = await database.is_user_admin(admin_id)
        except Exception:
            existed = True

        granted = await database.ensure_configured_admin(admin_id)

        if granted:
            logger.info(
                "Configured ADMIN_ID=%s ensured as MEDBOT admin", admin_id
            )
            if not existed:
                # Audit only the first promotion, not every restart.
                await audit.log_action(
                    admin_id,
                    "owner_bootstrap",
                    target_type="admin",
                    target_id=admin_id,
                    details="configured ADMIN_ID",
                )
    else:
        logger.warning(
            "ADMIN_ID is not configured. "
            "No admin was promoted; set ADMIN_ID in the environment."
        )

    # Warm the AI discovery/probe cache so the first assistant reply does not
    # pay for provider discovery. Runs in the background and is best-effort.
    asyncio.create_task(warm_ai_pool())


def main():
    if not BOT_TOKEN:
        print("خطأ: لم يتم العثور على BOT_TOKEN!")
        return

    request_config = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request_config)
        .post_init(post_init)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("quota", quota_command))
    app.add_handler(CommandHandler("whoami", whoami_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("ask", ai_handler))

    # Contact Admin messaging (isolated module). Must register before the
    # catch-all inline UI handler so its `msg_*`/`contact` callbacks win.
    messaging.register_messaging_handlers(app)
    audit.register_audit_handlers(app)
    admin_management.register_admin_management_handlers(app)
    platform_settings.register_platform_settings_handlers(app)
    topics.register_topics_handlers(app)
    notifications.register_notifications_handlers(app)
    visibility.register_visibility_handlers(app)

    # Inline UI
    app.add_handler(CallbackQueryHandler(callback_router))

    # Uploaded media / student contributions
    app.add_handler(
        MessageHandler(
            filters.Document.ALL
            | filters.PHOTO
            | filters.AUDIO
            | filters.VIDEO
            | filters.VOICE,
            media_router,
        )
    )

    # Text / AI / search
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            ai_handler,
        )
    )

    app.add_error_handler(error_handler)

    print("============================================================")
    print("MEDBOT")
    print("============================================================")
    print("MENU=ENABLED")
    print("LIBRARY=ENABLED")
    print("SEARCH=ENABLED")
    print("ASSISTANT=ENABLED")
    print("CONTRIBUTIONS=ENABLED")
    print("ADMIN=ENABLED")
    print("ACCOUNT=ENABLED")
    print("ABOUT=ENABLED")
    print("TELEGRAM_POLLING=STARTING")
    print("============================================================")

    # Explicitly request every update type. Telegram persists the last
    # `allowed_updates` it was given ("If not specified, the previous setting
    # will be used"), so a stale restricted filter from an earlier run or
    # webhook silently drops `callback_query` updates: /start works, but
    # button presses never reach the bot. Naming the full set on every boot
    # makes that stuck state impossible.
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=polling_allowed_updates(),
    )


if __name__ == "__main__":
    main()
