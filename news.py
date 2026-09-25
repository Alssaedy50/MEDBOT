"""News Center + News publishing (الأخبار) for MEDBOT.

Phase 1 of the News & Notifications system. It owns the *reading* surface
(📰 مركز الأخبار) and a minimal admin *publishing* surface, built directly on
the `news`, `news_reads` and `news_subscriptions` tables (migration v13).

Design rules held here:

* Three news kinds, all first-class: ``notify`` / ``section`` / ``resource``
  (see ``database.NEWS_TYPES``). They share one chronological feed instead of
  three parallel systems.
* The feed is deliberately the *only* public list, ordered newest-first with
  pagination. There is no archive tier in Phase 1: ``archived`` rows are the
  admin lifecycle (hidden), so the student feed stays simple and predictable.
* Real references only. A news row points at a live folder/content id; the
  detail screen resolves the *current* name and breadcrumb from the registry
  and offers a navigation button only when the referenced row still exists.
  Nothing about the platform hierarchy is hardcoded or copied into news.
* Read tracking is standalone (`news_reads`), so the unread badge on the home
  page is one cheap query and a duplicate read is impossible.

Future phases are anticipated by the data model and left unimplemented:
Phase 2 will turn the ``draft`` preview into a full create → preview → publish
→ archive flow, deliver published news privately to subscribers
(``news_subscriptions``), and let a registered resource generate its own news;
Phase 3 will scope an admin to a subject/folder and let that admin publish
only inside that scope. Nothing here has to be rebuilt for either.

Isolated like the other modules: its own handler set (``news_callback_handler``
/ ``handle_news_text``), registered *before* the catch-all router, and a
``can_news`` admin gate. State keys are namespaced ``news_*``.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, ContextTypes

import audit
import database
import i18n
import workflow

logger = logging.getLogger(__name__)

# Callback data prefixes owned by this module.
NEWS_CALLBACKS = (
    "news",
    "news_open:",
    "news_more:",
    "news_readall",
    "news_filter:",
    "admin_news",
    "news_admin_all",
    "news_admin_archived",
    "news_admin_view:",
    "news_admin_preview:",
    "news_admin_pub:",
    "news_admin_archive:",
    "news_admin_restore:",
    "news_admin_delete:",
    "news_new:",
)

# Admin publish-wizard state keys.
_STATE_TYPE = "news_new_type"
_STATE_TITLE = "news_new_title"
_STATE_BODY = "news_new_body"
_STATE_SECTION = "news_new_section"
_STATE_RESOURCE = "news_new_resource"

_ALL_STATE_KEYS = (
    _STATE_TYPE, _STATE_TITLE, _STATE_BODY, _STATE_SECTION, _STATE_RESOURCE,
)

# One workflow for the whole wizard: re-entering it is idempotent and
# `workflow.begin` only cancels *other* flows, so advancing from the title step
# to the body step never wipes the title it just captured.
NEWS_WORKFLOW = "news_draft"


def esc(value) -> str:
    import html

    return html.escape(str(value if value is not None else ""))


def btn(text, callback_data):
    return InlineKeyboardButton(text, callback_data=callback_data)


def _home_keyboard():
    return InlineKeyboardMarkup([[btn("🏠 الرئيسية", "home")]])


def _type_label(news_type) -> str:
    return database.NEWS_TYPE_LABELS.get(news_type, database.NEWS_TYPE_ICONS.get(news_type, "📰"))


def _resource_icon(node_type) -> str:
    return {
        "book": "📚", "books": "📚", "video": "🎥", "audio": "🎧",
        "mcq": "📝", "summary": "📑", "summaries": "📑",
        "image": "🖼", "photo": "🖼", "document": "📄", "doc": "📄",
    }.get(str(node_type or "").strip().lower(), "📁")


async def _lang(user_id) -> str:
    try:
        return await database.get_user_language(user_id)
    except Exception:
        return i18n.DEFAULT_LANGUAGE


async def _edit(query, text, markup=None):
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup
        )
    except Exception:
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            logger.warning("news: edit_message_text failed")


async def _is_manager(user_id) -> bool:
    try:
        return await database.user_has_permission(user_id, "can_news")
    except Exception:
        return False


# ---------------------------------------------------------------
# Student side — 📰 News Center
# ---------------------------------------------------------------


def _feed_line(news, is_read, lang) -> str:
    """One compact feed entry: type, title, section, time, read state."""
    mark = i18n.t("news_read", lang) if is_read else i18n.t("news_unread", lang)
    parts = [f"{database.NEWS_TYPE_ICONS.get(news['news_type'], '📰')} <b>{esc(news['title'])}</b>"]

    section = news.get("section_name") or news.get("subject_name")
    if section:
        parts.append(f"🗂 {esc(section)}")

    stamp = news.get("published_at") or news.get("created_at")
    if stamp:
        parts.append(f"🕒 {esc(stamp)}")

    parts.append(mark)
    return " · ".join(parts)


async def show_news_feed(query, page: int = 0, news_type: str = None):
    """The student News Center: chronological, paginated, read-aware.

    Only published news is listed (drafts/archived stay in the admin view).
    The unread state is resolved in one query for the page, so rendering the
    feed never does per-row work.
    """
    user_id = query.from_user.id
    lang = await _lang(user_id)
    page_size = database.NEWS_PAGE_SIZE

    try:
        page = max(0, int(page))
    except (TypeError, ValueError):
        page = 0

    if news_type not in database.NEWS_TYPES:
        news_type = None

    try:
        items = await database.list_news(
            status="published",
            news_type=news_type,
            limit=page_size,
            offset=page * page_size,
        )
        total = await database.count_news(status="published", news_type=news_type)
    except Exception:
        logger.exception("news: feed failed")
        items, total = [], 0

    # Resolve read state for this page only, in a single query.
    read_ids = set()
    if items:
        try:
            read_ids = await database.get_read_news_ids(
                user_id, [n["id"] for n in items]
            )
        except Exception:
            read_ids = set()

    # Resolve each row's real section/subject name with one shared connection.
    details = {}
    if items:
        try:
            db = await database.get_db()
            try:
                for item in items:
                    resolved = await database.get_news_detail(item["id"], db_conn=db)
                    if resolved:
                        details[item["id"]] = resolved
            finally:
                await db.close()
        except Exception:
            details = {}

    lines = [i18n.t("news_title", lang)]
    if not items:
        lines.append("")
        lines.append(i18n.t("news_empty", lang))
    else:
        shown_from = page * page_size + 1
        shown_to = page * page_size + len(items)
        lines.append(f"\n📄 {shown_from}–{shown_to} / {total}")

    rows = []
    for item in items:
        merged = details.get(item["id"]) or item
        is_read = item["id"] in read_ids
        rows.append(
            [
                btn(
                    ("✅ " if is_read else "🔵 ") + str(merged["title"])[:34],
                    f"news_open:{item['id']}",
                )
            ]
        )

    # Type filters keep the three kinds reachable without three systems.
    filter_row = [btn("📰 الكل", "news_filter:all")]
    for key in database.NEWS_TYPES:
        filter_row.append(
            btn(database.NEWS_TYPE_ICONS[key], f"news_filter:{key}")
        )
    rows.append(filter_row)

    if page + 1 < (total + page_size - 1) // max(page_size, 1):
        rows.append([btn(i18n.t("news_more", lang), f"news_more:{page + 1}")])

    if items:
        rows.append([btn(i18n.t("news_mark_all_read", lang), "news_readall")])

    rows.append([btn(i18n.t("home", lang), "home")])

    await _edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


def _detail_lines(news, lang) -> list:
    """The student-facing detail body for one resolved news row.

    Shared by the student reader and the admin preview so both always show
    the same real registry context (current names/breadcrumb, not a copy).
    """
    lines = [
        f"{database.NEWS_TYPE_ICONS.get(news['news_type'], '📰')} "
        f"<b>{esc(news['title'])}</b>",
        f"🏷 {_type_label(news['news_type'])}",
    ]

    if news.get("subject_name"):
        lines.append(f"🧪 المادة: {esc(news['subject_name'])}")
    if news.get("section_name"):
        lines.append(f"🗂 القسم: {esc(news['section_name'])}")
    if news.get("folder_path"):
        lines.append(f"📍 {esc(news['folder_path'])}")
    if news.get("doctor"):
        lines.append(f"👨‍⚕️ {esc(news['doctor'])}")
    if news.get("event_at"):
        lines.append(f"📅 {esc(news['event_at'])}")

    listed = news.get("published_at") or news.get("created_at")
    if listed:
        lines.append(f"🕒 {esc(listed)}")

    lines.append("")
    if news.get("body"):
        lines.append(esc(news["body"]))

    if news["news_type"] == "resource" and news.get("resource_present"):
        lines.append("")
        lines.append(f"📄 {esc(news.get('resource_title') or '')}")

    return lines


def _detail_access_rows(news, lang) -> list:
    """The real access button for a resolved news row (or none)."""
    rows = []
    if news["news_type"] == "resource" and news.get("resource_present"):
        rows.append(
            [btn(i18n.t("news_view_resource", lang), f"file:{news['resource_id']}")]
        )
    elif news.get("folder_id"):
        # A section notice, or a resource whose content row was removed but
        # whose registered section still exists: navigate to the real section.
        rows.append(
            [btn(i18n.t("news_open_section", lang), f"folder:{news['folder_id']}")]
        )
    return rows


async def open_news(query, context, news_id, admin: bool = False):
    """Open one news item, mark it read, and offer the right access action.

    A student may only open **published** news: `draft` and `archived` are
    admin lifecycle states, so a stale/invented callback id can never expose
    unpublished text. When the row is refused no read record is created. An
    authorised admin (`can_news`) may preview any state, but that goes through
    the admin surface (`show_admin_news_item`), never this student path.

    The detail view resolves the referenced folder/resource live, so the
    buttons shown always match the current registry: a resource news offers
    "📂 عرض المورد" only when the content row still exists, and a
    section/notification news offers "🗂 فتح القسم" only when its section does.
    """
    user_id = query.from_user.id
    lang = await _lang(user_id)

    try:
        news = await database.get_news_detail(news_id)
    except Exception:
        logger.exception("news: detail failed for %s", news_id)
        news = None

    # Only a published row is visible on the student path. A non-published row
    # is treated exactly like a missing one: no text, no read record.
    if news and not admin and news.get("status") != "published":
        news = None

    if not news:
        await _edit(
            query,
            i18n.t("news_no_match", lang),
            InlineKeyboardMarkup([[btn(i18n.t("news_back_feed", lang), "news")]]),
        )
        return

    # An unread news is marked read on open (idempotent). Never reached for a
    # refused draft/archived row. Admin previews never record a read.
    if not admin:
        try:
            await database.mark_news_read(user_id, news_id)
        except Exception:
            logger.debug("news: mark_news_read failed", exc_info=True)

    lines = _detail_lines(news, lang)
    rows = _detail_access_rows(news, lang)

    rows.append([btn(i18n.t("news_back_feed", lang), "news")])
    rows.append([btn(i18n.t("home", lang), "home")])

    await _edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def mark_all_read(query):
    """Mark every published news item read for the caller."""
    try:
        created = await database.mark_all_news_read(query.from_user.id)
        logger.info("news: %s marked %s item(s) read", query.from_user.id, created)
    except Exception:
        logger.exception("news: mark_all_read failed")
    await show_news_feed(query)


# ---------------------------------------------------------------
# Admin side — minimal Phase 1 publishing
# ---------------------------------------------------------------


def _admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn("🔴 إشعار هام", "news_new:notify")],
            [btn("🟡 خبر قسم", "news_new:section")],
            [btn("🟢 مورد جديد", "news_new:resource")],
            [btn("📋 كل الأخبار", "news_admin_all")],
            [btn("🗄 الأرشيف", "news_admin_archived")],
            [btn("⬅️ إدارة المنصة", "admin")],
            [btn("🏠 الرئيسية", "home")],
        ]
    )


def _status_filter_for_view(view: str):
    """Map an admin view to the `list_news` status filter.

    ``active`` is the default working set (everything except archived);
    ``archived`` reaches the archived rows so they can be restored; ``all``
    is the unfiltered list exposed through the explicit filter callbacks.
    """
    if view == "archived":
        return "archived", False
    if view == "all":
        return None, True
    return None, False


async def show_admin_news(query, context=None, view: str = "active"):
    """Admin news overview: type counts + a clickable list of rows.

    Every row is a `news_admin_view:<id>` button, so an admin can open any
    news item — including archived ones — and reach its publish/archive/
    restore/delete actions. `view` selects the working set:

    * ``active``   — draft + published (the default working set)
    * ``archived`` — archived rows only (so restore is reachable)
    * ``all``      — every lifecycle state, archived included
    """
    if not await _is_manager(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    if context is not None:
        _clear_state(context)

    status, include_archived = _status_filter_for_view(view)

    try:
        counts = await database.get_news_counts_by_type()
        rows_data = await database.list_news(
            status=status, include_archived=include_archived, limit=20
        )
        archived_count = await database.count_news(
            status="archived", include_archived=True
        )
    except Exception:
        logger.exception("news: admin overview failed")
        counts, rows_data, archived_count = {}, [], 0

    title = {
        "archived": "🗄 <b>الأخبار المؤرشفة</b>",
        "all": "📋 <b>كل الأخبار</b>",
    }.get(view, "📰 <b>إدارة الأخبار</b>")

    lines = [
        title,
        "",
        "الأنواع الثلاثة: إشعار هام، خبر قسم، مورد جديد.",
        "أنشئ الخبر كمسودة، راجعه، ثم انشره للطلاب.",
        "",
        f"🔴 {counts.get('notify', 0)} | 🟡 {counts.get('section', 0)} | "
        f"🟢 {counts.get('resource', 0)}  ·  🗄 {archived_count}",
        "",
    ]

    if not rows_data:
        lines.append("• لا توجد أخبار في هذا العرض.")
    else:
        for item in rows_data:
            state = database.NEWS_STATUS_LABELS.get(item["status"], item["status"])
            lines.append(
                f"• {database.NEWS_TYPE_ICONS.get(item['news_type'], '📰')} "
                f"{esc(str(item['title'])[:40])} — {esc(state)}"
            )
        lines.append("")
        lines.append("اضغط على أي خبر للفتح والتحكم.")

    keyboard = []
    for item in rows_data:
        state = database.NEWS_STATUS_LABELS.get(item["status"], "")
        keyboard.append(
            [
                btn(
                    f"{database.NEWS_TYPE_ICONS.get(item['news_type'], '📰')} "
                    f"{str(item['title'])[:26]} · {state}",
                    f"news_admin_view:{item['id']}",
                )
            ]
        )

    filter_row = [btn("📋 الكل", "news_admin_all")]
    if view == "archived":
        filter_row.append(btn("↩️ العودة للعرض العادي", "admin_news"))
    else:
        filter_row.append(btn("🗄 الأرشيف", "news_admin_archived"))
    keyboard.append(filter_row)

    keyboard.append([btn("⬅️ إدارة المنصة", "admin")])
    keyboard.append([btn("🏠 الرئيسية", "home")])

    await _edit(query, "\n".join(lines), InlineKeyboardMarkup(keyboard))


def _admin_item_menu(news, back: str = None) -> InlineKeyboardMarkup:
    """Actions for one news row, shaped by its lifecycle state.

    draft     -> Preview / Publish / Delete / Back
    published -> View    / Archive / Delete / Back
    archived  -> View    / Restore / Delete / Back

    ``View``/``Preview`` renders the student-facing detail (and its real
    resource/section access button) without marking it read for the admin.
    ``back`` defaults to the listing that owns the row so an archived item
    returns to the archive view and everything else to the working set.
    """
    if back is None:
        back = "news_admin_archived" if news["status"] == "archived" else "admin_news"

    rows = []
    if news["status"] == "draft":
        rows.append([btn("👁 معاينة", f"news_admin_preview:{news['id']}")])
        rows.append([btn("📢 نشر", f"news_admin_pub:{news['id']}")])
    elif news["status"] == "published":
        rows.append([btn("👁 عرض", f"news_admin_preview:{news['id']}")])
        rows.append([btn("🗄 أرشفة", f"news_admin_archive:{news['id']}")])
    elif news["status"] == "archived":
        rows.append([btn("👁 عرض", f"news_admin_preview:{news['id']}")])
        rows.append([btn("♻️ استرجاع كمسودة", f"news_admin_restore:{news['id']}")])

    rows.append([btn("🗑 حذف", f"news_admin_delete:{news['id']}")])
    rows.append([btn("⬅️ رجوع", back)])
    rows.append([btn("🏠 الرئيسية", "home")])
    return InlineKeyboardMarkup(rows)

async def show_admin_news_item(query, news_id, back: str = None):
    """Admin manage view of one news row: status + lifecycle actions."""
    if not await _is_manager(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    try:
        news = await database.get_news_detail(news_id)
    except Exception:
        news = None

    if not news:
        await _edit(
            query,
            "⚠️ الخبر غير موجود.",
            InlineKeyboardMarkup([[btn("⬅️ إدارة الأخبار", "admin_news")]]),
        )
        return

    status = database.NEWS_STATUS_LABELS.get(news["status"], news["status"])
    lang = await _lang(query.from_user.id)
    lines = _detail_lines(news, lang)
    lines.insert(2, f"📊 {esc(status)}")

    await _edit(query, "\n".join(lines), _admin_item_menu(news, back=back))


async def preview_admin_news(query, news_id, back: str = "admin_news"):
    """Render the student-facing view of a news row for an authorised admin.

    Read-only: nothing is marked read, and the lifecycle buttons stay
    reachable so the admin can act right after reviewing. Any state (draft /
    published / archived) is previewable here because the caller passed the
    `can_news` gate.
    """
    if not await _is_manager(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    try:
        news = await database.get_news_detail(news_id)
    except Exception:
        news = None

    if not news:
        await _edit(
            query,
            "⚠️ الخبر غير موجود.",
            InlineKeyboardMarkup([[btn("⬅️ إدارة الأخبار", "admin_news")]]),
        )
        return

    lang = await _lang(query.from_user.id)
    lines = ["🔎 <b>معاينة (كما يراها الطالب)</b>", ""]
    lines.extend(_detail_lines(news, lang))

    rows = _detail_access_rows(news, lang)
    rows.extend(_admin_item_menu(news, back=back).inline_keyboard)

    await _edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def start_create_news(query, context, news_type):
    """Begin the two-step draft wizard for one news kind.

    Phase 1 keeps it to title → body. The richer create → preview → publish →
    archive flow, folder/resource pickers and resource-generated news are
    Phase 2; the wizard already lands the row as a `draft` so nothing reaches
    students before an explicit publish.
    """
    if not await _is_manager(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    if news_type not in database.NEWS_TYPES:
        await _edit(query, "⚠️ نوع غير معروف.", _admin_menu())
        return

    workflow.begin(context, NEWS_WORKFLOW)
    context.user_data[_STATE_TYPE] = news_type
    context.user_data.pop(_STATE_TITLE, None)
    context.user_data.pop(_STATE_BODY, None)

    label = database.NEWS_TYPE_LABELS.get(news_type, news_type)
    hint = {
        "notify": "مثال: محاضرة اليوم — 10:00 بقاعة 3.",
        "section": "اكتب عنوان خبر القسم، ثم في سطر جديد القسم/المادة.",
        "resource": "اكتب عنوان الخبر، ويمكنك ذكر رقم المورد من قسم الموارد.",
    }.get(news_type, "")

    await _edit(
        query,
        f"{database.NEWS_TYPE_ICONS.get(news_type, '📰')} <b>خبر جديد — {esc(label)}</b>\n\n"
        "أرسل عنوان الخبر في رسالة واحدة.\n"
        + (f"\n{hint}\n" if hint else "")
        + "\nلإلغاء العملية أرسل /cancel.",
        InlineKeyboardMarkup(
            [[btn("❌ إلغاء", "admin_news")], [btn("🏠 الرئيسية", "home")]]
        ),
    )


async def handle_news_text(update, context) -> bool:
    """Consume typed input for the admin news wizard. Returns handled."""
    waiting_body = context.user_data.get(_STATE_BODY) is True
    waiting_title = (
        context.user_data.get(_STATE_TYPE) is not None and not waiting_body
    )

    if not waiting_title and not waiting_body:
        return False

    if not update.message or not update.message.text:
        return False

    if waiting_title and not workflow.owns(context, NEWS_WORKFLOW):
        return False
    if waiting_body and not workflow.owns(context, NEWS_WORKFLOW):
        return False

    if not await _is_manager(update.effective_user.id):
        _clear_state(context)
        await update.message.reply_text("🔒 غير مصرح.", reply_markup=_home_keyboard())
        return True

    text = update.message.text.strip()

    if text == "/cancel":
        _clear_state(context)
        await update.message.reply_text(
            "❌ تم إلغاء العملية.",
            reply_markup=InlineKeyboardMarkup(
                [[btn("📰 إدارة الأخبار", "admin_news")], [btn("🏠 الرئيسية", "home")]]
            ),
        )
        return True

    news_type = context.user_data.get(_STATE_TYPE)

    # ---- Step 1: title -------------------------------------------------
    if waiting_title:
        context.user_data[_STATE_TITLE] = text
        workflow.begin(context, NEWS_WORKFLOW)
        context.user_data[_STATE_BODY] = True

        await update.message.reply_text(
            "📝 أرسل الآن نص الخبر كما سيظهر للطالب، "
            "أو أرسل /skip لتجاهله.\n\nلإلغاء العملية أرسل /cancel.",
            reply_markup=InlineKeyboardMarkup(
                [[btn("❌ إلغاء", "admin_news")], [btn("🏠 الرئيسية", "home")]]
            ),
        )
        return True

    # ---- Step 2: body -> create draft ---------------------------------
    body = None if text == "/skip" else text
    title = context.user_data.pop(_STATE_TITLE, None)
    context.user_data.pop(_STATE_BODY, None)
    context.user_data.pop(_STATE_TYPE, None)

    if not title:
        await update.message.reply_text(
            "⚠️ تعذّر إنشاء الخبر (عنوان مفقود).",
            reply_markup=InlineKeyboardMarkup(
                [[btn("📰 إدارة الأخبار", "admin_news")], [btn("🏠 الرئيسية", "home")]]
            ),
        )
        return True

    news_id = await database.create_news(
        news_type=news_type,
        title=title,
        body=body,
        sender_id=update.effective_user.id,
        status="draft",
    )

    if not news_id:
        await update.message.reply_text(
            "⚠️ تعذّر إنشاء الخبر. تحقق من العنوان والنص.",
            reply_markup=InlineKeyboardMarkup(
                [[btn("📰 إدارة الأخبار", "admin_news")], [btn("🏠 الرئيسية", "home")]]
            ),
        )
        return True

    await update.message.reply_text(
        f"✅ تم إنشاء مسودة: <b>{esc(title)}</b>\n\n"
        "راجعها ثم اضغط 📢 نشر لإظهارها للطلاب.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [btn("🔎 مراجعة الخبر", f"news_admin_view:{news_id}")],
                [btn("📰 إدارة الأخبار", "admin_news")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )
    return True


def _clear_state(context):
    for key in _ALL_STATE_KEYS:
        context.user_data.pop(key, None)


async def _publish(query, news_id):
    ok = await database.publish_news(news_id)
    if ok:
        await audit.log_action(
            query.from_user.id, "news_publish",
            target_type="news", target_id=news_id,
        )
    await show_admin_news_item(query, news_id)


async def _archive(query, news_id):
    ok = await database.archive_news(news_id)
    if ok:
        await audit.log_action(
            query.from_user.id, "news_archive",
            target_type="news", target_id=news_id,
        )
    await show_admin_news_item(query, news_id)


async def _restore(query, news_id):
    ok = await database.restore_news(news_id)
    if ok:
        await audit.log_action(
            query.from_user.id, "news_restore",
            target_type="news", target_id=news_id,
        )
    # A restored row leaves the archive, so land back on the working set.
    await show_admin_news_item(query, news_id)


async def _delete(query, news_id):
    ok = await database.delete_news(news_id)
    if ok:
        await audit.log_action(
            query.from_user.id, "news_delete",
            target_type="news", target_id=news_id,
        )
    await show_admin_news(query)


# ---------------------------------------------------------------
# Callback dispatch
# ---------------------------------------------------------------


def _int_or_none(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


async def _news_hidden_for(query) -> bool:
    """True when the News Center is hidden for this caller (admins bypass)."""
    try:
        if await database.is_user_admin(query.from_user.id):
            return False
        if not await database.is_feature_hidden("news"):
            return False
    except Exception:
        return False

    await _edit(
        query,
        "🛠 هذا القسم غير متاح مؤقتاً للصيانة أو التحديث.",
        _home_keyboard(),
    )
    return True


async def news_callback_handler(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    try:
        await query.answer()
    except Exception:
        pass

    data = query.data or ""

    # ---- Public (student) ------------------------------------------
    if data == "news":
        if await _news_hidden_for(query):
            return
        await show_news_feed(query)
        return

    if data.startswith("news_open:"):
        if await _news_hidden_for(query):
            return
        news_id = _int_or_none(data.split(":", 1)[1])
        if news_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await open_news(query, context, news_id)
        return

    if data.startswith("news_more:"):
        if await _news_hidden_for(query):
            return
        page = _int_or_none(data.split(":", 1)[1]) or 0
        await show_news_feed(query, page=page)
        return

    if data.startswith("news_filter:"):
        if await _news_hidden_for(query):
            return
        news_type = data.split(":", 1)[1]
        await show_news_feed(
            query, news_type=news_type if news_type != "all" else None
        )
        return

    if data == "news_readall":
        if await _news_hidden_for(query):
            return
        await mark_all_read(query)
        return

    # ---- Admin -----------------------------------------------------
    if data == "admin_news":
        await show_admin_news(query, context)
        return

    if data == "news_admin_all":
        await show_admin_news(query, context, view="all")
        return

    if data == "news_admin_archived":
        await show_admin_news(query, context, view="archived")
        return

    if data.startswith("news_new:"):
        await start_create_news(query, context, data.split(":", 1)[1])
        return

    if data.startswith("news_admin_view:"):
        news_id = _int_or_none(data.split(":", 1)[1])
        if news_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        if not await _is_manager(query.from_user.id):
            await _edit(query, "🔒 غير مصرح.", _home_keyboard())
            return
        await show_admin_news_item(query, news_id)
        return

    if data.startswith("news_admin_preview:"):
        news_id = _int_or_none(data.split(":", 1)[1])
        if news_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        if not await _is_manager(query.from_user.id):
            await _edit(query, "🔒 غير مصرح.", _home_keyboard())
            return
        await preview_admin_news(query, news_id)
        return

    if data.startswith("news_admin_pub:"):
        news_id = _int_or_none(data.split(":", 1)[1])
        if news_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        if not await _is_manager(query.from_user.id):
            await _edit(query, "🔒 غير مصرح.", _home_keyboard())
            return
        await _publish(query, news_id)
        return

    if data.startswith("news_admin_archive:"):
        news_id = _int_or_none(data.split(":", 1)[1])
        if news_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        if not await _is_manager(query.from_user.id):
            await _edit(query, "🔒 غير مصرح.", _home_keyboard())
            return
        await _archive(query, news_id)
        return

    if data.startswith("news_admin_restore:"):
        news_id = _int_or_none(data.split(":", 1)[1])
        if news_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        if not await _is_manager(query.from_user.id):
            await _edit(query, "🔒 غير مصرح.", _home_keyboard())
            return
        await _restore(query, news_id)
        return

    if data.startswith("news_admin_delete:"):
        news_id = _int_or_none(data.split(":", 1)[1])
        if news_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        if not await _is_manager(query.from_user.id):
            await _edit(query, "🔒 غير مصرح.", _home_keyboard())
            return
        await _delete(query, news_id)
        return

    await _edit(query, "⚠️ إجراء غير معروف.", _home_keyboard())


def register_news_handlers(app):
    """Register the News Center + admin publishing surfaces."""
    app.add_handler(
        CallbackQueryHandler(
            news_callback_handler,
            pattern=r"^(" + "|".join(NEWS_CALLBACKS) + ")",
        )
    )
