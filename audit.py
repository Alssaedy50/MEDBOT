"""Audit log subsystem for MEDBOT.

Isolated like `messaging.py`: this module owns the `audit_log` table access and
its own handler set. It must not read or write any other subsystem's tables.

Integration contract (see `register_audit_handlers`):
    audit.register_audit_handlers(app)

Design: `log_action()` is best-effort by contract. It never raises into the
caller and never blocks the audited operation, so a failing audit write can
never break an admin action. Every entry records actor + action + target + time.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
)

import database

logger = logging.getLogger(__name__)

# Important admin operations worth recording. Extend by adding entries here.
AUDIT_ACTIONS = (
    "folder_create",
    "folder_rename",
    "folder_move",
    "folder_retype",
    "folder_toggle",
    "folder_delete",
    "content_upload",
    "content_rename",
    "content_move",
    "content_retype",
    "content_delete",
    "contribution_approve",
    "contribution_reject",
    "contribution_revise",
    "message_reply",
    "message_status",
    "admin_add",
    "admin_remove",
    "admin_role",
    "admin_permissions",
    "owner_bootstrap",
    "ownership_transfer",
    "platform_setting",
    "topic_create",
    "topic_link",
    "topic_unlink",
    "topic_toggle",
    "topic_delete",
    "notification_send",
    "language_set",
    "feature_visibility",
)

ACTION_LABELS = {
    "folder_create": "🗂 إنشاء مجلد",
    "folder_rename": "✏️ إعادة تسمية مجلد",
    "folder_move": "📦 نقل مجلد",
    "folder_retype": "🏷 تغيير نوع مجلد",
    "folder_toggle": "🔀 تبديل استقبال المساهمات",
    "folder_delete": "🗑 حذف مجلد",
    "content_upload": "📤 رفع محتوى",
    "content_rename": "✏️ إعادة تسمية محتوى",
    "content_move": "📦 نقل محتوى",
    "content_retype": "🏷 تغيير نوع محتوى",
    "content_delete": "🗑 حذف محتوى",
    "contribution_approve": "✅ قبول مساهمة",
    "contribution_reject": "❌ رفض مساهمة",
    "contribution_revise": "🔁 طلب تعديل مساهمة",
    "message_reply": "💬 الرد على رسالة",
    "message_status": "🔄 تغيير حالة رسالة",
    "admin_add": "➕ إضافة مشرف",
    "admin_remove": "➖ إزالة مشرف",
    "admin_role": "👑 تغيير دور",
    "admin_permissions": "🔐 تغيير صلاحيات",
    "owner_bootstrap": "🔑 تهيئة المالك",
    "ownership_transfer": "👑 نقل الملكية",
    "platform_setting": "⚙️ تعديل إعداد المنصة",
    "topic_create": "🧭 إنشاء موضوع",
    "topic_link": "🔗 ربط قسم بموضوع",
    "topic_unlink": "✂️ إزالة رابط موضوع",
    "topic_toggle": "🔀 تفعيل/تعطيل موضوع",
    "topic_delete": "🗑 حذف موضوع",
    "notification_send": "🔔 إرسال إشعار",
    "language_set": "🌐 تغيير اللغة",
    "feature_visibility": "🙈 إظهار/إخفاء قسم",
}


def esc(value) -> str:
    import html

    return html.escape(str(value if value is not None else ""))


def btn(text, callback_data):
    return InlineKeyboardButton(text, callback_data=callback_data)


def _home_keyboard():
    return InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]])


async def log_action(actor_id, action, target_type=None, target_id=None,
                     details=None) -> bool:
    """Record one admin operation. Never raises; returns True on success.

    `actor_role` is resolved from the existing admin identity. Malformed input
    (unknown action, non-numeric actor, None details) is tolerated so auditing
    can never break the operation being audited.
    """
    try:
        if not action:
            return False

        actor_role = None
        try:
            record = await database.get_admin_record(actor_id)
            if record:
                actor_role = record.get("role")
        except Exception:
            actor_role = None

        return await database.add_audit_entry(
            actor_id=None if actor_id is None else int(actor_id),
            actor_role=actor_role,
            action=str(action),
            target_type=None if target_type is None else str(target_type),
            target_id=target_id,
            details=None if details is None else str(details),
        )
    except Exception:
        # Auditing is best-effort: swallow everything, including a bad actor_id.
        logger.warning("audit: log_action failed for %s", action)
        return False


# ------------------------------------------------------------
# Read-only viewer (owner + can_admins only)
# ------------------------------------------------------------
async def _can_view(user_id) -> bool:
    try:
        if await database.is_owner(user_id):
            return True
        return await database.user_has_permission(user_id, "can_admins")
    except Exception:
        return False


def _viewer_keyboard():
    rows = [[btn(ACTION_LABELS.get(key, key), f"audit_act:{key}")]
            for key in AUDIT_ACTIONS]
    rows.append([btn("📜 كل السجل", "audit_log")])
    rows.append([btn("⬅️ Admin", "admin")])
    rows.append([btn("🏠 الرئيسية", "home")])
    return InlineKeyboardMarkup(rows)


async def _edit(query, text, markup=None):
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup
        )
    except Exception:
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            logger.warning("audit: edit_message_text failed")


async def show_audit_log(query, action=None):
    """Render recent audit entries, optionally filtered by action."""
    if not await _can_view(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    try:
        entries = await database.get_audit_entries(limit=20, action=action)
        total = await database.get_audit_count()
    except Exception:
        logger.exception("audit: viewer failed")
        entries, total = [], "?"

    lines = ["📜 <b>سجل التدقيق</b>"]
    if action:
        lines.append(f"🔎 فلتر: {esc(ACTION_LABELS.get(action, action))}")
    lines.append(f"📦 إجمالي السجلات: {total}\n")

    if not entries:
        lines.append("لا توجد سجلات مطابقة.")
    else:
        for entry in entries:
            try:
                (_id, actor_id, actor_role, act, target_type,
                 target_id, _details, created) = entry[:8]
            except Exception:
                continue
            label = ACTION_LABELS.get(act, act)
            who = esc(actor_id)
            if actor_role:
                who += f" ({esc(actor_role)})"
            target = f" ← {esc(target_type)}#{esc(target_id)}" if target_type else ""
            lines.append(f"• {esc(label)} — {who}{target}\n  <i>{esc(created)}</i>")

    await _edit(query, "\n".join(lines), _viewer_keyboard())


async def audit_callback_handler(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    try:
        await query.answer()
    except Exception:
        pass

    data = query.data or ""

    if data == "audit_log":
        await show_audit_log(query)
        return

    if data.startswith("audit_act:"):
        await show_audit_log(query, action=data.split(":", 1)[1])
        return

    # Unknown audit callback: fail safely rather than falling through.
    await _edit(query, "⚠️ إجراء غير معروف.", _home_keyboard())


def register_audit_handlers(app):
    """Register the audit viewer. Mirrors messaging registration."""
    app.add_handler(
        CallbackQueryHandler(
            audit_callback_handler,
            pattern=r"^(audit_log|audit_act:)",
        )
    )
