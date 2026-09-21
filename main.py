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
from ai import generate_medical_ai_response

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DAILY_LIMIT = 20

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


async def edit_safe(query, text, reply_markup=None):
    """Safely edit an inline message."""
    try:
        await query.edit_message_text(
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup,
        )
    except Exception:
        try:
            await query.edit_message_text(
                text=text,
                parse_mode=None,
                reply_markup=reply_markup,
            )
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
                btn("🔎 Search", "search"),
            ],
            [
                btn("🤖 MEDBOT Assistant", "assistant"),
                btn("📤 Student Contributions", "contribute"),
            ],
            [
                btn("📊 My Account", "account"),
                btn("ℹ️ About MEDBOT", "about"),
            ],
            [
                btn("🛠 Admin Panel", "admin"),
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
            home_keyboard(),
        )
    else:
        await send_safe_message(
            update,
            text,
            home_keyboard(),
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


async def open_file(query, context, content_id):
    try:
        record = await database.get_file_record(content_id)
    except Exception:
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
        title, file_id, file_type, source_type, source_contribution_id = record
    except Exception:
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

    if not file_id:
        await edit_safe(
            query,
            f"📄 *{title}*\n\n"
            "المورد مسجل في قاعدة بيانات MEDBOT، "
            "لكن لا يوجد ملف قابل للإرسال حالياً.",
            InlineKeyboardMarkup(
                [
                    [btn("📚 MEDBOT Resources", "library:0")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    chat_id = query.message.chat_id

    try:
        ft = str(file_type or "").lower()

        if ft in ("photo", "image", "jpg", "jpeg", "png", "webp"):
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=file_id,
                caption=f"🖼 {title}",
            )
        elif ft in ("audio", "mp3", "m4a", "wav"):
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=file_id,
                caption=f"🎧 {title}",
            )
        elif ft in ("video", "mp4", "mkv", "mov"):
            await context.bot.send_video(
                chat_id=chat_id,
                video=file_id,
                caption=f"🎥 {title}",
            )
        else:
            await context.bot.send_document(
                chat_id=chat_id,
                document=file_id,
                caption=f"📄 {title}",
            )

        await context.bot.send_message(
            chat_id=chat_id,
            text="📚 يمكنك العودة إلى المكتبة من هنا:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [btn("📚 MEDBOT Resources", "library:0")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )

    except Exception as exc:
        logger.exception("File send failed")
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ تعذر إرسال هذا المورد من Telegram حالياً.\n\n"
                "المورد نفسه ما زال مسجلاً في MEDBOT."
            ),
            reply_markup=InlineKeyboardMarkup(
                [
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
    Unified search entry point.

    The actual search logic lives in database.search_content().
    This wrapper keeps main.py backward-compatible while avoiding
    a second, inconsistent search implementation.
    """
    return await database.search_content(query_text)


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
    except Exception as exc:
        logger.exception("Search failed")
        await update.message.reply_text(
            "⚠️ حدث خطأ أثناء البحث.",
            reply_markup=home_keyboard(),
        )
        return

    context_text = []

    if not results:
        await update.message.reply_text(
            "🔎 *نتيجة البحث*\n\n" "المورد المطلوب غير مسجل حالياً في MEDBOT.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=home_keyboard(),
        )
        return

    buttons = []

    for row in results:
        try:
            (
                content_id,
                title,
                file_type,
                folder_id,
                folder_name,
                full_path,
            ) = row
        except ValueError:
            # Backward compatibility with the old 5-column search result.
            (
                content_id,
                title,
                file_type,
                folder_id,
                folder_name,
            ) = row
            full_path = folder_name or ""

        context_text.append(f"• {title} — {folder_name or 'بدون قسم'}")

        buttons.append(
            [
                btn(
                    f"📄 {str(title)[:35]}",
                    f"file:{content_id}",
                )
            ]
        )

    buttons.append([btn("🏠 الرئيسية", "home")])

    await update.message.reply_text(
        "🔎 *نتائج البحث*\n\n" + "\n".join(context_text),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ============================================================
# ACCOUNT / ABOUT
# ============================================================


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


async def contribution_media_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    folder_id = context.user_data.get("contribution_folder")

    if not folder_id:
        return False

    user = update.effective_user
    file_id = None
    file_type = None

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

    try:
        contribution_id = await database.add_contribution(
            user.id,
            user.full_name,
            folder_id,
            title,
            file_id,
            file_type,
        )

        context.user_data.pop("contribution_folder", None)

        await update.message.reply_text(
            "✅ *تم استلام مساهمتك بنجاح.*\n\n"
            f"رقم المساهمة: `{contribution_id}`\n"
            "الحالة الحالية: `pending`\n\n"
            "سيتمكن المشرفون من مراجعتها قبل نشرها في المكتبة.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=home_keyboard(),
        )

    except Exception as exc:
        logger.exception("Contribution failed")
        await update.message.reply_text(
            "⚠️ تعذر تسجيل المساهمة حالياً. لم يتم تأكيد نجاح الإضافة.",
            reply_markup=home_keyboard(),
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


async def show_admin_folders(query):
    """Admin-only folder management menu."""
    if not await _admin_check(query):
        await edit_safe(
            query,
            "🔒 غير مصرح.",
            InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]]),
        )
        return

    folders = await database.get_folders(0)

    rows = [[btn("➕ إنشاء قسم جديد", "admin_folder_create")]]

    if folders:
        rows.append([btn("📂 اختيار قسم لإضافة قسم داخله", "admin_folder_parent:0")])

    rows.extend(
        [
            [btn("⬅️ Admin", "admin")],
            [btn("🏠 الرئيسية", "home")],
        ]
    )

    await edit_safe(
        query,
        "🗂 *إدارة الأقسام*\n\n" "يمكنك إنشاء أقسام رئيسية أو أقسام فرعية متداخلة.",
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

    await edit_safe(
        query,
        "🛠 *MEDBOT Admin Panel*\n\n"
        f"📤 المساهمات المعلقة: {pending_count}\n\n"
        "اختر الإجراء:",
        InlineKeyboardMarkup(
            [
                [btn("📥 مراجعة المساهمات", "admin_pending")],
                [btn("🤖 AI Registry", "admin_ai")],
                [btn("📊 Runtime", "admin_runtime")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
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
                [btn("⬅️ Pending", "admin_pending")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


async def process_approval(query, contribution_id, approve):
    try:
        is_admin = await database.is_user_admin(query.from_user.id)
    except Exception:
        is_admin = False

    if not is_admin:
        return

    try:
        if approve:
            result = await database.approve_contribution(contribution_id)
            action = "اعتماد"
        else:
            result = await database.reject_contribution(contribution_id)
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

        # Notify contributor when possible.
        try:
            if result and len(result) >= 5:
                contributor_id = result[4]
                await query.get_bot().send_message(
                    chat_id=contributor_id,
                    text=(
                        "📢 تحديث مساهمتك في MEDBOT\n\n"
                        f"تم {action} المساهمة رقم `{contribution_id}`."
                    ),
                    parse_mode=ParseMode.MARKDOWN,
                )
        except Exception:
            pass

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
        await edit_safe(
            query,
            "🤖 *MEDBOT Assistant*\n\n"
            "اكتب سؤالك الطبي الآن.\n\n"
            "سيتم استخدام نظام MEDBOT AI ضمن القواعد والمصادر المسجلة في النظام.",
            InlineKeyboardMarkup(
                [
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

    if data == "contribute":
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
            await process_approval(query, contribution_id, False)
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


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel any active admin folder creation flow."""
    if not update.message:
        return

    if context.user_data.get("admin_folder_create"):
        context.user_data.pop("admin_folder_create", None)
        context.user_data.pop("admin_folder_parent", None)
        context.user_data.pop("admin_folder_name", None)
        context.user_data.pop("admin_folder_type", None)

        await update.message.reply_text(
            "❌ تم إلغاء إنشاء القسم.",
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

    # Admin folder creation state has priority over search/AI.
    if context.user_data.get("admin_folder_create"):
        handled = await request_admin_folder_name(update, context)
        if handled:
            return

    # Search mode has priority over AI.
    if context.user_data.get("search_mode"):
        context.user_data["search_mode"] = False
        await run_search(update, query)
        return

    if query.startswith("/ask"):
        query = query.replace("/ask", "", 1).strip()

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
    handled = await contribution_media_handler(update, context)

    if handled:
        return

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
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("ask", ai_handler))

    # Inline UI
    app.add_handler(CallbackQueryHandler(callback_router))

    # Uploaded media / student contributions
    app.add_handler(
        MessageHandler(
            filters.Document.ALL | filters.PHOTO | filters.AUDIO | filters.VIDEO,
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
