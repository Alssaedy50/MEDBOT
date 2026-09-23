"""Platform Settings admin surface for MEDBOT (إعدادات المنصة).

Isolated like `audit.py` / `admin_management.py`: this module owns the
`settings`-backed platform identity/interface content and its own handler set.
Every callback re-checks `can_settings` (owner always passes), and every
mutation writes an audit entry via `audit.log_action`.

Integration contract:
    platform_settings.register_platform_settings_handlers(app)

State keys used (namespaced):
    settings_edit_key -> the setting key awaiting new text from the admin
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
)

import audit
import database

logger = logging.getLogger(__name__)


def esc(value) -> str:
    import html

    return html.escape(str(value if value is not None else ""))


def btn(text, callback_data):
    return InlineKeyboardButton(text, callback_data=callback_data)


def _home_keyboard():
    return InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]])


async def _is_authorized(user_id) -> bool:
    try:
        return await database.user_has_permission(user_id, "can_settings")
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
            logger.warning("platform_settings: edit_message_text failed")


def _settings_menu(settings) -> InlineKeyboardMarkup:
    rows = []
    for key in database.PLATFORM_SETTING_KEYS:
        label = database.PLATFORM_SETTING_LABELS.get(key, key)
        value = str(settings.get(key) or "")
        preview = value[:24] + ("…" if len(value) > 24 else "")
        rows.append(
            [btn(f"{label}: {preview}", f"set_edit:{key}")]
        )
    rows.append([btn("⬅️ إدارة المنصة", "admin")])
    rows.append([btn("🏠 الرئيسية", "home")])
    return InlineKeyboardMarkup(rows)


async def show_platform_settings(query, context=None):
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    if context is not None:
        context.user_data.pop("settings_edit_key", None)

    try:
        settings = await database.get_platform_settings()
    except Exception:
        logger.exception("platform_settings: load failed")
        settings = {}

    lines = [
        "⚙️ <b>إعدادات المنصة</b>\n",
        "اضغط على أي إعداد لتعديل نصه. تُحفظ القيم في قاعدة البيانات "
        "وتظهر مباشرة في واجهة المستخدم.",
    ]

    await _edit(query, "\n".join(lines), _settings_menu(settings))


async def start_edit(query, context, key):
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    if key not in database.PLATFORM_SETTING_KEYS:
        await _edit(
            query,
            "⚠️ إعداد غير معروف.",
            InlineKeyboardMarkup([[btn("⬅️ الإعدادات", "admin_settings")]]),
        )
        return

    try:
        current = await database.get_platform_setting(key)
    except Exception:
        current = ""

    context.user_data["settings_edit_key"] = key
    label = database.PLATFORM_SETTING_LABELS.get(key, key)

    await _edit(
        query,
        f"⚙️ <b>{esc(label)}</b>\n\n"
        f"القيمة الحالية:\n<code>{esc(current)}</code>\n\n"
        "أرسل النص الجديد في رسالة، أو أرسل /cancel للإلغاء.",
        InlineKeyboardMarkup(
            [
                [btn("❌ إلغاء", "admin_settings")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


async def handle_settings_text(update, context) -> bool:
    """Consume the admin's typed setting value. Returns True when handled."""
    key = context.user_data.get("settings_edit_key")

    if not key:
        return False

    if not update.message or not update.message.text:
        return False

    context.user_data.pop("settings_edit_key", None)

    if not await _is_authorized(update.effective_user.id):
        await update.message.reply_text("🔒 غير مصرح.", reply_markup=_home_keyboard())
        return True

    text = update.message.text.strip()

    if text == "/cancel":
        await update.message.reply_text(
            "❌ تم إلغاء التعديل.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [btn("⚙️ الإعدادات", "admin_settings")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return True

    ok = await database.set_platform_setting(key, text)

    if not ok:
        await update.message.reply_text(
            "⚠️ نص غير صالح (فارغ أو طويل جداً).",
            reply_markup=InlineKeyboardMarkup(
                [
                    [btn("⚙️ الإعدادات", "admin_settings")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return True

    await audit.log_action(
        update.effective_user.id,
        "platform_setting",
        target_type="setting",
        target_id=key,
    )

    await update.message.reply_text(
        "✅ تم حفظ الإعداد بنجاح.",
        reply_markup=InlineKeyboardMarkup(
            [
                [btn("⚙️ الإعدادات", "admin_settings")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )
    return True


async def platform_settings_callback_handler(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    try:
        await query.answer()
    except Exception:
        pass

    data = query.data or ""

    if data == "admin_settings":
        await show_platform_settings(query, context)
        return

    if data.startswith("set_edit:"):
        await start_edit(query, context, data.split(":", 1)[1])
        return

    await _edit(query, "⚠️ إجراء غير معروف.", _home_keyboard())


def register_platform_settings_handlers(app):
    """Register the Platform Settings admin surface."""
    app.add_handler(
        CallbackQueryHandler(
            platform_settings_callback_handler,
            pattern=r"^(admin_settings|set_edit:)",
        )
    )
