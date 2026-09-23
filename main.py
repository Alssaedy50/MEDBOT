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
from ai import (
    generate_medical_ai_response,
    generate_medbot_assistant_response,
    NOT_REGISTERED_MESSAGE,
)
from search_engine import search_library_summary

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DAILY_LIMIT = 20


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
    """Send long messages safely."""
    if not text:
        text = "لا توجد بيانات متاحة حالياً."

    message = update.effective_message
    if not message:
        return

    chunk_size = 4000
    chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]

    for index, chunk in enumerate(chunks):
        for attempt in range(2):
            try:
                await message.reply_text(
                    chunk,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup if index == len(chunks) - 1 else None,
                )
                break
            except Exception:
                try:
                    await message.reply_text(
                        chunk,
                        parse_mode=None,
                        reply_markup=reply_markup if index == len(chunks) - 1 else None,
                    )
                    break
                except Exception as exc:
                    if attempt == 1:
                        logger.error("Failed to send message: %s", exc)
                    await asyncio.sleep(1)


async def edit_safe(query, text, reply_markup=None, parse_mode=ParseMode.MARKDOWN):
    """Safely edit an inline message.

    Falls back to plain text if the requested parse mode fails, so a
    malformed entity can never leave the caller stuck on a stale screen.
    """
    for mode in (parse_mode, None):
        try:
            await query.edit_message_text(
                text=text,
                parse_mode=mode,
                reply_markup=reply_markup,
            )
            return
        except Exception as exc:
            logger.warning("Failed to edit callback message: %s", exc)


def btn(text, callback):
    return InlineKeyboardButton(text, callback_data=callback)


# ============================================================
# MAIN HOME
# ============================================================


def home_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                btn("📚 MEDBOT Resources", "library:0"),
            ],
            [
                btn("🤖 MEDBOT Assistant", "assistant"),
                btn("📤 Student Contributions", "contribute"),
            ],
            [
                btn("📄 مساهماتي", "my_contributions"),
                btn("📊 My Account", "account"),
            ],
            [
                btn("📬 Contact Admin", "contact"),
                btn("ℹ️ About MEDBOT", "about"),
            ],
            [
                btn("🛠 Admin Panel", "admin"),
            ],
        ]
    )


async def home_keyboard_for(user_id):
    """Home keyboard, adding an owner-only audit shortcut.

    The audit viewer is reachable from the Admin Panel too; this surfaces it
    directly for the configured owner without changing the base keyboard.
    """
    try:
        is_owner = await database.is_owner(user_id)
    except Exception:
        is_owner = False

    if not is_owner:
        return home_keyboard()

    return InlineKeyboardMarkup(
        [
            [btn("📚 MEDBOT Resources", "library:0")],
            [
                btn("🤖 MEDBOT Assistant", "assistant"),
                btn("📤 Student Contributions", "contribute"),
            ],
            [
                btn("📄 مساهماتي", "my_contributions"),
                btn("📊 My Account", "account"),
            ],
            [
                btn("📬 Contact Admin", "contact"),
                btn("ℹ️ About MEDBOT", "about"),
            ],
            [
                btn("🛠 Admin Panel", "admin"),
                btn("📜 سجل التدقيق", "audit_log"),
            ],
        ]
    )


