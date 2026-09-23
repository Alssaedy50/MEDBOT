"""Notifications (الإشعارات) admin surface for MEDBOT.

The `notify_admins_*` helpers used elsewhere ping admins about new content.
This module owns the *admin → users* broadcast direction, plus a durable
history in the `notifications` table so a send is auditable and reviewable.

Isolated like the other admin modules: own handler set, `can_notifications`
gate (owner always passes), audit entries on every send.

State keys used (namespaced):
    notifications_body -> admin is typing the broadcast body
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

# Telegram is rate-limited; cap a single broadcast so one admin action cannot
# stall the polling loop for minutes.
MAX_BROADCAST_RECIPIENTS = 500


def esc(value) -> str:
    import html

    return html.escape(str(value if value is not None else ""))


def btn(text, callback_data):
    return InlineKeyboardButton(text, callback_data=callback_data)


def _home_keyboard():
    return InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]])


async def _is_authorized(user_id) -> bool:
    try:
        return await database.user_has_permission(user_id, "can_notifications")
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
            logger.warning("notifications: edit_message_text failed")


def _menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn("✍️ إرسال إشعار", "notif_new")],
            [btn("📜 سجل الإشعارات", "notif_history")],
            [btn("⬅️ إدارة المنصة", "admin")],
            [btn("🏠 الرئيسية", "home")],
        ]
    )


async def show_notifications(query, context=None):
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    if context is not None:
        context.user_data.pop("notifications_body", None)

    try:
        total = await database.get_notifications_count()
    except Exception:
        total = "?"

    await _edit(
        query,
        "🔔 <b>الإشعارات</b>\n\n"
        "يمكنك إرسال إشعار إلى جميع مستخدمي المنصة، أو مراجعة سجل "
        "الإشعارات السابقة.\n\n"
        f"📦 إجمالي الإشعارات المُرسلة: {total}",
        _menu(),
    )


async def start_notification(query, context):
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    context.user_data["notifications_body"] = True

    await _edit(
        query,
        "✍️ <b>إرسال إشعار</b>\n\n"
        "أرسل الآن نص الإشعار. سيتلقاه جميع المستخدمين المسجلين.\n\n"
        "لإلغاء العملية أرسل /cancel.",
        InlineKeyboardMarkup(
            [[btn("❌ إلغاء", "admin_notifications")], [btn("🏠 الرئيسية", "home")]]
        ),
    )


async def broadcast_notification(bot, body: str, sender_id=None, title: str = None) -> tuple:
    """Deliver a notification to every registered user.

    Per-recipient failures are non-fatal (a blocked bot must not abort the
    broadcast). Returns (recipients, delivered) and records the send in the
    `notifications` table. Best-effort: never raises.
    """
    body = (body or "").strip()
    if not body:
        return 0, 0

    try:
        recipients = await database.get_all_user_ids()
    except Exception:
        logger.exception("notifications: could not load recipients")
        return 0, 0

    recipients = list(recipients)[:MAX_BROADCAST_RECIPIENTS]

    text = f"🔔 <b>إشعار</b>\n\n{esc(body)}"

    delivered = 0
    for user_id in recipients:
        try:
            await bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
            delivered += 1
        except Exception:
            logger.warning("notifications: delivery failed for %s", user_id)

    try:
        await database.record_notification(
            sender_id=sender_id,
            title=title,
            body=body,
            audience="all",
            recipients=len(recipients),
            delivered=delivered,
        )
    except Exception:
        logger.exception("notifications: could not persist notification record")

    return len(recipients), delivered


async def handle_notification_text(update, context) -> bool:
    """Consume the admin's typed broadcast body. Returns True when handled."""
    if not context.user_data.get("notifications_body"):
        return False

    if not update.message or not update.message.text:
        return False

    context.user_data.pop("notifications_body", None)

    if not await _is_authorized(update.effective_user.id):
        await update.message.reply_text("🔒 غير مصرح.", reply_markup=_home_keyboard())
        return True

    text = update.message.text.strip()

    if text == "/cancel":
        await update.message.reply_text(
            "❌ تم إلغاء الإشعار.",
            reply_markup=InlineKeyboardMarkup(
                [[btn("🔔 الإشعارات", "admin_notifications")], [btn("🏠 الرئيسية", "home")]]
            ),
        )
        return True

    if not text:
        await update.message.reply_text("⚠️ النص فارغ.")
        return True

    recipients, delivered = await broadcast_notification(
        context.bot, text, sender_id=update.effective_user.id
    )

    await audit.log_action(
        update.effective_user.id,
        "notification_send",
        target_type="notification",
        details=f"delivered={delivered}/{recipients}",
    )

    await update.message.reply_text(
        f"✅ تم إرسال الإشعار إلى {delivered} من {recipients} مستخدم.",
        reply_markup=InlineKeyboardMarkup(
            [[btn("🔔 الإشعارات", "admin_notifications")], [btn("🏠 الرئيسية", "home")]]
        ),
    )
    return True


async def show_history(query):
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    try:
        rows = await database.get_notifications(limit=15)
    except Exception:
        rows = []

    lines = ["📜 <b>سجل الإشعارات</b>\n"]

    if not rows:
        lines.append("لا توجد إشعارات سابقة.")
    else:
        for entry in rows:
            try:
                (_id, sender, _title, body, _aud, recipients, delivered, created) = entry[:8]
            except Exception:
                continue
            lines.append(
                f"• {esc(str(body)[:120])}\n"
                f"  📤 {esc(delivered)}/{esc(recipients)} — <i>{esc(created)}</i>"
            )

    await _edit(query, "\n".join(lines), _menu())


async def notifications_callback_handler(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    try:
        await query.answer()
    except Exception:
        pass

    data = query.data or ""

    if data == "admin_notifications":
        await show_notifications(query, context)
        return

    if data == "notif_new":
        await start_notification(query, context)
        return

    if data == "notif_history":
        await show_history(query)
        return

    await _edit(query, "⚠️ إجراء غير معروف.", _home_keyboard())


def register_notifications_handlers(app):
    """Register the Notifications admin surface."""
    app.add_handler(
        CallbackQueryHandler(
            notifications_callback_handler,
            pattern=r"^(admin_notifications|notif_new|notif_history)",
        )
    )
