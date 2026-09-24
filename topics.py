"""Search Topics (مواضيع البحث) for MEDBOT — user entry points + admin control.

A topic is a high-level academic area (Physiology, Anatomy, ...) that links to
one or more registered folders. It is deliberately not a mirror of every leaf
branch: the admin decides which major sections represent a topic.

Isolated like the other admin modules: it owns the `topics`/`topic_folders`
tables and its own handler set, and it never mutates folders or content except
to move an existing link. Read access is public (it is part of the student
navigation); the management surface is gated by `can_topics`.

State keys used (namespaced):
    topics_create      -> admin is typing a new topic name
    topics_link_id     -> admin is choosing a folder to link
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
import i18n
import workflow

logger = logging.getLogger(__name__)


def esc(value) -> str:
    import html

    return html.escape(str(value if value is not None else ""))


def btn(text, callback_data):
    return InlineKeyboardButton(text, callback_data=callback_data)


def _home_keyboard():
    return InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]])


def _resource_icon(node_type) -> str:
    return {
        "book": "📚", "books": "📚", "video": "🎥", "audio": "🎧",
        "mcq": "📝", "summary": "📑", "summaries": "📑",
        "image": "🖼", "photo": "🖼", "document": "📄", "doc": "📄",
    }.get(str(node_type or "").strip().lower(), "📁")


async def _is_manager(user_id) -> bool:
    try:
        return await database.user_has_permission(user_id, "can_topics")
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
            logger.warning("topics: edit_message_text failed")


# ---------------------------------------------------------------
# User side
# ---------------------------------------------------------------


async def _lang(user_id) -> str:
    try:
        return await database.get_user_language(user_id)
    except Exception:
        return i18n.DEFAULT_LANGUAGE


async def _topics_menu(prefix="topic_open", lang=None) -> InlineKeyboardMarkup:
    lang = lang or i18n.DEFAULT_LANGUAGE
    rows = [[btn(i18n.t("topics_open_resources", lang), "library:0")]]
    for topic in await database.get_topics(active_only=True):
        try:
            count = await database.topic_resource_count(topic["id"])
        except Exception:
            count = 0
        icon = topic.get("icon") or "🧭"
        rows.append(
            [btn(
                f"{icon} {str(topic['name'])[:30]} ({count})",
                f"{prefix}:{topic['id']}",
            )]
        )
    rows.append([btn(i18n.t("home", lang), "home")])
    return InlineKeyboardMarkup(rows)


async def show_topics(query):
    """Public topic list for quick access to registered resources.

    Deliberately distinct from the Resources/library browser: a topic is a
    curated high-level academic index, while the library shows the full
    registered hierarchy. The first row links to the library so both remain
    reachable from one screen.
    """
    try:
        topics = await database.get_topics(active_only=True)
    except Exception:
        logger.exception("topics: list failed")
        topics = []

    lang = await _lang(query.from_user.id)
    markup = await _topics_menu(lang=lang)

    if not topics:
        await _edit(
            query,
            i18n.t("topics_title", lang) + "\n\n" + i18n.t("topics_empty", lang),
            markup,
        )
        return

    await _edit(query, i18n.t("topics_title", lang), markup)


async def open_topic(query, topic_id):
    """Show the sections linked to a topic, then normal folder navigation."""
    lang = await _lang(query.from_user.id)

    try:
        topic = await database.get_topic(topic_id)
    except Exception:
        topic = None

    if not topic or not topic["active"]:
        await _edit(
            query,
            i18n.t("topic_unavailable", lang),
            InlineKeyboardMarkup(
                [
                    [btn(i18n.t("menu_topics", lang), "topics")],
                    [btn(i18n.t("home", lang), "home")],
                ]
            ),
        )
        return

    try:
        folders = await database.get_topic_folders(topic_id)
    except Exception:
        folders = []

    rows = []
    for item in folders:
        try:
            folder_id, name, node_type, _accepts = item[:4]
        except Exception:
            continue
        rows.append(
            [btn(f"{_resource_icon(node_type)} {str(name)[:35]}", f"folder:{folder_id}")]
        )

    if not rows:
        rows.append([btn(i18n.t("topic_no_sections", lang), "noop")])

    rows.append([btn(i18n.t("menu_topics", lang), "topics")])
    rows.append([btn(i18n.t("home", lang), "home")])

    body = (
        f"🧭 <b>{esc(topic['name'])}</b>\n\n"
        + (esc(topic["description"]) + "\n\n" if topic.get("description") else "")
        + i18n.t("topic_choose_section", lang)
    )

    await _edit(query, body, InlineKeyboardMarkup(rows))


# ---------------------------------------------------------------
# Admin side
# ---------------------------------------------------------------


def _admin_topics_menu(topics) -> InlineKeyboardMarkup:
    rows = []
    for topic in topics:
        mark = "✅" if topic["active"] else "⛔"
        rows.append(
            [btn(f"{mark} {str(topic['name'])[:28]}", f"topics_view:{topic['id']}")]
        )
    rows.append([btn("➕ إنشاء موضوع", "topics_create")])
    rows.append([btn("⬅️ إدارة المنصة", "admin")])
    rows.append([btn("🏠 الرئيسية", "home")])
    return InlineKeyboardMarkup(rows)


async def show_admin_topics(query, context=None):
    if not await _is_manager(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    if context is not None:
        context.user_data.pop("topics_create", None)
        context.user_data.pop("topics_link_id", None)

    try:
        topics = await database.get_topics()
    except Exception:
        topics = []

    await _edit(
        query,
        "🧭 <b>مواضيع البحث</b>\n\n"
        "المواضيع هي مداخل رئيسية لمجالات أكاديمية كبرى، وترتبط بأقسام "
        "مسجّلة. اختر موضوعاً لإدارته.",
        _admin_topics_menu(topics),
    )


async def show_topic_detail(query, topic_id):
    if not await _is_manager(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    try:
        topic = await database.get_topic(topic_id)
    except Exception:
        topic = None

    if not topic:
        await _edit(
            query,
            "⚠️ الموضوع غير موجود.",
            InlineKeyboardMarkup([[btn("⬅️ المواضيع", "admin_topics")]]),
        )
        return

    try:
        folders = await database.get_topic_folders(topic_id)
    except Exception:
        folders = []

    state = "✅ مفعّل" if topic["active"] else "⛔ معطّل"
    lines = [
        "🧭 <b>إدارة الموضوع</b>",
        f"🏷 الاسم: {esc(topic['name'])}",
        f"📊 الحالة: {state}",
        f"🔢 الترتيب: {esc(topic['display_order'])}",
    ]
    if topic.get("description"):
        lines.append(f"📝 {esc(topic['description'])}")

    lines.append("\n📂 <b>الأقسام المرتبطة</b>")
    rows = []
    if folders:
        for item in folders:
            try:
                folder_id, name, _ntype, _acc = item[:4]
            except Exception:
                continue
            lines.append(f"• {esc(name)}")
            rows.append(
                [btn(f"✂️ إزالة {str(name)[:22]}",
                     f"topics_unlink:{topic_id}:{folder_id}")]
            )
    else:
        lines.append("• لا توجد أقسام مرتبطة بعد.")

    rows.append([btn("🔗 ربط قسم", f"topics_link:{topic_id}")])
    toggle = "⛔ تعطيل" if topic["active"] else "✅ تفعيل"
    rows.append([btn(toggle, f"topics_toggle:{topic_id}")])
    rows.append([btn("🔽 تقديم", f"topics_order:{topic_id}:-1"),
                 btn("🔼 تأخير", f"topics_order:{topic_id}:1")])
    rows.append([btn("🗑 حذف الموضوع", f"topics_delete:{topic_id}")])
    rows.append([btn("⬅️ المواضيع", "admin_topics")])
    rows.append([btn("🏠 الرئيسية", "home")])

    await _edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def start_create_topic(query, context):
    if not await _is_manager(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    workflow.begin(context, "topics_create")
    context.user_data["topics_create"] = True

    await _edit(
        query,
        "➕ <b>إنشاء موضوع</b>\n\n"
        "أرسل اسم الموضوع (مثال: علم وظائف الأعضاء).\n\n"
        "يمكنك كتابة الوصف بعد اسم الموضوع في سطر منفصل.",
        InlineKeyboardMarkup(
            [[btn("❌ إلغاء", "admin_topics")], [btn("🏠 الرئيسية", "home")]]
        ),
    )


async def handle_topics_text(update, context) -> bool:
    """Consume typed topic name (create) or folder id (link). Returns handled."""
    creating = context.user_data.get("topics_create")
    linking = context.user_data.get("topics_link_id")

    if not creating and not linking:
        return False

    if not update.message or not update.message.text:
        return False

    # Another workflow may have claimed this input; only the active flow may
    # consume it (see workflow.py).
    if creating and not workflow.owns(context, "topics_create"):
        return False
    if linking and not workflow.owns(context, "topics_link"):
        return False

    if not await _is_manager(update.effective_user.id):
        context.user_data.pop("topics_create", None)
        context.user_data.pop("topics_link_id", None)
        await update.message.reply_text("🔒 غير مصرح.", reply_markup=_home_keyboard())
        return True

    text = update.message.text.strip()

    if text == "/cancel":
        context.user_data.pop("topics_create", None)
        context.user_data.pop("topics_link_id", None)
        await update.message.reply_text(
            "❌ تم إلغاء العملية.",
            reply_markup=InlineKeyboardMarkup(
                [[btn("🧭 المواضيع", "admin_topics")], [btn("🏠 الرئيسية", "home")]]
            ),
        )
        return True

    if creating:
        context.user_data.pop("topics_create", None)
        parts = [line.strip() for line in text.splitlines() if line.strip()]
        name = parts[0] if parts else ""
        description = " ".join(parts[1:]) if len(parts) > 1 else None

        new_id = await database.add_topic(name, description)

        if not new_id:
            await update.message.reply_text(
                "⚠️ اسم غير صالح.",
                reply_markup=InlineKeyboardMarkup(
                    [[btn("🧭 المواضيع", "admin_topics")], [btn("🏠 الرئيسية", "home")]]
                ),
            )
            return True

        await audit.log_action(
            update.effective_user.id, "topic_create",
            target_type="topic", target_id=new_id,
        )
        await update.message.reply_text(
            f"✅ تم إنشاء الموضوع: <b>{esc(name)}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [btn("🧭 إدارة الموضوع", f"topics_view:{new_id}")],
                    [btn("🧭 المواضيع", "admin_topics")],
                    [btn("🏠 الرئيسية", "home")],
                ]
            ),
        )
        return True

    # Linking a folder by id.
    try:
        folder_id = int("".join(ch for ch in text if ch.isdigit()))
    except (TypeError, ValueError):
        folder_id = 0

    topic_id = context.user_data.pop("topics_link_id")

    if folder_id <= 0:
        await update.message.reply_text(
            "⚠️ أرسل رقم القسم (ID) الصحيح.",
            reply_markup=InlineKeyboardMarkup(
                [[btn("🧭 إدارة الموضوع", f"topics_view:{topic_id}")],
                 [btn("🏠 الرئيسية", "home")]]
            ),
        )
        return True

    ok = await database.link_topic_folder(topic_id, folder_id)

    if ok:
        await audit.log_action(
            update.effective_user.id, "topic_link",
            target_type="topic", target_id=topic_id,
            details=f"folder={folder_id}",
        )

    await update.message.reply_text(
        "✅ تم ربط القسم بالموضوع." if ok else "⚠️ تعذر ربط القسم (تأكد من رقم القسم).",
        reply_markup=InlineKeyboardMarkup(
            [
                [btn("🧭 إدارة الموضوع", f"topics_view:{topic_id}")],
                [btn("🧭 المواضيع", "admin_topics")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )
    return True


async def _link_picker(query, context, topic_id):
    """Offer the current folder tree so the admin can pick a folder to link."""
    workflow.begin(context, "topics_link")
    context.user_data["topics_link_id"] = topic_id
    context.user_data.pop("topics_create", None)
    await _link_picker_level(query, context, topic_id, 0)


async def _link_picker_level(query, context, topic_id, parent_id):
    try:
        folders = await database.get_folders(parent_id)
    except Exception:
        folders = []

    rows = []
    try:
        breadcrumb = await database.get_breadcrumbs(parent_id) if parent_id else "الجذر"
    except Exception:
        breadcrumb = str(parent_id)

    for item in folders:
        try:
            folder_id, name, node_type, _acc = item[:4]
        except Exception:
            continue
        rows.append(
            [
                btn(f"{_resource_icon(node_type)} {str(name)[:18]}",
                    f"topics_pick_child:{topic_id}:{folder_id}"),
                btn("🔗 ربط", f"topics_pick:{topic_id}:{folder_id}"),
            ]
        )

    if parent_id:
        try:
            parent = await database.get_parent_id(parent_id)
        except Exception:
            parent = 0
        rows.append([btn("⬅️ رجوع", f"topics_pick_root:{topic_id}:{parent or 0}")])

    rows.append([btn("⬅️ إدارة الموضوع", f"topics_view:{topic_id}")])
    rows.append([btn("🏠 الرئيسية", "home")])

    await _edit(
        query,
        "🔗 <b>ربط قسم بالموضوع</b>\n\n"
        f"📍 {esc(breadcrumb)}\n\n"
        "تنقّل بين الأقسام ثم اضغط «🔗 ربط» بجانب القسم المطلوب. "
        "ربط قسم يضمّ أيضاً كل أقسامه الفرعية وموارده.",
        InlineKeyboardMarkup(rows),
    )


async def topics_callback_handler(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    try:
        await query.answer()
    except Exception:
        pass

    data = query.data or ""

    # ---- Public ----------------------------------------------------
    if data == "topics":
        await show_topics(query)
        return

    if data.startswith("topic_open:"):
        try:
            topic_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await open_topic(query, topic_id)
        return

    # ---- Admin: list / detail / mutations --------------------------
    if data == "admin_topics":
        await show_admin_topics(query, context)
        return

    if data.startswith("topics_view:"):
        try:
            topic_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await show_topic_detail(query, topic_id)
        return

    if data == "topics_create":
        await start_create_topic(query, context)
        return

    if data.startswith("topics_link:"):
        try:
            topic_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        if not await _is_manager(query.from_user.id):
            await _edit(query, "🔒 غير مصرح.", _home_keyboard())
            return
        await _link_picker(query, context, topic_id)
        return

    if data.startswith("topics_pick_child:"):
        parts = data.split(":")
        try:
            topic_id, folder_id = int(parts[1]), int(parts[2])
        except (IndexError, TypeError, ValueError):
            await _edit(query, "⚠️ طلب غير صالح.", _home_keyboard())
            return
        if not await _is_manager(query.from_user.id):
            await _edit(query, "🔒 غير مصرح.", _home_keyboard())
            return
        await _link_picker_level(query, context, topic_id, folder_id)
        return

    if data.startswith("topics_pick_root:"):
        parts = data.split(":")
        try:
            topic_id, folder_id = int(parts[1]), int(parts[2])
        except (IndexError, TypeError, ValueError):
            await _edit(query, "⚠️ طلب غير صالح.", _home_keyboard())
            return
        if not await _is_manager(query.from_user.id):
            await _edit(query, "🔒 غير مصرح.", _home_keyboard())
            return
        await _link_picker_level(query, context, topic_id, folder_id)
        return

    if data.startswith("topics_pick:"):
        parts = data.split(":")
        try:
            topic_id, folder_id = int(parts[1]), int(parts[2])
        except (IndexError, TypeError, ValueError):
            await _edit(query, "⚠️ طلب غير صالح.", _home_keyboard())
            return
        if not await _is_manager(query.from_user.id):
            await _edit(query, "🔒 غير مصرح.", _home_keyboard())
            return
        ok = await database.link_topic_folder(topic_id, folder_id)
        if ok:
            await audit.log_action(
                query.from_user.id, "topic_link",
                target_type="topic", target_id=topic_id,
                details=f"folder={folder_id}",
            )
        context.user_data.pop("topics_link_id", None)
        await show_topic_detail(query, topic_id)
        return

    if data.startswith("topics_unlink:"):
        parts = data.split(":")
        try:
            topic_id, folder_id = int(parts[1]), int(parts[2])
        except (IndexError, TypeError, ValueError):
            await _edit(query, "⚠️ طلب غير صالح.", _home_keyboard())
            return
        if not await _is_manager(query.from_user.id):
            await _edit(query, "🔒 غير مصرح.", _home_keyboard())
            return
        ok = await database.unlink_topic_folder(topic_id, folder_id)
        if ok:
            await audit.log_action(
                query.from_user.id, "topic_unlink",
                target_type="topic", target_id=topic_id,
                details=f"folder={folder_id}",
            )
        await show_topic_detail(query, topic_id)
        return

    if data.startswith("topics_toggle:"):
        try:
            topic_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        if not await _is_manager(query.from_user.id):
            await _edit(query, "🔒 غير مصرح.", _home_keyboard())
            return
        topic = await database.get_topic(topic_id)
        if topic:
            await database.update_topic(topic_id, active=not topic["active"])
            await audit.log_action(
                query.from_user.id, "topic_toggle",
                target_type="topic", target_id=topic_id,
                details=f"active={not topic['active']}",
            )
        await show_topic_detail(query, topic_id)
        return

    if data.startswith("topics_order:"):
        parts = data.split(":")
        try:
            topic_id, delta = int(parts[1]), int(parts[2])
        except (IndexError, TypeError, ValueError):
            await _edit(query, "⚠️ طلب غير صالح.", _home_keyboard())
            return
        if not await _is_manager(query.from_user.id):
            await _edit(query, "🔒 غير مصرح.", _home_keyboard())
            return
        topic = await database.get_topic(topic_id)
        if topic:
            new_order = max(0, int(topic["display_order"]) + delta)
            await database.update_topic(topic_id, display_order=new_order)
        await show_topic_detail(query, topic_id)
        return

    if data.startswith("topics_delete:"):
        try:
            topic_id = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        if not await _is_manager(query.from_user.id):
            await _edit(query, "🔒 غير مصرح.", _home_keyboard())
            return
        ok = await database.delete_topic(topic_id)
        if ok:
            await audit.log_action(
                query.from_user.id, "topic_delete",
                target_type="topic", target_id=topic_id,
            )
        await show_admin_topics(query, context)
        return

    await _edit(query, "⚠️ إجراء غير معروف.", _home_keyboard())


def register_topics_handlers(app):
    """Register the Search Topics surfaces (public + admin)."""
    app.add_handler(
        CallbackQueryHandler(
            topics_callback_handler,
            pattern=(
                r"^(topics|topic_open:|admin_topics|topics_view:|topics_create|"
                r"topics_link:|topics_pick:|topics_pick_child:|topics_pick_root:|"
                r"topics_unlink:|topics_toggle:|topics_order:|topics_delete:)"
            ),
        )
    )