async def show_home(update: Update):
    user = update.effective_user

    await database.register_user(
        user.id,
        user.username,
        user.full_name,
    )

    remaining = await database.get_remaining_quota(
        user.id,
        max_limit=DAILY_LIMIT,
    )

    text = (
        f"🩺 *MEDBOT*\n\n"
        f"مرحباً بك دكتور {user.first_name}.\n\n"
        "منصة أكاديمية طبية تساعدك على الوصول إلى مكتبة MEDBOT "
        "والبحث في الموارد المسجلة واستخدام المساعد الذكي ضمن محتوى MEDBOT.\n\n"
        f"📊 *رصيد الذكاء الاصطناعي اليوم:* {remaining}/{DAILY_LIMIT}\n\n"
        "اختر الخدمة التي تريد استخدامها:"
    )

    if update.callback_query:
        await edit_safe(
            update.callback_query,
            text,
            await home_keyboard_for(user.id),
        )
    else:
        await send_safe_message(
            update,
            text,
            await home_keyboard_for(user.id),
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


def folder_keyboard(folders, parent_id=0):
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
        rows.append([btn("⬅️ رجوع", f"library:{parent_id}")])

    rows.append([btn("🏠 الرئيسية", "home")])

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

    if parent_id == 0:
        title = (
            "📚 *MEDBOT Resources*\n\n"
            "اختر السنة أو القسم الذي تريد الدخول إليه:"
        )
    else:
        try:
            breadcrumb = await database.get_breadcrumbs(parent_id)
        except Exception:
            breadcrumb = "📚 MEDBOT Resources"

        title = (
            "📚 *MEDBOT Resources*\n\n"
            f"📍 {breadcrumb}\n\n"
            "اختر القسم:"
        )

    if not folders:
        title += "\n\nℹ️ لا توجد أقسام مسجلة في هذا المستوى حالياً."

    await edit_safe(
        query,
        title,
        folder_keyboard(folders, parent_id),
    )


async def show_folder(query, folder_id):
    try:
        folders = await database.get_folders(folder_id)
        files = await database.get_files(folder_id)
        parent_id = await database.get_parent_id(folder_id)
        breadcrumb = await database.get_breadcrumbs(folder_id)
        folder = await database.get_folder(folder_id)
    except Exception as exc:
        logger.exception("Folder loading failed")
        await edit_safe(
            query,
            "⚠️ تعذر تحميل محتوى القسم حالياً.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
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

    if not rows:
        body = (
            f"📂 *{str(folder_name)[:80]}*\n\n"
            f"📍 {breadcrumb}\n\n"
            "ℹ️ لا توجد أقسام أو موارد مسجلة هنا حالياً."
        )
    else:
        body = (
            f"📂 *{str(folder_name)[:80]}*\n\n"
            f"📍 {breadcrumb}\n\n"
            "اختر القسم أو المورد:"
        )

    # Correct parent-aware navigation.
    rows.append([btn("⬅️ رجوع", f"library:{parent_id or 0}")])
    rows.append([btn("🏠 الرئيسية", "home")])

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

    try:
        await _send_registered_media(context, chat_id, file_id, file_type, title)

        await context.bot.send_message(
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


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            reply_markup=home_keyboard(),
        )
        return

    if not results:
        await update.message.reply_text(
            "🔎 *نتيجة البحث*\n\n"
            "المورد المطلوب غير مسجل حالياً في MEDBOT.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=home_keyboard(),
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

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def show_account(query):
    user = query.from_user

    try:
        remaining = await database.get_remaining_quota(
            user.id,
            max_limit=DAILY_LIMIT,
        )
    except Exception:
        remaining = "غير متاح"

    text = (
        "📊 *My Account*\n\n"
        f"👤 الاسم: {user.full_name}\n"
        f"🆔 Telegram ID: `{user.id}`\n\n"
        f"🤖 رصيد AI اليومي: {remaining}/{DAILY_LIMIT}\n\n"
        "يتم تجديد الرصيد تلقائياً مع بداية يوم جديد."
    )

    await edit_safe(
        query,
        text,
        InlineKeyboardMarkup(
            [
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


async def show_about(query):
    try:
        about = await database.get_about_us()
    except Exception:
        about = None

    if not about:
        about = (
            "MEDBOT هو نظام أكاديمي طبي يعمل من خلال Telegram "
            "لتنظيم والوصول إلى الموارد التعليمية الطبية المسجلة."
        )

    await edit_safe(
        query,
        f"ℹ️ *About MEDBOT*\n\n{about}",
        InlineKeyboardMarkup(
            [
                [btn("🏠 الرئيسية", "home")],
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
        except Exception:
            continue

        label = CONTRIBUTION_STATUS_LABELS.get(status, status)
        lines.append(f"🆔 `{contribution_id}` — {label}\n📄 {title}")

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


async def contribution_folders():
    db = await database.get_db()

    try:
        sql = """
            SELECT id, name, node_type
            FROM folders
            WHERE accepts_contributions = 1
            ORDER BY id ASC
        """

        async with db.execute(sql) as cursor:
            return await cursor.fetchall()
    finally:
        await db.close()


async def show_contribute(query):
    try:
        folders = await contribution_folders()
    except Exception:
        folders = []

    buttons = []

    for folder in folders:
        folder_id, name, node_type = folder
        buttons.append(
            [
                btn(
                    f"📤 {str(name)[:35]}",
                    f"contrib_folder:{folder_id}",
                )
            ]
        )

    if not buttons:
        text = (
            "📤 *Student Contributions*\n\n"
            "لا توجد حالياً مجلدات مفتوحة لاستقبال مساهمات الطلاب."
        )
    else:
        text = (
            "📤 *Student Contributions*\n\n" "اختر القسم الذي تريد إرسال المورد إليه:"
        )

    buttons.append([btn("🏠 الرئيسية", "home")])

    await edit_safe(
        query,
        text,
        InlineKeyboardMarkup(buttons),
    )


async def select_contribution_folder(query, context, folder_id):
    try:
        accepts = await database.folder_accepts_contributions(folder_id)
    except Exception:
        accepts = False

    if not accepts:
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
        return

    context.user_data["contribution_folder"] = folder_id

    await edit_safe(
        query,
        "📤 *إرسال مساهمة*\n\n"
        "تم اختيار القسم.\n\n"
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
        admins = await database.get_all_admins()
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
                reply_markup=home_keyboard(),
            )
            return True

        _clear_contribution_state(context)

        await update.message.reply_text(
            "✅ *تم استلام التعديل وإعادة إرسال المساهمة.*\n\n"
            f"رقم المساهمة: `{resubmit_id}`\n"
            "الحالة الحالية: `pending`\n\n"
            "ستتم مراجعتها من جديد.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=home_keyboard(),
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
            reply_markup=home_keyboard(),
        )
        return True
    except Exception:
        logger.exception("Contribution failed")
        await update.message.reply_text(
            "⚠️ تعذر تسجيل المساهمة حالياً. لم يتم تأكيد نجاح الإضافة.",
            reply_markup=home_keyboard(),
        )
        return True

    _clear_contribution_state(context)

    await update.message.reply_text(
        "✅ *تم استلام مساهمتك بنجاح.*\n\n"
        f"رقم المساهمة: `{contribution_id}`\n"
        "الحالة الحالية: `pending`\n\n"
        "سيتمكن المشرفون من مراجعتها قبل نشرها في المكتبة.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=home_keyboard(),
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
        "اختر الإجراء المطلوب:",
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

        rows.append(
            [
                btn(
                    f"{resource_icon(node_type)} {str(name)[:35]}",
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
        "اختر قسماً لإدارته، أو أنشئ قسماً جديداً.\n"
        "يمكنك أيضاً الدخول إلى قسم لإضافة قسم فرعي بداخله.",
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

    try:
        is_admin = await database.is_user_admin(update.effective_user.id)
    except Exception:
        is_admin = False

    if not is_admin:
        context.user_data.pop("admin_folder_rename", None)
        context.user_data.pop("admin_folder_rename_id", None)
        await update.message.reply_text(
            "🔒 غير مصرح.",
            reply_markup=home_keyboard(),
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
                reply_markup=home_keyboard(),
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
            reply_markup=home_keyboard(),
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
            reply_markup=home_keyboard(),
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

    try:
        is_admin = await database.is_user_admin(update.effective_user.id)
    except Exception:
        is_admin = False

    if not is_admin:
        context.user_data.pop("admin_folder_create", None)
        await update.message.reply_text(
            "🔒 غير مصرح.",
            reply_markup=home_keyboard(),
        )
        return True

    name = update.message.text.strip()

    if name == "/cancel":
        context.user_data.pop("admin_folder_create", None)
        await update.message.reply_text(
            "❌ تم إلغاء إنشاء القسم.",
            reply_markup=home_keyboard(),
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

    await _audit(query, "folder_create", "folder",
                 details=f"name={name}, type={node_type}, parent={parent_id}")

    await edit_safe(
        query,
        "✅ <b>تم إنشاء القسم بنجاح.</b>\n\n"
        f"📁 الاسم: <b>{escape(name)}</b>\n"
        f"🧩 النوع: <code>{escape(str(node_type))}</code>\n"
        f"📤 استقبال المساهمات: {'نعم' if int(accepts) else 'لا'}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [btn("➕ إنشاء قسم آخر", "admin_folder_create")],
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
            reply_markup=home_keyboard(),
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
            reply_markup=home_keyboard(),
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
            reply_markup=home_keyboard(),
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
    """Show target folders for moving a folder."""
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

    rows = [[btn("🏠 نقل إلى الجذر", f"admin_folder_move_to:{folder_id}:0")]]

    try:
        roots = await database.get_folders(0)
    except Exception:
        roots = []

    for item in roots:
        try:
            target_id, name, _node_type, _acc = item[:4]
        except Exception:
            continue

        if int(target_id) == folder_id:
            continue

        rows.append(
            [
                btn(
                    f"📁 {str(name)[:35]}",
                    f"admin_folder_move_to:{folder_id}:{target_id}",
                )
            ]
        )

    rows.append([btn("❌ إلغاء", f"admin_folder:{folder_id}")])

    await edit_safe(
        query,
        "🚚 *نقل القسم*\n\n"
        f"📁 القسم: <b>{escape(str(folder[2]))}</b>\n\n"
        "اختر القسم الهدف (الجذر أو قسم رئيسي):",
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

    try:
        is_admin = await database.is_user_admin(update.effective_user.id)
    except Exception:
        is_admin = False

    if not is_admin:
        _clear_admin_state(context)
        await update.message.reply_text(
            "🔒 غير مصرح.",
            reply_markup=home_keyboard(),
        )
        return True

    text = update.message.text.strip()

    if text == "/cancel":
        _clear_admin_state(context)
        await update.message.reply_text(
            "❌ تم إلغاء العملية.",
            reply_markup=home_keyboard(),
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
                reply_markup=home_keyboard(),
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
            reply_markup=home_keyboard(),
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
            reply_markup=home_keyboard(),
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
            reply_markup=home_keyboard(),
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

    rows = [
        [btn("🗂 إدارة الأقسام والفروع", "admin_folders")],
        [btn("📥 مراجعة المساهمات", "admin_pending")],
        [btn("📬 رسائل الطلاب", "admin_messages")],
        [btn("🤖 AI Registry", "admin_ai")],
        [btn("📊 Runtime", "admin_runtime")],
    ]

    try:
        can_view_audit = await database.is_owner(user_id) or await database.user_has_permission(
            user_id, "can_admins"
        )
    except Exception:
        can_view_audit = False

    if can_view_audit:
        rows.append([btn("📜 سجل التدقيق", "audit_log")])

    try:
        can_manage_admins = await database.is_owner(user_id) or await database.user_has_permission(
            user_id, "can_admins"
        )
    except Exception:
        can_manage_admins = False

    if can_manage_admins:
        rows.append([btn("👥 إدارة المشرفين", "amg_list")])

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

    await edit_safe(
        query,
        text,
        InlineKeyboardMarkup(
            [
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

    try:
        is_admin = await database.is_user_admin(update.effective_user.id)
    except Exception:
        is_admin = False

    if not is_admin:
        _clear_review_state(context)
        await update.message.reply_text(
            "🔒 غير مصرح.",
            reply_markup=home_keyboard(),
        )
        return True

    text = (update.message.text or "").strip()

    if text == "/cancel":
        _clear_review_state(context)
        await update.message.reply_text(
            "❌ تم إلغاء المراجعة.",
            reply_markup=home_keyboard(),
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
                reply_markup=home_keyboard(),
            )
            return True

        contributor_id, title = result[0], result[1]

        await update.message.reply_text(
            f"✅ تم رفض المساهمة رقم `{contribution_id}`.\n"
            "تم إرسال سبب الرفض إلى صاحب المساهمة.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=home_keyboard(),
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
            reply_markup=home_keyboard(),
        )
        return True

    contributor_id = result[0]

    await update.message.reply_text(
        f"✅ تم طلب تعديل المساهمة رقم `{contribution_id}`.\n"
        "تم إرسال الملاحظات إلى صاحب المساهمة.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=home_keyboard(),
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


async def show_ai_registry(query):
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

    if not await _require_permission(query, "can_ai"):
        return

    try:
        rows = await database.ai_registry_get_all()
    except Exception:
        rows = []

    if not rows:
        text = "🤖 *AI Registry*\n\nلا توجد نماذج مسجلة حالياً."
    else:
        lines = ["🤖 *AI Registry*\n"]

        for row in rows:
            try:
                provider = row[1]
                model = row[2]
                availability = row[4]
                auth_status = row[5]

                lines.append(
                    f"• *{provider}* — `{model or 'N/A'}`\n"
                    f"  availability: `{availability or 'N/A'}`\n"
                    f"  auth: `{auth_status or 'N/A'}`"
                )
            except Exception:
                continue

        text = "\n".join(lines)

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


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    try:
        await query.answer()
    except Exception:
        pass

    data = query.data or ""

    if data == "home":
        context.user_data["search_mode"] = False
        context.user_data["assistant_mode"] = None
        _clear_admin_state(context)
        _clear_review_state(context)
        _clear_contribution_state(context)
        messaging._clear_contact_state(context)
        await show_home(update)
        return

    if data == "noop":
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
        context.user_data["assistant_mode"] = None
        await edit_safe(
            query,
            "🤖 *MEDBOT Assistant*\n\n"
            "اختر نوع المساعدة التي تريد استخدامها:\n\n"
            "📚 *البحث داخل MEDBOT*\n"
            "ابحث في الكتب والمحاضرات والملخصات وMCQs "
            "والملفات المسجلة داخل المنصة فقط.\n\n"
            "🩺 *المساعد الطبي العام*\n"
            "اسأل عن أي موضوع طبي للدراسة والشرح والفهم.",
            InlineKeyboardMarkup(
                [
                    [btn("📚 البحث داخل MEDBOT", "assistant_search")],
                    [btn("🩺 المساعد الطبي العام", "assistant_medical")],
                    [btn("📊 Quota", "account")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    if data == "assistant_search":
        context.user_data["search_mode"] = True
        context.user_data["assistant_mode"] = "resource"
        await edit_safe(
            query,
            "📚 *البحث داخل MEDBOT*\n\n"
            "اكتب اسم الكتاب أو المحاضرة أو الملف أو الموضوع "
            "الذي تريد البحث عنه.\n\n"
            "🔒 سيتم البحث فقط داخل الموارد المسجلة في MEDBOT.",
            InlineKeyboardMarkup(
                [
                    [btn("🤖 العودة للمساعد", "assistant")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    if data == "assistant_medical":
        context.user_data["search_mode"] = False
        context.user_data["assistant_mode"] = "medical"
        await edit_safe(
            query,
            "🩺 *المساعد الطبي العام*\n\n"
            "اكتب سؤالك الطبي الآن.\n\n"
            "يمكنك طلب شرح المفاهيم الطبية، المقارنات، "
            "الآليات المرضية، الفسيولوجيا، التشريح وغيرها.",
            InlineKeyboardMarkup(
                [
                    [btn("📚 بحث داخل MEDBOT", "assistant_search")],
                    [btn("📊 Quota", "account")],
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

    if data.startswith("approve:"):
        try:
            contribution_id = int(data.split(":", 1)[1])
            await process_approval(query, contribution_id, True)
        except Exception:
            pass
        return

    if data.startswith("reject:"):
        try:
            contribution_id = int(data.split(":", 1)[1])
            await request_review_note(query, context, contribution_id, "reject")
        except Exception:
            pass
        return

    if data.startswith("revise:"):
        try:
            contribution_id = int(data.split(":", 1)[1])
            await request_review_note(query, context, contribution_id, "revise")
        except Exception:
            pass
        return

    if data == "admin_ai":
        await show_ai_registry(query)
        return

    if data == "admin_runtime":
        await show_runtime(query)
        return


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

    await send_safe_message(
        update,
        f"📊 *رصيدك المتبقي لليوم:* {remaining} من {DAILY_LIMIT} طلباً.",
        home_keyboard(),
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
        home_keyboard(),
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

    _clear_admin_state(context)
    _clear_review_state(context)
    _clear_contribution_state(context)
    messaging._clear_contact_state(context)

    if active or review_active or contact_active:
        await update.message.reply_text(
            "❌ تم إلغاء العملية الجارية.",
            reply_markup=home_keyboard(),
        )
        return

    await update.message.reply_text(
        "ℹ️ لا توجد عملية قيد التنفيذ.",
        reply_markup=home_keyboard(),
    )


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

    # MEDBOT resource-search mode has priority over general AI.
    if context.user_data.get("search_mode"):
        context.user_data["search_mode"] = False
        context.user_data["assistant_mode"] = None
        await run_search(update, query)
        return

    # Explicit /ask always means Medical AI.
    if query.startswith("/ask"):
        query = query.replace("/ask", "", 1).strip()
        context.user_data["assistant_mode"] = "medical"

    # If the user is inside the Assistant gateway, route according
    # to the selected assistant mode instead of guessing the intent.
    assistant_mode = context.user_data.get("assistant_mode")

    # MEDBOT resource assistant: deterministic search first, then grounded AI.
    if assistant_mode == "resource":
        context.user_data["assistant_mode"] = None

        try:
            answer = await generate_medbot_assistant_response(
                query,
                user_id=user.id,
            )
        except Exception:
            logger.exception("MEDBOT grounded assistant failed")
            answer = NOT_REGISTERED_MESSAGE

        await send_safe_message(
            update,
            answer,
            home_keyboard(),
        )
        return

    if assistant_mode is None:
        await update.message.reply_text(
            "🤖 *MEDBOT Assistant*\n\n"
            "اختر أولاً نوع المساعدة التي تريد استخدامها:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [
                    [btn("📚 البحث داخل MEDBOT", "assistant_search")],
                    [btn("🩺 المساعد الطبي العام", "assistant_medical")],
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

    # Only the General Medical AI mode consumes the daily AI quota.
    if context.user_data.get("assistant_mode") != "medical":
        await update.message.reply_text(
            "⚠️ لم يتم تحديد وضع المساعد بشكل صحيح.\n\n"
            "يرجى اختيار أحد المسارين من 🤖 MEDBOT Assistant.",
            reply_markup=home_keyboard(),
        )
        return

    allowed, remaining = await database.check_and_increment_quota(
        user.id,
        max_limit=DAILY_LIMIT,
    )

    if not allowed:
        await update.message.reply_text(
            "⚠️ استنفدت رصيدك اليومي المتاح (20 طلباً). "
            "يتجدد الرصيد تلقائياً كل 24 ساعة.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=home_keyboard(),
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
        ai_answer = await generate_medical_ai_response(query, user_id=user.id)
    except Exception as exc:
        logger.exception("AI request failed")
        ai_answer = (
            "⚠️ تعذر الوصول إلى خدمة الذكاء الاصطناعي حالياً.\n\n"
            "لم يتم إنشاء إجابة غير مؤكدة."
        )

    final_text = (
        f"{ai_answer}\n\n"
        "—\n"
        f"💡 *الرصيد المتبقي اليوم: {remaining} من {DAILY_LIMIT} طلب*"
    )

    await send_safe_message(
        update,
        final_text,
        home_keyboard(),
    )


# ============================================================
# MEDIA / CONTRIBUTION ROUTING
# ============================================================


async def media_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin resource upload has priority over student contributions.
    handled = await admin_upload_media_handler(update, context)

    if handled:
        return

    handled = await contribution_media_handler(update, context)

    if handled:
        return

    # Unrecognized media with no active state: never crash, never corrupt.
    await update.message.reply_text(
        "📚 إذا كنت تريد إرسال مساهمة، افتح:\n"
        "📤 Student Contributions\n\n"
        "أو استخدم /start للعودة إلى الرئيسية.",
        reply_markup=home_keyboard(),
    )


# ============================================================
# ERRORS / STARTUP
# ============================================================


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.warning("MEDBOT network/runtime warning: %s", context.error)


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

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
