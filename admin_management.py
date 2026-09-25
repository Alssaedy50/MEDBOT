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
import authorization
import database
import workflow

logger = logging.getLogger(__name__)


def _int_or_none(value):
    try:
        if value is None or value is True or value is False:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


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
    rows.append([btn("⚙️ إدارة الصلاحيات", "amg_perms_guide")])
    rows.append([btn("ℹ️ صلاحيات الأدوار", "amg_roles")])
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


async def show_role_reference(query):
    """Explain the scope of every role (owner / admin / reviewer)."""
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    lines = ["👑 <b>صلاحيات الأدوار</b>", ""]
    for role in ("owner", "admin", "reviewer"):
        label = database.ROLE_LABELS.get(role, role)
        lines.append(f"<b>{esc(label)}</b>")
        lines.append(esc(database.ROLE_DESCRIPTIONS.get(role, "")))
        lines.append("")

    await _edit(
        query,
        "\n".join(lines).rstrip(),
        InlineKeyboardMarkup(
            [
                [btn("⬅️ إدارة المشرفين", "amg_list")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


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
    target_is_owner = record["role"] == "owner"
    # `owner` is not assignable from the role list: it is only reachable through
    # the explicit "نقل الملكية" action, so two owners can never exist.
    for role in database.ROLE_ASSIGNABLE:
        label = database.ROLE_LABELS.get(role, role)
        mark = "✅" if record["role"] == role else "▫️"
        rows.append([btn(f"{mark} {label}", f"amg_role:{target_id}:{role}")])

    if target_is_owner:
        lines.append(f"✅ {esc(database.ROLE_LABELS['owner'])}")

    lines.append("")
    lines.append(esc(database.ROLE_DESCRIPTIONS.get(record["role"], "")))

    # Ownership transfer is a distinct, owner-only operation: it swaps two
    # roles atomically so the platform is never left without an owner.
    if await database.is_owner(query.from_user.id) and not target_is_owner:
        rows.append([btn("👑 نقل الملكية", f"amg_transfer:{target_id}")])

    # Read-only preview: let the actor review what this admin actually sees and
    # can manage, without changing anyone's Telegram identity.
    rows.append([btn("👁 معاينة واجهة المشرف", f"amg_preview:{target_id}")])

    # Phase 3 — scoped responsibility (folder / topic / resource). Only
    # meaningful for a non-owner; the owner is unrestricted by design.
    if not target_is_owner:
        scopes = await database.get_admin_scopes(target_id)
        scope_note = f" ({len(scopes)})" if scopes else ""
        rows.append([btn(f"🧭 نطاقات المسؤولية{scope_note}", f"amg_scopes:{target_id}")])

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
        "🛡 دورك الحالي: مشرف (لمالك واحد فقط في المنصة).\n\n"
        "سيتم تطبيق دور المالك الجديد الكامل من هذه اللحظة.",
        InlineKeyboardMarkup(
            [
                [btn("👥 إدارة المشرفين", "amg_list")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


async def show_permissions_guide(query):
    """The '⚙️ إدارة الصلاحيات' surface: roles + scoped operations explained.

    Read-only guidance for owners so the permission/scope model is explicit.
    Gated by `can_admins`; a scoped admin never reaches it (they lack
    `can_admins`, and the panel hides the entry for them).
    """
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    lines = [
        "⚙️ <b>إدارة الصلاحيات والنطاقات</b>",
        "",
        "النموذج: <b>الدور + الصلاحيات + النطاقات</b>.",
        "«المشرف» يحصل على صلاحيات محددة، ويمكن تقييده بنطاق "
        "(قسم / موضوع / مورد) فلا يعمل إلا داخله.",
        "",
        "🛡 <b>الصلاحيات المتاحة (Scope-aware)</b>",
    ]
    for key in database.SCOPED_PERMISSIONS:
        lines.append(f"• {esc(database.SCOPED_PERMISSION_LABELS.get(key, key))}")

    lines.append("")
    lines.append("🧭 <b>النطاقات</b>")
    for key in database.SCOPE_TYPES:
        lines.append(f"• {esc(database.SCOPE_TYPE_LABELS.get(key, key))}")

    lines.append("")
    lines.append(
        "ℹ️ المشرف بلا نطاقات = وصول كامل بحسب صلاحياته. "
        "بمجرد إضافة نطاق واحد يصبح مقيّدًا به."
    )

    await _edit(
        query,
        "\n".join(lines),
        InlineKeyboardMarkup(
            [
                [btn("👥 إدارة المشرفين", "amg_list")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


def _scopes_keyboard(target_id, scopes) -> InlineKeyboardMarkup:
    rows = []
    for scope in scopes:
        label = database.SCOPE_TYPE_LABELS.get(scope["scope_type"], scope["scope_type"])
        rows.append(
            [
                btn(
                    f"🗑 {label} #{scope['scope_id']}",
                    f"amg_scope_view:{target_id}:{scope['scope_type']}:{scope['scope_id']}",
                )
            ]
        )
    rows.append([btn("➕ إضافة نطاق", f"amg_scope_add:{target_id}")])
    rows.append([btn("⬅️ بيانات المشرف", f"amg_view:{target_id}")])
    rows.append([btn("🏠 الرئيسية", "home")])
    return InlineKeyboardMarkup(rows)


async def show_admin_scopes(query, target_id):
    """List one admin's scopes with add/remove controls. Owner-gated."""
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

    scopes = await database.get_admin_scopes(target_id)
    name = str(record.get("username") or "").strip() or "بدون اسم"

    lines = [
        "🧭 <b>نطاقات المشرف</b>",
        f"🆔 <code>{esc(target_id)}</code> · {esc(name)}",
        "",
    ]

    if scopes:
        lines.append("النطاقات الحالية (يعمل داخلها فقط):")
        for scope in scopes:
            label = database.SCOPE_TYPE_LABELS.get(
                scope["scope_type"], scope["scope_type"]
            )
            lines.append(f"• {esc(label)} — <code>{esc(scope['scope_id'])}</code>")
    else:
        lines.append(
            "لا توجد نطاقات — هذا المشرف غير مقيّد ويعمل على كامل المنصة "
            "بحسب صلاحياته."
        )

    lines.append("")
    lines.append(
        "ℹ️ اضغط على نطاق لسحبه، أو أضف نطاقًا جديدًا (قسم / موضوع / مورد)."
    )

    await _edit(query, "\n".join(lines), _scopes_keyboard(target_id, scopes))


async def revoke_admin_scope(query, target_id, scope_type, scope_id):
    """Remove one scope and audit it. Refuses the owner."""
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    if await database.is_owner(target_id):
        # The owner is unrestricted by design; scoping them is meaningless and
        # could look like a grant. Refuse explicitly.
        await _edit(
            query,
            "ℹ️ المالك غير مقيّد بنطاقات.",
            InlineKeyboardMarkup([[btn("⬅️ بيانات المشرف", f"amg_view:{target_id}")]]),
        )
        return

    ok = await database.remove_admin_scope(target_id, scope_type, scope_id)
    if ok:
        await audit.log_action(
            query.from_user.id,
            "scope_revoke",
            target_type=scope_type,
            target_id=scope_id,
            details=f"admin={target_id}",
        )

    await show_admin_scopes(query, target_id)


async def show_scope_add_menu(query, target_id):
    """Choose the scope type to add: folder / topic / resource."""
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

    await _edit(
        query,
        "➕ <b>إضافة نطاق</b>\n\n"
        "اختر نوع النطاق الذي سيكون المشرف مسؤولًا عنه:",
        InlineKeyboardMarkup(
            [
                [btn("🗂 قسم", f"amg_scope_roots:{target_id}:folder")],
                [btn("🧭 موضوع", f"amg_scope_add_topic:{target_id}")],
                [btn("📄 مورد", f"amg_scope_res_roots:{target_id}")],
                [btn("⬅️ نطاقات المشرف", f"amg_scopes:{target_id}")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )


async def _folder_pick_keyboard(target_id, scope_type, parent_id, folders):
    rows = []
    if scope_type == "folder":
        rows.append(
            [btn("✅ اختيار هذا القسم", f"amg_scope_set:{target_id}:folder:{parent_id}")]
        )
    for folder in folders:
        try:
            folder_id, name = folder[0], folder[1]
        except Exception:
            continue
        rows.append(
            [
                btn(
                    f"📁 {str(name)[:30]}",
                    f"amg_scope_browse:{target_id}:{scope_type}:{folder_id}",
                ),
                btn("✅", f"amg_scope_set:{target_id}:{scope_type}:{folder_id}"),
            ]
        )
    rows.append([btn("⬅️ رجوع", f"amg_scopes:{target_id}")])
    rows.append([btn("🏠 الرئيسية", "home")])
    return InlineKeyboardMarkup(rows)


async def browse_scope_folders(query, target_id, scope_type, parent_id=0):
    """Browse the real folder tree to pick a folder scope (or a resource)."""
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    try:
        folders = await database.get_folders(parent_id)
    except Exception:
        folders = []

    if scope_type == "resource":
        # Pick a folder, then a resource inside it.
        rows = []
        for folder in folders:
            try:
                folder_id, name = folder[0], folder[1]
            except Exception:
                continue
            rows.append(
                [btn(f"📁 {str(name)[:30]}", f"amg_scope_res:{target_id}:{folder_id}")]
            )
        rows.append([btn("⬅️ رجوع", f"amg_scopes:{target_id}")])
        rows.append([btn("🏠 الرئيسية", "home")])
        await _edit(
            query,
            "📄 <b>اختيار مورد</b>\n\nاختر القسم الذي يحتوي المورد:",
            InlineKeyboardMarkup(rows),
        )
        return

    await _edit(
        query,
        "🗂 <b>اختيار قسم</b>\n\n"
        "ادخل إلى القسم المطلوب ثم اضغط «✅ اختيار هذا القسم».",
        _folder_pick_keyboard(target_id, scope_type, parent_id, folders),
    )


async def pick_scope_resources(query, target_id, folder_id):
    """List the real resources in a folder to pick one as a resource scope."""
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    try:
        files = await database.get_files(folder_id)
    except Exception:
        files = []

    rows = []
    for item in files:
        try:
            content_id, title = item[0], item[1]
        except Exception:
            continue
        rows.append(
            [
                btn(
                    f"📄 {str(title)[:30]}",
                    f"amg_scope_set:{target_id}:resource:{content_id}",
                )
            ]
        )

    if not files:
        rows.append([btn("ℹ️ لا توجد موارد في هذا القسم", "noop")])

    rows.append([btn("⬅️ رجوع", f"amg_scope_res_roots:{target_id}")])
    rows.append([btn("🏠 الرئيسية", "home")])

    await _edit(
        query,
        "📄 <b>اختيار مورد</b>\n\nاختر المورد الذي سيكون نطاقًا للمشرف:",
        InlineKeyboardMarkup(rows),
    )


async def pick_scope_topic(query, target_id):
    """List the real Search Topics to pick one as a topic scope."""
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    try:
        topics = await database.get_topics(active_only=False)
    except Exception:
        topics = []

    rows = []
    for topic in topics:
        try:
            topic_id, name = topic["id"], topic["name"]
        except Exception:
            continue
        rows.append(
            [
                btn(
                    f"🧭 {str(name)[:30]}",
                    f"amg_scope_set:{target_id}:topic:{topic_id}",
                )
            ]
        )

    if not topics:
        rows.append([btn("ℹ️ لا توجد مواضيع", "noop")])

    rows.append([btn("⬅️ رجوع", f"amg_scope_add:{target_id}")])
    rows.append([btn("🏠 الرئيسية", "home")])

    await _edit(
        query,
        "🧭 <b>اختيار موضوع</b>\n\n"
        "سيشمل النطاق كل الأقسام المرتبطة بهذا الموضوع:",
        InlineKeyboardMarkup(rows),
    )


async def set_admin_scope(query, target_id, scope_type, scope_id):
    """Grant one scope (validated against the live registry) and audit it."""
    if not await _is_authorized(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    if await database.is_owner(target_id):
        await _edit(
            query,
            "ℹ️ المالك غير مقيّد بنطاقات.",
            InlineKeyboardMarkup([[btn("⬅️ بيانات المشرف", f"amg_view:{target_id}")]]),
        )
        return

    record = await database.get_admin_record(target_id)
    if not record:
        await _edit(
            query,
            "⚠️ المشرف غير موجود.",
            InlineKeyboardMarkup([[btn("⬅️ إدارة المشرفين", "amg_list")]]),
        )
        return

    ok = await database.add_admin_scope(
        target_id, scope_type, scope_id, created_by=query.from_user.id
    )

    if not ok:
        await _edit(
            query,
            "⚠️ تعذّر إضافة النطاق (العنصر غير موجود).",
            InlineKeyboardMarkup([[btn("⬅️ نطاقات المشرف", f"amg_scopes:{target_id}")]]),
        )
        return

    await audit.log_action(
        query.from_user.id,
        "scope_grant",
        target_type=scope_type,
        target_id=scope_id,
        details=f"admin={target_id}",
    )

    await show_admin_scopes(query, target_id)


async def show_admin_preview(query, target_id):
    """Read-only preview of what a given admin sees inside MEDBOT.

    This is an admin-preview/impersonation aid, not a session switch: the
    acting admin's Telegram identity is never changed, no session is created or
    faked, and the target's role/permissions are never mutated. It renders the
    target's stored role and effective capabilities from the RBAC tables so the
    acting admin can review their access, then jump to the real permission
    controls to change it.
    """
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

    role = record["role"]
    role_label = database.ROLE_LABELS.get(role, role)
    name = str(record.get("username") or "").strip() or "بدون اسم"
    permissions = await database.get_admin_permissions(target_id)

    lines = [
        "👁 <b>معاينة واجهة المشرف</b>",
        f"🆔 <code>{esc(target_id)}</code>",
        f"📝 الاسم: {esc(name)}",
        f"👑 الدور: {role_label}",
        "",
        "🔐 <b>ما يستطيع هذا الحساب الوصول إليه</b>",
    ]

    granted = [
        database.PERMISSION_LABELS.get(key, key)
        for key in database.PERMISSION_KEYS
        if permissions.get(key)
    ]

    if granted:
        for label in granted:
            lines.append(f"✅ {esc(label)}")
    else:
        lines.append("⛔ لا يملك أي صلاحية إدارية حالياً.")

    lines.append("")
    lines.append(esc(database.ROLE_DESCRIPTIONS.get(role, "")))
    lines.append("")
    lines.append(
        "ℹ️ هذه معاينة للقراءة فقط: هويتك في Telegram لم تتغير، ولا توجد جلسة "
        "مزيفة. للتحكم في الوصول استخدم أزرار الصلاحيات."
    )

    await audit.log_action(
        query.from_user.id,
        "admin_preview",
        target_type="admin",
        target_id=target_id,
    )

    await _edit(
        query,
        "\n".join(lines),
        InlineKeyboardMarkup(
            [
                [btn("🔐 تعديل صلاحيات هذا المشرف", f"amg_view:{target_id}")],
                [btn("⬅️ إدارة المشرفين", "amg_list")],
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

    # `owner` is transfer-only; promoting through the role list is what could
    # create two owners, so it is refused here with a pointer to the right flow.
    if role == "owner":
        await _edit(
            query,
            "👑 لتعيين مالك جديد استخدم «نقل الملكية» من صفحة المشرف.\n"
            "بهذه الطريقة يبقى مالك واحد فقط في المنصة.",
            InlineKeyboardMarkup([[btn("⬅️ إدارة المشرفين", "amg_list")]]),
        )
        return

    if role not in database.ROLE_ASSIGNABLE:
        await _edit(query, "⚠️ دور غير مدعوم.", _home_keyboard())
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
    if await database.is_owner(target_id):
        await _edit(
            query,
            "🔒 لا يمكن تغيير دور المالك الحالي إلى مشرف.\n"
            "انقل الملكية إلى حساب آخر أولاً.",
            InlineKeyboardMarkup([[btn("⬅️ إدارة المشرفين", "amg_list")]]),
        )
        return

    # Choosing a role also applies its baseline permission scope.
    ok = await database.apply_role_preset(target_id, role)

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

    workflow.begin(context, "admin_add")
    context.user_data["admin_mgmt_waiting_add"] = True

    await _edit(
        query,
        "➕ <b>إضافة مشرف</b>\n\n"
        "أرسل الـ Telegram ID الرقمي أو @username.\n"
        "يُتعرَّف على أي حساب استخدم البوت من قبل تلقائياً؛ لا حاجة لأن يرسل "
        "/start مرة أخرى.\n"
        "لن يحصل المشرف الجديد على أي صلاحية حتى تمنحها له.",
        InlineKeyboardMarkup([[btn("⬅️ إدارة المشرفين", "amg_list")]]),
    )


async def handle_add_admin_text(update, context):
    """Consume the owner's typed identifier for adding a sub-admin."""
    if not context.user_data.get("admin_mgmt_waiting_add"):
        return False

    if not update.message or not update.message.text:
        return False

    if not workflow.owns(context, "admin_add"):
        return False

    context.user_data.pop("admin_mgmt_waiting_add", None)

    if not await _is_authorized(update.effective_user.id):
        await update.message.reply_text("🔒 غير مصرح.", reply_markup=_home_keyboard())
        return True

    identifier = update.message.text.strip()

    ok, message = await database.add_sub_admin_by_any(identifier)

    if ok:
        # A newly added admin starts least-privilege: an explicit all-False map
        # (never an empty column, which would mean legacy full access).
        #
        # `add_sub_admin_by_any` uses INSERT OR REPLACE, which resets role and
        # permissions. Re-adding an existing (non-owner) admin would therefore
        # wipe its role/perms, so the resolved row is repaired afterwards.
        # The owner row is never touched by the add path.
        try:
            resolved_id, _, _ = await database.resolve_user_by_identifier(identifier)

            if resolved_id and not await database.is_owner(resolved_id):
                await database.set_admin_role(resolved_id, "admin")
                await database.update_admin_permissions(
                    resolved_id, {key: False for key in database.PERMISSION_KEYS}
                )
        except Exception:
            logger.exception("admin_management: failed to normalise new admin")

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

    if data == "amg_roles":
        await show_role_reference(query)
        return

    if data == "amg_perms_guide":
        await show_permissions_guide(query)
        return

    if data.startswith("amg_scopes:"):
        target_id = _int_or_none(data.split(":", 1)[1])
        if target_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await show_admin_scopes(query, target_id)
        return

    if data.startswith("amg_scope_add:"):
        target_id = _int_or_none(data.split(":", 1)[1])
        if target_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await show_scope_add_menu(query, target_id)
        return

    if data.startswith("amg_scope_add_topic:"):
        target_id = _int_or_none(data.split(":", 1)[1])
        if target_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await pick_scope_topic(query, target_id)
        return

    if data.startswith("amg_scope_roots:") or data.startswith("amg_scope_res_roots:"):
        parts = data.split(":")
        if len(parts) < 3:
            await _edit(query, "⚠️ بيانات غير صالحة.", _home_keyboard())
            return
        target_id = _int_or_none(parts[1])
        if target_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        scope_type = parts[2] if parts[0] == "amg_scope_roots" else "resource"
        await browse_scope_folders(query, target_id, scope_type, 0)
        return

    if data.startswith("amg_scope_browse:"):
        parts = data.split(":")
        if len(parts) != 4:
            await _edit(query, "⚠️ بيانات غير صالحة.", _home_keyboard())
            return
        target_id = _int_or_none(parts[1])
        folder_id = _int_or_none(parts[3])
        if target_id is None or folder_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await browse_scope_folders(query, target_id, parts[2], folder_id)
        return

    if data.startswith("amg_scope_res:"):
        parts = data.split(":")
        if len(parts) != 3:
            await _edit(query, "⚠️ بيانات غير صالحة.", _home_keyboard())
            return
        target_id = _int_or_none(parts[1])
        folder_id = _int_or_none(parts[2])
        if target_id is None or folder_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await pick_scope_resources(query, target_id, folder_id)
        return

    if data.startswith("amg_scope_set:"):
        parts = data.split(":")
        if len(parts) != 4:
            await _edit(query, "⚠️ بيانات غير صالحة.", _home_keyboard())
            return
        target_id = _int_or_none(parts[1])
        scope_id = _int_or_none(parts[3])
        if target_id is None or scope_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await set_admin_scope(query, target_id, parts[2], scope_id)
        return

    if data.startswith("amg_scope_view:"):
        parts = data.split(":")
        if len(parts) != 4:
            await _edit(query, "⚠️ بيانات غير صالحة.", _home_keyboard())
            return
        target_id = _int_or_none(parts[1])
        scope_id = _int_or_none(parts[3])
        if target_id is None or scope_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await revoke_admin_scope(query, target_id, parts[2], scope_id)
        return

    if data.startswith("amg_view:"):
        try:
            target_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await show_admin_detail(query, target_id)
        return

    if data.startswith("amg_preview:"):
        try:
            target_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await show_admin_preview(query, target_id)
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
            pattern=r"^(amg_list|amg_add|amg_roles|amg_perms_guide|amg_view:|amg_preview:|amg_perm:|amg_role:|amg_remove:|amg_transfer|amg_scopes:|amg_scope_)",
        )
    )
