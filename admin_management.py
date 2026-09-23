"""Owner-only admin management (RBAC control surface) for MEDBOT.

Isolated like `messaging.py` and `audit.py`: this module owns the role and
permission management UI and its own handler set. Every callback re-checks
owner/`can_admins` authorization, and every mutation writes an audit entry via
`audit.log_action`.

Integration contract (see `register_admin_management_handlers`):
    admin_management.register_admin_management_handlers(app)

State keys used (namespaced):
    admin_mgmt_waiting_add -> owner is typing a Telegram ID / @username to add
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
    """Owner, or a sub-admin holding `can_admins`."""
    try:
        if await database.is_owner(user_id):
            return True
        return await database.user_has_permission(user_id, "can_admins")
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
            logger.warning("admin_management: edit_message_text failed")


def _admins_menu(admins) -> InlineKeyboardMarkup:
    rows = []
    for record in admins:
        telegram_id = record["telegram_id"]
        role_label = database.ROLE_LABELS.get(record["role"], record["role"])
        name = str(record.get("username") or "").strip() or "بدون اسم"
        rows.append(
            [btn(f"{role_label} | {name[:18]} | {telegram_id}", f"amg_view:{telegram_id}")]
        )
    rows.append([btn("➕ إضافة مشرف", "amg_add")])
    rows.append([btn("⬅️ Admin", "admin")])
    rows.append([btn("🏠 الرئيسية", "home")])
    return InlineKeyboardMarkup(rows)


async def _load_admins():
    """All admin records, resolved through the RBAC helpers."""
    try:
        return await database.get_admins_full_records()
    except Exception:
        return []


async def show_admin_management(query, context=None):
    """Owner-only list of admins with role + permission controls."""
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    if context is not None:
        context.user_data.pop("admin_mgmt_waiting_add", None)

    admins = await _load_admins()

    lines = ["👥 <b>إدارة المشرفين</b>", f"📦 العدد: {len(admins)}\n",
             "اختر مشرفاً لتعديل دوره وصلاحياته."]
    await _edit(query, "\n".join(lines), _admins_menu(admins))


async def show_admin_detail(query, target_id):
    """Per-admin detail: role buttons + permission toggles."""
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    record = await database.get_admin_record(target_id)

    if not record:
        await _edit(
            query,
            "⚠️ المشرف غير موجود.",
            InlineKeyboardMarkup([[btn("⬅️ إدارة المشرفين", "amg_list")]]),
        )
        return

    role_label = database.ROLE_LABELS.get(record["role"], record["role"])
    name = str(record.get("username") or "").strip() or "بدون اسم"

    lines = [
        "👤 <b>بيانات المشرف</b>",
        f"🆔 <code>{esc(target_id)}</code>",
        f"📝 الاسم: {esc(name)}",
        f"👑 الدور: {role_label}\n",
        "🔐 <b>الصلاحيات</b>",
    ]

    rows = []
    perm_buttons = []
    for key in database.PERMISSION_KEYS:
        granted = bool(record["permissions"].get(key))
        mark = "✅" if granted else "⛔"
        short = database.PERMISSION_SHORT_LABELS.get(
            key, database.PERMISSION_LABELS.get(key, key)
        )
        lines.append(f"{mark} {esc(database.PERMISSION_LABELS.get(key, key))}")
        perm_buttons.append(btn(f"{mark} {short}", f"amg_perm:{target_id}:{key}"))

    # Two toggles per row keeps the keyboard inside Telegram's width and
    # avoids a very tall menu now that the permission set is larger.
    for i in range(0, len(perm_buttons), 2):
        rows.append(perm_buttons[i:i + 2])

    lines.append("\n👑 <b>الدور</b>")
    for role in database.ROLES:
        label = database.ROLE_LABELS.get(role, role)
        mark = "✅" if record["role"] == role else "▫️"
        rows.append([btn(f"{mark} {label}", f"amg_role:{target_id}:{role}")])

    # Ownership transfer is a distinct, owner-only operation: it swaps two
    # roles atomically so the platform is never left without an owner.
    if await database.is_owner(query.from_user.id) and record["role"] != "owner":
        rows.append([btn("👑 نقل الملكية", f"amg_transfer:{target_id}")])

    rows.append([btn("🗑 إزالة المشرف", f"amg_remove:{target_id}")])
    rows.append([btn("⬅️ إدارة المشرفين", "amg_list")])
    rows.append([btn("🏠 الرئيسية", "home")])

    await _edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def confirm_transfer(query, target_id):
    """Owner-only confirmation screen before an ownership transfer."""
    if not await database.is_owner(query.from_user.id):
        await _edit(query, "🔒 نقل الملكية متاح للمالك فقط.", _home_keyboard())
        return

    record = await database.get_admin_record(target_id)
    if not record:
        await _edit(
            query,
            "⚠️ المشرف غير موجود.",
            InlineKeyboardMarkup([[btn("⬅️ إدارة المشرفين", "amg_list")]]),
        )
        return

    if record["role"] == "owner":
        await _edit(
            query,
            "ℹ️ هذا الحساب هو المالك بالفعل.",
            InlineKeyboardMarkup([[btn("⬅️ إدارة المشرفين", "amg_list")]]),
        )
        return

    if record["role"] not in database.ADMIN_ROLES:
        await _edit(
            query,
            "⚠️ لا يمكن نقل الملكية إلى حساب ملغى.",
            InlineKeyboardMarkup([[btn("⬅️ إدارة المشرفين", "amg_list")]]),
        )
        return

    name = str(record.get("username") or "").strip() or "بدون اسم"
    await _edit(
        query,
        "👑 <b>نقل الملكية</b>\n\n"
        f"سيصبح الحساب <code>{esc(target_id)}</code> ({esc(name)}) "
        "هو المالك الجديد، وسيتحوّل دورك إلى «مشرف».\n\n"
        "هل تريد المتابعة؟",
        InlineKeyboardMarkup(
            [
                [btn("✅ تأكيد نقل الملكية", f"amg_transfer_confirm:{target_id}")],
                [btn("❌ إلغاء", f"amg_view:{target_id}")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


async def execute_transfer(query, target_id):
    if not await database.is_owner(query.from_user.id):
        await _edit(query, "🔒 نقل الملكية متاح للمالك فقط.", _home_keyboard())
        return

    ok, message = await database.transfer_ownership(
        query.from_user.id, target_id
    )

    if not ok:
        await _edit(
            query,
            f"⚠️ {esc(message)}",
            InlineKeyboardMarkup([[btn("⬅️ إدارة المشرفين", "amg_list")]]),
        )
        return

    await audit.log_action(
        query.from_user.id,
        "ownership_transfer",
        target_type="admin",
        target_id=target_id,
    )

    await _edit(
        query,
        "✅ <b>تم نقل الملكية بنجاح.</b>\n\n"
        f"👑 المالك الجديد: <code>{esc(target_id)}</code>\n"
        "🛡 دورك الحالي: مشرف\n\n"
        "سيتم تطبيق دور المالك الجديد الكامل من هذه اللحظة.",
        InlineKeyboardMarkup(
            [
                [btn("👥 إدارة المشرفين", "amg_list")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


async def toggle_permission(query, target_id, permission):
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    record = await database.get_admin_record(target_id)
    if not record:
        await _edit(
            query,
            "⚠️ المشرف غير موجود.",
            InlineKeyboardMarkup([[btn("⬅️ إدارة المشرفين", "amg_list")]]),
        )
        return

    updated = dict(record["permissions"])
    updated[permission] = not updated.get(permission)

    ok = await database.update_admin_permissions(target_id, updated)

    if ok:
        await audit.log_action(
            query.from_user.id,
            "admin_permissions",
            target_type="admin",
            target_id=target_id,
            details=f"{permission}={'grant' if updated[permission] else 'revoke'}",
        )

    await show_admin_detail(query, target_id)


async def change_role(query, target_id, role):
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    if role not in database.ROLES:
        await _edit(query, "⚠️ دور غير مدعوم.", _home_keyboard())
        return

    # Only the configured owner may mint another owner.
    if role == "owner" and not await database.is_owner(query.from_user.id):
        await _edit(query, "🔒 تعيين المالك متاح للمالك فقط.", _home_keyboard())
        return

    record = await database.get_admin_record(target_id)
    if not record:
        await _edit(
            query,
            "⚠️ المشرف غير موجود.",
            InlineKeyboardMarkup([[btn("⬅️ إدارة المشرفين", "amg_list")]]),
        )
        return

    # The active owner must not be demoted; a new owner has to exist first.
    if (
        role != "owner"
        and await database.is_owner(target_id)
    ):
        await _edit(
            query,
            "🔒 لا يمكن تغيير دور المالك الحالي إلى مشرف.\n"
            "عيّن حساباً آخر كمالك أولاً، ثم غيّر هذا الحساب.",
            InlineKeyboardMarkup([[btn("⬅️ إدارة المشرفين", "amg_list")]]),
        )
        return

    ok = await database.set_admin_role(target_id, role)

    if ok:
        await audit.log_action(
            query.from_user.id,
            "admin_role",
            target_type="admin",
            target_id=target_id,
            details=f"role={role}",
        )
    else:
        await _edit(
            query,
            "⚠️ تعذّر تغيير الدور.",
            InlineKeyboardMarkup([[btn("⬅️ إدارة المشرفين", "amg_list")]]),
        )
        return

    await show_admin_detail(query, target_id)


async def remove_admin(query, target_id):
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    # The configured owner must never be removable through the UI.
    if await database.is_owner(target_id):
        await _edit(
            query,
            "🔒 لا يمكن إزالة المالك.",
            InlineKeyboardMarkup([[btn("⬅️ إدارة المشرفين", "amg_list")]]),
        )
        return

    ok = await database.remove_sub_admin(target_id)

    if ok:
        await audit.log_action(
            query.from_user.id,
            "admin_remove",
            target_type="admin",
            target_id=target_id,
        )
    else:
        await _edit(
            query,
            "⚠️ تعذّر إلغاء وصول هذا المشرف.",
            InlineKeyboardMarkup([[btn("⬅️ إدارة المشرفين", "amg_list")]]),
        )
        return

    await show_admin_management(query)


async def start_add_admin(query, context):
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    context.user_data["admin_mgmt_waiting_add"] = True

    await _edit(
        query,
        "➕ <b>إضافة مشرف</b>\n\n"
        "أرسل الـ Telegram ID الرقمي أو @username.\n"
        "لن يحصل المشرف الجديد على أي صلاحية حتى تمنحها له.",
        InlineKeyboardMarkup([[btn("⬅️ إدارة المشرفين", "amg_list")]]),
    )


async def handle_add_admin_text(update, context):
    """Consume the owner's typed identifier for adding a sub-admin."""
    if not context.user_data.get("admin_mgmt_waiting_add"):
        return False

    if not update.message or not update.message.text:
        return False

    context.user_data.pop("admin_mgmt_waiting_add", None)

    if not await _is_authorized(update.effective_user.id):
        await update.message.reply_text("🔒 غير مصرح.", reply_markup=_home_keyboard())
        return True

    identifier = update.message.text.strip()

    # `add_sub_admin_by_any` uses INSERT OR REPLACE, which would reset an
    # existing row's role/permissions. Never let that touch the owner.
    digits = "".join(ch for ch in identifier if ch.isdigit())
    if digits and await database.is_owner(int(digits)):
        await update.message.reply_text(
            "🔒 هذا المعرف هو المالك الحالي ولا يمكن إضافته كمشرف.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [btn("👥 إدارة المشرفين", "amg_list")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return True

    ok, message = await database.add_sub_admin_by_any(identifier)

    if ok:
        # A newly added admin starts least-privilege: an explicit all-False map
        # (never an empty column, which would mean legacy full access).
        try:
            new_id = int("".join(ch for ch in identifier if ch.isdigit()) or 0)
            if new_id > 0:
                await database.set_admin_role(new_id, "admin")
                await database.update_admin_permissions(
                    new_id, {key: False for key in database.PERMISSION_KEYS}
                )
        except Exception:
            pass

        await audit.log_action(
            update.effective_user.id,
            "admin_add",
            target_type="admin",
            target_id=identifier,
        )

    await update.message.reply_text(
        f"{'✅' if ok else '⚠️'} {esc(message)}",
        reply_markup=InlineKeyboardMarkup(
            [
                [btn("👥 إدارة المشرفين", "amg_list")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )
    return True


async def admin_management_callback_handler(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    try:
        await query.answer()
    except Exception:
        pass

    data = query.data or ""

    if data == "amg_list":
        await show_admin_management(query, context)
        return

    if data == "amg_add":
        await start_add_admin(query, context)
        return

    if data.startswith("amg_view:"):
        try:
            target_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await show_admin_detail(query, target_id)
        return

    if data.startswith("amg_perm:"):
        parts = data.split(":")
        if len(parts) != 3:
            await _edit(query, "⚠️ بيانات غير صالحة.", _home_keyboard())
            return
        try:
            target_id = int(parts[1])
        except (TypeError, ValueError):
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await toggle_permission(query, target_id, parts[2])
        return

    if data.startswith("amg_role:"):
        parts = data.split(":")
        if len(parts) != 3:
            await _edit(query, "⚠️ بيانات غير صالحة.", _home_keyboard())
            return
        try:
            target_id = int(parts[1])
        except (TypeError, ValueError):
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await change_role(query, target_id, parts[2])
        return

    if data.startswith("amg_transfer_confirm:"):
        try:
            target_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await execute_transfer(query, target_id)
        return

    if data.startswith("amg_transfer:"):
        try:
            target_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await confirm_transfer(query, target_id)
        return

    if data.startswith("amg_remove:"):
        try:
            target_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await remove_admin(query, target_id)
        return

    # Unknown admin-management callback: fail safely.
    await _edit(query, "⚠️ إجراء غير معروف.", _home_keyboard())


def register_admin_management_handlers(app):
    """Register the owner-only admin management surface."""
    app.add_handler(
        CallbackQueryHandler(
            admin_management_callback_handler,
            pattern=r"^(amg_list|amg_add|amg_view:|amg_perm:|amg_role:|amg_remove:|amg_transfer)",
        )
    )
