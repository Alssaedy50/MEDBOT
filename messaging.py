"""Contact Admin messaging system for MEDBOT.

Isolated from library, contributions and exams: this module owns the
`messages` table access and its own handler set. It must not import from
`database` functions belonging to other systems.

Integration contract (see `register_messaging_handlers`):
    messaging.register_messaging_handlers(app)

State keys used (namespaced to avoid collisions):
    contact_category  -> selected category awaiting body
    contact_reply_id  -> admin message id awaiting reply text
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database
import audit

logger = logging.getLogger(__name__)


def esc(value) -> str:
    """Escape user text for Telegram HTML parse mode."""
    import html

    return html.escape(str(value if value is not None else ""))


def btn(text, callback_data):
    return InlineKeyboardButton(text, callback_data=callback_data)


HOME_KEYBOARD = InlineKeyboardMarkup(
    [[btn("🏠 الرئيسية", "home")]]
)


async def _contact_label() -> str:
    """Platform-configurable contact label (defaults to 'تواصل مع المنصة')."""
    try:
        return await database.get_platform_setting("contact_text")
    except Exception:
        return "تواصل مع المنصة"


def _contact_keyboard():
    rows = [
        [btn("💬 رسالة", "msg_cat:message")],
        [btn("📑 طلب ملخص", "msg_cat:summary")],
        [btn("💡 اقتراح", "msg_cat:suggestion")],
        [btn("🚩 بلاغ", "msg_cat:report")],
        [btn("📥 رسائلي", "msg_mine")],
        [btn("🏠 الرئيسية", "home")],
    ]
    return InlineKeyboardMarkup(rows)


def _clear_contact_state(context):
    context.user_data.pop("contact_category", None)
    context.user_data.pop("contact_reply_id", None)


async def _edit(query, text, markup=None):
    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    except Exception:
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            logger.warning("messaging: edit_message_text failed")


async def _reply(update, text, markup=None):
    try:
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    except Exception:
        try:
            await update.message.reply_text(text, reply_markup=markup)
        except Exception:
            logger.warning("messaging: reply_text failed")


# ---------------------------------------------------------------
# Student side
# ---------------------------------------------------------------


async def contact_admin_screen(query, context):
    _clear_contact_state(context)
    label = await _contact_label()
    await _edit(
        query,
        f"📬 <b>{esc(label)}</b>\n\n"
        "اختر نوع الرسالة التي تريد إرسالها.\n"
        "يمكنك إرسال رسالة، طلب ملخص، اقتراح، أو بلاغ.",
        _contact_keyboard(),
    )


async def select_category(query, context, category):
    if category not in database.MESSAGE_CATEGORIES:
        await _edit(
            query,
            "⚠️ نوع الرسالة غير مدعوم.",
            _contact_keyboard(),
        )
        return

    context.user_data["contact_category"] = category
    label = database.MESSAGE_CATEGORY_LABELS.get(category, category)

    await _edit(
        query,
        f"📝 <b>{esc(label)}</b>\n\n"
        "اكتب الآن نص الرسالة وأرسله.\n\n"
        "لإلغاء العملية اضغط ❌ إلغاء أو أرسل /cancel.",
        InlineKeyboardMarkup(
            [
                [btn("❌ إلغاء", "msg_cancel")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


async def notify_admins_new_message(bot, message_id, category, body, sender):
    """Notify every admin. Per-recipient failures are non-fatal."""
    try:
        admins = await database.get_all_admins()
    except Exception:
        logger.exception("messaging: could not load admins")
        return 0

    label = database.MESSAGE_CATEGORY_LABELS.get(category, category)

    text = (
        "📬 <b>رسالة جديدة من طالب</b>\n\n"
        f"🆔 `{message_id}`\n"
        f"🏷 النوع: {esc(label)}\n"
        f"👤 من: {esc(sender or 'طالب')}\n\n"
        f"📝 {esc(str(body)[:400])}\n\n"
        "افتح لوحة الإدارة للرد."
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
            logger.warning("messaging: notify failed for admin %s", admin_id)

    return delivered


async def handle_contact_text(update, context):
    """Capture a typed message body. Returns True when handled here."""
    category = context.user_data.get("contact_category")

    if not category:
        return False

    if not update.message or not update.message.text:
        return False

    user = update.effective_user
    text = update.message.text.strip()

    if text == "/cancel":
        _clear_contact_state(context)
        await _reply(update, "❌ تم إلغاء إرسال الرسالة.", HOME_KEYBOARD)
        return True

    try:
        message_id = await database.create_message(
            user.id,
            user.full_name,
            category,
            text,
        )
    except database.MessageValidationError as exc:
        await _reply(update, str(exc))
        return True
    except Exception:
        logger.exception("messaging: create_message failed")
        _clear_contact_state(context)
        await _reply(
            update,
            "⚠️ تعذر إرسال الرسالة حالياً. لم يتم تأكيد الإرسال.",
            HOME_KEYBOARD,
        )
        return True

    _clear_contact_state(context)

    await _reply(
        update,
        "✅ <b>تم استلام رسالتك.</b>\n\n"
        f"🆔 رقم الرسالة: <code>{message_id}</code>\n"
        "🏷 الحالة: 🆕 <b>جديدة</b>\n\n"
        "ستتم مراجعتها من قبل الإدارة، وسيتم إشعارك عند الرد.\n"
        "يمكنك متابعة الحالة من «📥 رسائلي».",
        InlineKeyboardMarkup(
            [
                [btn("📥 رسائلي", "msg_mine")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )

    await notify_admins_new_message(
        context.bot,
        message_id,
        category,
        text,
        user.full_name,
    )
    return True


async def show_my_messages(query):
    try:
        items = await database.get_user_messages(query.from_user.id)
    except Exception:
        logger.exception("messaging: get_user_messages failed")
        items = []

    if not items:
        await _edit(
            query,
            "📥 <b>رسائلي</b>\n\nلم ترسل أي رسالة بعد.",
            InlineKeyboardMarkup(
                [
                    [btn("📬 التواصل مع الإدارة", "contact")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    lines = ["📥 <b>رسائلي</b>\n"]

    for item in items:
        try:
            message_id, category, body, status, reply, created_at = item[:6]
        except Exception:
            continue

        status_label = database.MESSAGE_STATUS_LABELS.get(status, status)
        lines.append(f"🆔 <code>{message_id}</code> — {esc(status_label)}")
        lines.append(f"🏷 {esc(database.MESSAGE_CATEGORY_LABELS.get(category, category))}")
        lines.append(f"📝 {esc(str(body)[:200])}")

        if reply:
            lines.append(f"↩️ <b>الرد:</b> {esc(str(reply)[:300])}")

        lines.append("")

    await _edit(
        query,
        "\n".join(lines),
        InlineKeyboardMarkup(
            [
                [btn("📬 التواصل مع الإدارة", "contact")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


# ---------------------------------------------------------------
# Admin side
# ---------------------------------------------------------------


async def _is_admin(user_id) -> bool:
    try:
        return await database.is_user_admin(user_id)
    except Exception:
        return False


async def _can_messages(user_id) -> bool:
    """Admin gate for the messaging surfaces (RBAC: can_messages)."""
    try:
        return await database.user_has_permission(user_id, "can_messages")
    except Exception:
        return False


async def show_admin_messages(query):
    """List messages awaiting an admin decision."""
    if not await _is_admin(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", HOME_KEYBOARD)
        return

    if not await _can_messages(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", HOME_KEYBOARD)
        return

    try:
        items = await database.get_messages_by_status(None)
        open_count = await database.get_open_messages_count()
    except Exception:
        logger.exception("messaging: admin list failed")
        items, open_count = [], "?"

    buttons = []

    for item in items:
        try:
            message_id, _uid, user_name, category, _body, status, _created = item[:7]
        except Exception:
            continue

        status_label = database.MESSAGE_STATUS_LABELS.get(status, status)
        buttons.append(
            [
                btn(
                    f"{status_label} | {str(user_name or 'طالب')[:18]} | #{message_id}",
                    f"msg_open:{message_id}",
                )
            ]
        )

    buttons.append([btn("⬅️ Admin", "admin")])
    buttons.append([btn("🏠 الرئيسية", "home")])

    text = (
        "📬 <b>رسائل الطلاب</b>\n\n"
        f"🟢 غير مغلقة: {open_count}\n"
        f"📦 الإجمالي المعروض: {len(items)}\n\n"
        "اختر رسالة لعرضها والرد عليها."
    )

    await _edit(query, text, InlineKeyboardMarkup(buttons))


async def open_admin_message(query, context, message_id):
    if not await _is_admin(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", HOME_KEYBOARD)
        return

    if not await _can_messages(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", HOME_KEYBOARD)
        return

    _clear_contact_state(context)

    try:
        record = await database.get_message(message_id)
    except Exception:
        record = None

    if not record:
        await _edit(
            query,
            "⚠️ الرسالة غير موجودة.",
            InlineKeyboardMarkup(
                [
                    [btn("📬 الرسائل", "admin_messages")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    (
        message_id,
        user_id,
        user_name,
        category,
        body,
        status,
        admin_reply,
        reviewed_by,
        created_at,
        updated_at,
    ) = record[:10]

    status_label = database.MESSAGE_STATUS_LABELS.get(status, status)
    category_label = database.MESSAGE_CATEGORY_LABELS.get(category, category)

    text = (
        "📬 <b>رسالة طالب</b>\n\n"
        f"🆔 <code>{message_id}</code>\n"
        f"👤 {esc(user_name or 'طالب')} (<code>{user_id}</code>)\n"
        f"🏷 {esc(category_label)}\n"
        f"📊 الحالة: {esc(status_label)}\n"
        f"🕒 {esc(created_at)}\n\n"
        f"📝 {esc(body)}"
    )

    if admin_reply:
        text += f"\n\n↩️ <b>الرد الحالي:</b> {esc(admin_reply)}"

    rows = [
        [btn("✏️ رد", f"msg_reply:{message_id}")],
        [btn("👀 قيد المراجعة", f"msg_status:{message_id}:IN_REVIEW")],
        [btn("🔒 إغلاق", f"msg_status:{message_id}:CLOSED")],
        [btn("⬅️ الرسائل", "admin_messages")],
        [btn("🏠 الرئيسية", "home")],
    ]

    await _edit(query, text, InlineKeyboardMarkup(rows))


async def request_admin_reply(query, context, message_id):
    """Arm the reply text flow for this admin."""
    if not await _is_admin(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", HOME_KEYBOARD)
        return

    if not await _can_messages(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", HOME_KEYBOARD)
        return

    try:
        record = await database.get_message(message_id)
    except Exception:
        record = None

    if not record:
        await _edit(query, "⚠️ الرسالة غير موجودة.", HOME_KEYBOARD)
        return

    if record[5] == "CLOSED":
        await _edit(
            query,
            "🔒 هذه الرسالة مغلقة ولا يمكن الرد عليها.",
            InlineKeyboardMarkup(
                [
                    [btn("📬 الرسائل", "admin_messages")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    context.user_data["contact_reply_id"] = int(message_id)

    await _edit(
        query,
        "✏️ <b>الرد على الرسالة</b>\n\n"
        f"اكتب نص الرد للرسالة <code>{message_id}</code> وأرسله.\n\n"
        "لإلغاء العملية أرسل /cancel.",
        InlineKeyboardMarkup(
            [
                [btn("⬅️ الرسالة", f"msg_open:{message_id}")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


async def handle_admin_reply_text(update, context):
    """Capture an admin reply. Returns True when handled here."""
    message_id = context.user_data.get("contact_reply_id")

    if not message_id:
        return False

    if not update.message or not update.message.text:
        return False

    if not await _is_admin(update.effective_user.id):
        _clear_contact_state(context)
        await _reply(update, "🔒 غير مصرح.", HOME_KEYBOARD)
        return True

    text = update.message.text.strip()

    if text == "/cancel":
        _clear_contact_state(context)
        await _reply(update, "❌ تم إلغاء الرد.", HOME_KEYBOARD)
        return True

    try:
        result = await database.reply_to_message(
            int(message_id),
            update.effective_user.id,
            text,
        )
    except Exception:
        logger.exception("messaging: reply_to_message failed")
        result = None

    _clear_contact_state(context)

    if not result:
        await _reply(
            update,
            "ℹ️ تعذر الرد. الرسالة غير موجودة أو مغلقة.",
            InlineKeyboardMarkup(
                [
                    [btn("📬 الرسائل", "admin_messages")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return True

    owner_id, mid = result

    await _reply(
        update,
        f"✅ تم إرسال الرد على الرسالة <code>{mid}</code>.",
        InlineKeyboardMarkup(
            [
                [btn("📬 الرسائل", "admin_messages")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )

    try:
        await context.bot.send_message(
            chat_id=owner_id,
            text=(
                "📬 <b>رد الإدارة على رسالتك</b>\n\n"
                f"🆔 <code>{mid}</code>\n\n"
                f"↩️ {esc(text)}"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.warning("messaging: could not notify owner %s", owner_id)

    await audit.log_action(
        update.effective_user.id,
        "message_reply",
        target_type="message",
        target_id=mid,
    )

    return True


async def change_status(query, context, message_id, status):
    if not await _is_admin(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", HOME_KEYBOARD)
        return

    if not await _can_messages(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", HOME_KEYBOARD)
        return

    if status not in database.MESSAGE_STATUSES:
        await _edit(query, "⚠️ حالة غير معروفة.", HOME_KEYBOARD)
        return

    try:
        ok = await database.set_message_status(message_id, status)
    except Exception:
        logger.exception("messaging: set_message_status failed")
        ok = False

    if not ok:
        await _edit(
            query,
            "⚠️ تعذر تحديث الحالة.",
            InlineKeyboardMarkup(
                [
                    [btn("📬 الرسائل", "admin_messages")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return

    await audit.log_action(
        query.from_user.id,
        "message_status",
        target_type="message",
        target_id=message_id,
        details=f"status={status}",
    )

    await open_admin_message(query, context, message_id)


# ---------------------------------------------------------------
# Router
# ---------------------------------------------------------------


async def messaging_callback_handler(update, context):
    query = update.callback_query

    try:
        await query.answer()
    except Exception:
        pass

    data = query.data or ""

    if data == "contact":
        await contact_admin_screen(query, context)
        return

    if data.startswith("msg_cat:"):
        await select_category(query, context, data.split(":", 1)[1])
        return

    if data == "msg_cancel":
        _clear_contact_state(context)
        await contact_admin_screen(query, context)
        return

    if data == "msg_mine":
        _clear_contact_state(context)
        await show_my_messages(query)
        return

    if data == "admin_messages":
        _clear_contact_state(context)
        await show_admin_messages(query)
        return

    if data.startswith("msg_open:"):
        try:
            message_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await _edit(query, "⚠️ معرف غير صالح.", HOME_KEYBOARD)
            return
        await open_admin_message(query, context, message_id)
        return

    if data.startswith("msg_reply:"):
        try:
            message_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await _edit(query, "⚠️ معرف غير صالح.", HOME_KEYBOARD)
            return
        await request_admin_reply(query, context, message_id)
        return

    if data.startswith("msg_status:"):
        parts = data.split(":")
        try:
            message_id = int(parts[1])
            status = parts[2]
        except (IndexError, TypeError, ValueError):
            await _edit(query, "⚠️ طلب غير صالح.", HOME_KEYBOARD)
            return
        await change_status(query, context, message_id, status)
        return


async def contact_command(update, context):
    """`/contact` shortcut for the contact screen."""
    if not update.message:
        return
    _clear_contact_state(context)
    await _reply(
        update,
        "📬 <b>التواصل مع الإدارة</b>\n\n"
        "اختر نوع الرسالة التي تريد إرسالها.",
        _contact_keyboard(),
    )


def register_messaging_handlers(app):
    """Register all Contact Admin handlers. Mirrors mcq_quiz registration."""
    app.add_handler(CommandHandler("contact", contact_command))
    app.add_handler(
        CallbackQueryHandler(
            messaging_callback_handler,
            pattern=r"^(contact|msg_cat:|msg_cancel|msg_mine|admin_messages|msg_open:|msg_reply:|msg_status:)",
        )
    )
