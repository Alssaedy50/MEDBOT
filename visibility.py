"""Navigation visibility control (إظهار/إخفاء الأقسام) for MEDBOT.

Isolated like `audit.py` / `admin_management.py`: this module owns the admin
surface that hides or shows any student-facing feature, and its own handler set.

Hiding a feature removes its button from the home page for regular users AND
blocks its callbacks, so a feature can be taken offline during a fault or an
update without a redeploy. Admins always keep their own entry point so they can
restore a hidden feature.

The persisted state lives in the `settings` table (`database.get_hidden_features`
/ `set_hidden_features`), so no schema change is involved.

Integration contract:
    visibility.register_visibility_handlers(app)
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
        return await database.user_has_permission(user_id, "can_visibility")
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
            logger.warning("visibility: edit_message_text failed")


def _visibility_menu(hidden) -> InlineKeyboardMarkup:
    rows = []
    for key in database.FEATURES:
        label = database.FEATURE_LABELS.get(key, key)
        mark = "🙈" if key in hidden else "👁"
        rows.append([btn(f"{mark} {label}", f"vis_toggle:{key}")])
    rows.append([btn("✅ إظهار الكل", "vis_showall")])
    rows.append([btn("⬅️ إدارة المنصة", "admin")])
    rows.append([btn("🏠 الرئيسية", "home")])
    return InlineKeyboardMarkup(rows)


async def show_visibility(query):
    """Admin screen: toggle each student-facing feature on or off."""
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    hidden = await database.get_hidden_features()

    lines = [
        "🙈 <b>إظهار وإخفاء الأقسام</b>",
        "",
        "اختر أي قسم لإخفائه عن المستخدمين أو إظهاره.",
        "إخفاء القسم يزيل زرّه من الصفحة الرئيسية ويمنع الوصول إليه "
        "فوراً — دون الحاجة إلى إعادة تشغيل البوت.",
        "",
        f"👁 الظاهرة: {len(database.FEATURES) - len(hidden)} | "
        f"🙈 المخفية: {len(hidden)}",
    ]

    await _edit(query, "\n".join(lines), _visibility_menu(hidden))


async def toggle_feature(query, feature):
    """Flip one feature's visibility and re-render the menu."""
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    if feature not in database.FEATURES:
        await _edit(
            query,
            "⚠️ قسم غير معروف.",
            InlineKeyboardMarkup([[btn("⬅️ إظهار وإخفاء الأقسام", "vis_list")]]),
        )
        return

    hidden = await database.get_hidden_features()
    if feature in hidden:
        hidden.discard(feature)
        action = "show"
    else:
        hidden.add(feature)
        action = "hide"

    ok = await database.set_hidden_features(hidden)

    if ok:
        await audit.log_action(
            query.from_user.id,
            "feature_visibility",
            target_type="feature",
            target_id=feature,
            details=action,
        )

    await show_visibility(query)


async def show_all_features(query):
    """Restore every feature at once (the recovery path after an update)."""
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    ok = await database.set_hidden_features(set())

    if ok:
        await audit.log_action(
            query.from_user.id,
            "feature_visibility",
            target_type="feature",
            target_id="all",
            details="show",
        )

    await show_visibility(query)


async def visibility_callback_handler(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    try:
        await query.answer()
    except Exception:
        pass

    data = query.data or ""

    if data == "vis_list":
        await show_visibility(query)
        return

    if data == "vis_showall":
        await show_all_features(query)
        return

    if data.startswith("vis_toggle:"):
        await toggle_feature(query, data.split(":", 1)[1])
        return

    await _edit(query, "⚠️ إجراء غير معروف.", _home_keyboard())


def register_visibility_handlers(app):
    """Register the feature-visibility surface."""
    app.add_handler(
        CallbackQueryHandler(
            visibility_callback_handler,
            pattern=r"^(vis_list|vis_showall|vis_toggle:)",
        )
    )
