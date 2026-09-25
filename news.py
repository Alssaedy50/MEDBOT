"""News Center + News publishing and subscriptions (الأخبار) for MEDBOT.

Phase 1 shipped the *reading* surface (📰 مركز الأخبار) and the News Core on
the `news` / `news_reads` / `news_subscriptions` tables (migration v13).
Phase 2 turns that Core into a full system, still built on the same tables:

* 📝 مركز النشر — a unified admin publishing centre for the three kinds
  (🔴 important / 🟡 section / 🟢 resource), each with a real reference picked
  from the live registry (a folder for a section, a content row for a
  resource). The lifecycle is create → preview → publish → archive, and a
  published item is privately delivered to its subscribers.
* ⚙️ اشتراكات الأخبار — a student chooses which kinds/sections reach them
  privately. This controls *private delivery only*: the News Center still
  lists every published item to everyone (see `show_news_feed`), and a
  delivered item stays unread until the student opens it.
* 🟢 resource-generated news — registering a resource can produce its news
  item automatically (best-effort, idempotent, never blocking the upload).

Design rules held here:

* Three news kinds, all first-class: ``notify`` / ``section`` / ``resource``
  (see ``database.NEWS_TYPES``). They share one chronological feed instead of
  three parallel systems.
* The feed is deliberately the *only* public list, ordered newest-first with
  pagination. ``archived`` rows are the admin lifecycle (hidden), so the
  student feed stays simple and predictable.
* Real references only. A news row points at a live folder/content id; the
  detail screen resolves the *current* name and breadcrumb from the registry
  and offers a navigation button only when the referenced row still exists.
  Nothing about the platform hierarchy is hardcoded or copied into news.
* Read tracking is standalone (`news_reads`), so the unread badge on the home
  page is one cheap query and a duplicate read is impossible.

Phase 3 (scoped admin / RBAC over a subject-folder) is anticipated by the data
model and left unimplemented; nothing here has to be rebuilt for it.

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
import news_delivery
import workflow

logger = logging.getLogger(__name__)

# Callback data prefixes owned by this module.
NEWS_CALLBACKS = (
    "news",
    "news_open:",
    "news_more:",
    "news_readall",
    "news_filter:",
    "news_subs",
    "news_sub:",
    "news_unsub:",
    "news_subs_sections",
    "news_subs_section:",
    "news_subs_back",
    "admin_news",
    "news_admin_all",
    "news_admin_archived",
    "news_admin_view:",
    "news_admin_preview:",
    "news_admin_pub:",
    "news_admin_archive:",
    "news_admin_restore:",
    "news_admin_delete:",
    "news_admin_deliveries:",
    "news_admin_retry:",
    "news_new:",
    "news_pick_child:",
    "news_pick_root:",
    "news_ref_child:",
    "news_ref_root:",
    "news_ref_set_section:",
    "news_ref_resource:",
    "news_ref_set_resource:",
)

# Admin publish-wizard state keys.
_STATE_TYPE = "news_new_type"
_STATE_STEP = "news_new_step"
_STATE_TITLE = "news_new_title"
_STATE_BODY = "news_new_body"
_STATE_DOCTOR = "news_new_doctor"
_STATE_EVENT = "news_new_event"
_STATE_SECTION = "news_new_section"
_STATE_RESOURCE = "news_new_resource"

_ALL_STATE_KEYS = (
    _STATE_TYPE, _STATE_STEP, _STATE_TITLE, _STATE_BODY, _STATE_DOCTOR,
    _STATE_EVENT, _STATE_SECTION, _STATE_RESOURCE,
)

# One workflow for the whole wizard: re-entering it is idempotent and
# `workflow.begin` only cancels *other* flows, so advancing from the title step
# to the body step never wipes the title it just captured.
NEWS_WORKFLOW = "news_draft"
NEWS_SECTION_WORKFLOW = "news_section_pick"


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

    rows.append([btn(i18n.t("news_subs", lang), "news_subs")])

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
# Student side — ⚙️ News subscriptions
# ---------------------------------------------------------------
# Subscriptions decide *private delivery only*: nothing here changes what the
# News Center lists. A student subscribes to a whole kind or to one real
# section (a folder), and only then do matching items reach a Telegram inbox.


async def _subscription_state(user_id):
    """Return the caller's subscription set plus the three kind toggles.

    Reads the persisted rows once (no per-button queries) and derives the
    per-kind boolean, so rendering the screen is O(1) queries.
    """
    subs = set()
    try:
        subs = set(await database.get_news_subscriptions(user_id))
    except Exception:
        logger.exception("news: could not load subscriptions")
    type_subs = {value for kind, value in subs if kind == database.NEWS_SUB_TYPE}
    section_ids = {
        value for kind, value in subs if kind == database.NEWS_SUB_SECTION
    }
    return subs, type_subs, section_ids


async def show_subscriptions(query):
    """The ⚙️ subscriptions screen: three kinds + a sections entry."""
    user_id = query.from_user.id
    lang = await _lang(user_id)
    _subs, type_subs, section_ids = await _subscription_state(user_id)

    rows = []
    for news_type in database.NEWS_TYPES:
        icon = database.NEWS_TYPE_ICONS.get(news_type, "📰")
        label = database.NEWS_TYPE_LABELS.get(news_type, news_type)
        on = news_type in type_subs
        rows.append(
            [
                btn(
                    f"{icon} {label}",
                    f"news_sub:{news_type}",
                ),
                btn("✅ مشترك" if on else "🔔 اشترك", f"news_sub:{news_type}"),
            ]
        )

    if section_ids:
        sections_label = f"🗂 أقسام محددة ({len(section_ids)})"
    else:
        sections_label = "🗂 أقسام محددة"
    rows.append([btn(sections_label, "news_subs_sections")])

    rows.append([btn(i18n.t("news_back_feed", lang), "news")])
    rows.append([btn(i18n.t("home", lang), "home")])

    await _edit(
        query,
        "⚙️ <b>اشتراكات الأخبار</b>\n\n"
        "اختر ما يصلك كرسالة خاصة. مركز الأخبار يعرض كل الأخبار المنشورة "
        "للجميع، والاشتراك يتحكم فقط في التوصيل الخاص.",
        InlineKeyboardMarkup(rows),
    )


async def toggle_subscription(query, news_type):
    """Subscribe/unsubscribe the caller to a whole news kind (idempotent)."""
    user_id = query.from_user.id
    if news_type not in database.NEWS_TYPES:
        await _edit(query, "⚠️ نوع غير معروف.", _home_keyboard())
        return

    _subs, type_subs, _sections = await _subscription_state(user_id)
    try:
        if news_type in type_subs:
            await database.remove_news_subscription(
                user_id, database.NEWS_SUB_TYPE, news_type
            )
        else:
            await database.add_news_subscription(
                user_id, database.NEWS_SUB_TYPE, news_type
            )
    except Exception:
        logger.exception("news: toggle_subscription failed")

    await show_subscriptions(query)


async def show_section_subscriptions(query, parent_id: int = 0):
    """Browse the real folder hierarchy and subscribe to one section.

    The tree is the live registry (`database.get_folders`), so the student can
    only pick a section that actually exists. ``parent_id=0`` is the root and
    a section subscription is per exact folder — no synthetic taxonomy.
    """
    user_id = query.from_user.id
    lang = await _lang(user_id)
    _subs, _type_subs, section_ids = await _subscription_state(user_id)

    try:
        folders = await database.get_folders(parent_id)
    except Exception:
        logger.exception("news: could not load folders")
        folders = []

    try:
        breadcrumb = (
            await database.get_breadcrumbs(parent_id) if parent_id else "الرئيسية 🏠"
        )
    except Exception:
        breadcrumb = str(parent_id)

    rows = []
    for item in folders:
        try:
            folder_id, name, node_type = item[0], item[1], item[2]
        except Exception:
            continue
        icon = _resource_icon(node_type)
        subscribed = str(folder_id) in section_ids
        marker = "✅ " if subscribed else ""
        rows.append(
            [
                btn(
                    f"{marker}{icon} {str(name)[:20]}",
                    f"news_subs_section:{folder_id}",
                ),
                btn(
                    "🔔 اشترك" if not subscribed else "🔕 إلغاء",
                    f"news_subs_section:{folder_id}",
                ),
            ]
        )
        rows.append(
            [btn(f"↳ دخول {str(name)[:18]}", f"news_pick_child:{folder_id}")]
        )

    if parent_id:
        try:
            parent = await database.get_parent_id(parent_id)
        except Exception:
            parent = 0
        rows.append([btn("⬅️ رجوع", f"news_pick_root:{parent or 0}")])

    rows.append([btn("⚙️ الاشتراكات", "news_subs")])
    rows.append([btn(i18n.t("home", lang), "home")])

    await _edit(
        query,
        "🗂 <b>اشتراكات الأقسام</b>\n\n"
        f"📍 {esc(breadcrumb)}\n\n"
        "اضغط «🔔 اشترك» بجانب القسم الذي يهمك، أو «↳ دخول» للتنقل داخله.",
        InlineKeyboardMarkup(rows),
    )


async def toggle_section_subscription(query, folder_id):
    """Subscribe/unsubscribe the caller to one real folder section."""
    user_id = query.from_user.id
    folder_id = _int_or_none(folder_id)
    if folder_id is None:
        await _edit(query, "⚠️ قسم غير صالح.", _home_keyboard())
        return

    try:
        folder = await database.get_folder(folder_id)
    except Exception:
        folder = None
    if not folder:
        await _edit(query, "⚠️ القسم غير موجود.", _home_keyboard())
        return

    _subs, _type_subs, section_ids = await _subscription_state(user_id)
    try:
        if str(folder_id) in section_ids:
            await database.remove_news_subscription(
                user_id, database.NEWS_SUB_SECTION, str(folder_id)
            )
        else:
            await database.add_news_subscription(
                user_id, database.NEWS_SUB_SECTION, str(folder_id)
            )
    except Exception:
        logger.exception("news: toggle_section_subscription failed")

    # Return to the same tree level so repeated toggles stay in place.
    parent_id = folder[1] if folder[1] is not None else 0
    await show_section_subscriptions(query, parent_id=parent_id)


# ---------------------------------------------------------------
# Admin side — 📝 Publishing Center
# ---------------------------------------------------------------


def _admin_menu() -> InlineKeyboardMarkup:
    """The 📝 النشر entry menu: one row per kind + the two listings."""
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


async def _list_all_content() -> list:
    """Every content row as (id, folder_id, title), newest first (bounded).

    One query, so the resource picker's root listing never N+1s. The picker
    only ever selects an id that came from here (the real registry).
    """
    try:
        db = await database.get_db()
        try:
            async with db.execute(
                "SELECT id, folder_id, title FROM content ORDER BY id DESC LIMIT 200"
            ) as cur:
                return [tuple(row) for row in await cur.fetchall()]
        finally:
            await db.close()
    except Exception:
        logger.exception("news: could not list content")
        return []


def _reference_missing_for(news) -> str:
    """Return the missing required reference for a row, or "".

    A section news must point at a real folder and a resource news at a real
    content row before it can be published; the reference is validated again
    when it is set, and the publish refuses while it is absent.
    """
    if news.get("news_type") == "section" and not news.get("section_folder_id"):
        return "section"
    if news.get("news_type") == "resource" and not news.get("resource_id"):
        return "resource"
    return ""


# ---------------------------------------------------------------
# Admin side — reference pickers (real registry only)
# ---------------------------------------------------------------


async def pick_section(query, news_id, parent_id: int = 0):
    """Browse the live folder tree to set a section news' real reference."""
    if not await _is_manager(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    try:
        folders = await database.get_folders(parent_id)
    except Exception:
        folders = []
    try:
        breadcrumb = (
            await database.get_breadcrumbs(parent_id) if parent_id else "الرئيسية 🏠"
        )
    except Exception:
        breadcrumb = str(parent_id)

    rows = []
    for item in folders:
        try:
            folder_id, name, node_type = item[0], item[1], item[2]
        except Exception:
            continue
        rows.append(
            [
                btn(
                    f"{_resource_icon(node_type)} {str(name)[:18]}",
                    f"news_ref_child:{news_id}:{folder_id}",
                ),
                btn("✅ اختيار", f"news_ref_set_section:{news_id}:{folder_id}"),
            ]
        )

    if parent_id:
        try:
            parent = await database.get_parent_id(parent_id)
        except Exception:
            parent = 0
        rows.append([btn("⬅️ رجوع", f"news_ref_root:{news_id}:{parent or 0}")])

    rows.append([btn("⬅️ الخبر", f"news_admin_view:{news_id}")])
    rows.append([btn("🏠 الرئيسية", "home")])

    await _edit(
        query,
        "🗂 <b>اختيار القسم</b>\n\n"
        f"📍 {esc(breadcrumb)}\n\n"
        "تنقّل ثم اضغط «✅ اختيار» بجانب القسم الحقيقي للخبر.",
        InlineKeyboardMarkup(rows),
    )


async def pick_resource(query, news_id, folder_id: int = None):
    """Browse the live registry to set a resource news' real reference."""
    if not await _is_manager(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    rows = []

    if folder_id is None:
        # Root listing: recent resources plus the entry folders to browse.
        files = await _list_all_content()
        for content_id, _folder, title in files[:25]:
            rows.append(
                [
                    btn(f"📄 {str(title)[:24]}", f"news_ref_set_resource:{news_id}:{content_id}"),
                ]
            )
        try:
            folders = await database.get_folders(0)
        except Exception:
            folders = []
        for item in folders:
            try:
                fid, name, node_type = item[0], item[1], item[2]
            except Exception:
                continue
            rows.append(
                [btn(f"📁 {_resource_icon(node_type)} {str(name)[:20]}",
                     f"news_ref_resource:{news_id}:{fid}")]
            )
        breadcrumb = "الرئيسية 🏠"
    else:
        try:
            files = await database.get_files(folder_id)
        except Exception:
            files = []
        for row in files[:25]:
            try:
                content_id, title = row[0], row[1]
            except Exception:
                continue
            rows.append(
                [btn(f"📄 {str(title)[:24]}", f"news_ref_set_resource:{news_id}:{content_id}")]
            )
        try:
            folders = await database.get_folders(folder_id)
        except Exception:
            folders = []
        for item in folders:
            try:
                fid, name, node_type = item[0], item[1], item[2]
            except Exception:
                continue
            rows.append(
                [btn(f"📁 {_resource_icon(node_type)} {str(name)[:20]}",
                     f"news_ref_resource:{news_id}:{fid}")]
            )
        try:
            breadcrumb = await database.get_breadcrumbs(folder_id)
        except Exception:
            breadcrumb = str(folder_id)
        try:
            parent = await database.get_parent_id(folder_id)
        except Exception:
            parent = 0
        rows.append([btn("⬅️ رجوع", f"news_ref_resource:{news_id}")] if not parent
                    else [btn("⬅️ رجوع", f"news_ref_resource:{news_id}:{parent}")])

    rows.append([btn("⬅️ الخبر", f"news_admin_view:{news_id}")])
    rows.append([btn("🏠 الرئيسية", "home")])

    await _edit(
        query,
        "🟢 <b>اختيار المورد</b>\n\n"
        f"📍 {esc(breadcrumb)}\n\n"
        "اختر موردًا موجودًا فعليًا من القائمة.",
        InlineKeyboardMarkup(rows),
    )


async def set_section_reference(query, news_id, folder_id):
    """Point a section news at a real folder (validated server-side)."""
    if not await _is_manager(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    try:
        folder = await database.get_folder(_int_or_none(folder_id))
    except Exception:
        folder = None
    if not folder:
        await _edit(query, "⚠️ القسم غير موجود.", _home_keyboard())
        return

    try:
        ok = await database.update_news(
            news_id, section_folder_id=folder[0], subject_folder_id=folder[1]
        )
    except Exception:
        ok = False

    if ok:
        await audit.log_action(
            query.from_user.id, "news_reference",
            target_type="news", target_id=news_id, details=f"section={folder[0]}",
        )
    await show_admin_news_item(query, news_id)


async def set_resource_reference(query, news_id, content_id):
    """Point a resource news at a real content row (validated server-side)."""
    if not await _is_manager(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    try:
        record = await database.get_file_record(_int_or_none(content_id))
    except Exception:
        record = None
    if not record:
        await _edit(query, "⚠️ المورد غير موجود.", _home_keyboard())
        return

    try:
        ok = await database.update_news(news_id, resource_id=record[0])
    except Exception:
        ok = False

    if ok:
        await audit.log_action(
            query.from_user.id, "news_reference",
            target_type="news", target_id=news_id, details=f"resource={record[0]}",
        )
    await show_admin_news_item(query, news_id)


async def show_news_deliveries(query, news_id):
    """Admin view of one item's private-delivery log (counts + recent rows)."""
    if not await _is_manager(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    try:
        counts = await database.get_news_delivery_counts(news_id)
        rows_data = await database.get_news_deliveries(news_id, limit=30)
    except Exception:
        counts, rows_data = {}, []

    lines = [
        "📬 <b>سجل التوصيل</b>",
        "",
        f"✅ منشور: {counts.get('sent', 0)}  ·  ⏳ معلّق: {counts.get('pending', 0)}"
        f"  ·  ⚠️ فشل: {counts.get('failed', 0)}",
    ]
    if rows_data:
        lines.append("")
        for row in rows_data:
            lines.append(
                f"• <code>{row['user_id']}</code> — "
                f"{esc(row['status'])} ({row['attempts']})"
            )

    rows = []
    if counts.get("failed") or counts.get("pending"):
        rows.append([btn("🔁 إعادة المحاولة", f"news_admin_retry:{news_id}")])
    rows.append([btn("⬅️ الخبر", f"news_admin_view:{news_id}")])
    rows.append([btn("🏠 الرئيسية", "home")])

    await _edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def retry_deliveries(query, news_id):
    """Retry the pending/failed deliveries of one item (never resent)."""
    if not await _is_manager(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    try:
        result = await news_delivery.retry_failed(query.get_bot(), news_id)
        if result.get("sent") or result.get("failed"):
            await audit.log_action(
                query.from_user.id, "news_delivery_retry",
                target_type="news", target_id=news_id,
                details=f"sent={result.get('sent')}, failed={result.get('failed')}",
            )
    except Exception:
        logger.exception("news: retry_deliveries failed")

    await show_news_deliveries(query, news_id)


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
    }.get(view, "📝 <b>مركز النشر</b>")

    lines = [
        title,
        "",
        "أنشئ الخبر كمسودة، راجعه، ثم انشره — ويُوصَل المشتركون تلقائيًا.",
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
        # A section/resource news needs a real reference before it can publish.
        if news.get("news_type") == "section" and not news.get("section_folder_id"):
            rows.append([btn("🗂 اختيار القسم", f"news_ref_root:{news['id']}:0")])
        elif news.get("news_type") == "resource" and not news.get("resource_id"):
            rows.append([btn("🟢 اختيار المورد", f"news_ref_resource:{news['id']}")])
        else:
            rows.append([btn("📢 نشر", f"news_admin_pub:{news['id']}")])
        rows.append([btn("👁 معاينة", f"news_admin_preview:{news['id']}")])
    elif news["status"] == "published":
        rows.append([btn("👁 عرض", f"news_admin_preview:{news['id']}")])
        rows.append([btn("🗄 أرشفة", f"news_admin_archive:{news['id']}")])
        rows.append([btn("📬 سجل التوصيل", f"news_admin_deliveries:{news['id']}")])
    elif news["status"] == "archived":
        rows.append([btn("👁 عرض", f"news_admin_preview:{news['id']}")])
        rows.append([btn("♻️ استرجاع كمسودة", f"news_admin_restore:{news['id']}")])
        rows.append([btn("📬 سجل التوصيل", f"news_admin_deliveries:{news['id']}")])

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
    """Begin the draft wizard for one news kind.

    Lands the row as a ``draft`` — nothing reaches students before an explicit
    publish. The typed steps differ by kind:

    * notify   — title → body → doctor (optional) → event time (optional)
    * section  — title → body → doctor → event, then pick a real section
    * resource — title → body, then pick a real resource

    Section/resource references are *not* typed: the wizard routes to the real
    registry pickers after the text steps, so an invalid id can never be keyed
    in.
    """
    if not await _is_manager(query.from_user.id):
        await _edit(query, "🔒 غير مصرح.", _home_keyboard())
        return

    if news_type not in database.NEWS_TYPES:
        await _edit(query, "⚠️ نوع غير معروف.", _admin_menu())
        return

    workflow.begin(context, NEWS_WORKFLOW)
    context.user_data[_STATE_TYPE] = news_type
    for key in (_STATE_TITLE, _STATE_BODY, _STATE_DOCTOR, _STATE_EVENT,
                _STATE_SECTION, _STATE_RESOURCE):
        context.user_data.pop(key, None)
    context.user_data[_STATE_STEP] = "title"

    label = database.NEWS_TYPE_LABELS.get(news_type, news_type)
    hint = {
        "notify": "مثال: محاضرة اليوم — 10:00 بقاعة 3.",
        "section": "اكتب عنوان خبر القسم، ثم اختر القسم الحقيقي من الشجرة.",
        "resource": "اكتب عنوان الخبر، ثم اختر المورد الحقيقي من القائمة.",
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


def _wizard_step(context) -> str:
    """The step the wizard is currently waiting for, or "" when idle."""
    if context.user_data.get(_STATE_TYPE) is None:
        return ""
    step = context.user_data.get(_STATE_STEP)
    if step not in ("title", "body", "doctor", "event", "create"):
        return ""
    return step


async def handle_news_text(update, context) -> bool:
    """Consume typed input for the admin news wizard. Returns handled."""
    step = _wizard_step(context)
    if not step:
        return False

    if not update.message or not update.message.text:
        return False

    if not workflow.owns(context, NEWS_WORKFLOW):
        return False

    if not await _is_manager(update.effective_user.id):
        _clear_state(context)
        await update.message.reply_text("🔒 غير مصرح.", reply_markup=_home_keyboard())
        return True

    text = update.message.text.strip()

    def _cancel_markup():
        return InlineKeyboardMarkup(
            [[btn("❌ إلغاء", "admin_news")], [btn("🏠 الرئيسية", "home")]]
        )

    if text == "/cancel":
        _clear_state(context)
        await update.message.reply_text("❌ تم إلغاء العملية.", reply_markup=_cancel_markup())
        return True

    news_type = context.user_data.get(_STATE_TYPE)

    # ---- Step: title ---------------------------------------------------
    if step == "title":
        context.user_data[_STATE_TITLE] = text
        context.user_data[_STATE_STEP] = "body"
        workflow.begin(context, NEWS_WORKFLOW)
        await update.message.reply_text(
            "📝 أرسل الآن نص الخبر كما سيظهر للطالب، "
            "أو أرسل /skip لتجاهله.\n\nلإلغاء العملية أرسل /cancel.",
            reply_markup=_cancel_markup(),
        )
        return True

    # ---- Step: body ----------------------------------------------------
    if step == "body":
        context.user_data[_STATE_BODY] = None if text == "/skip" else text
        context.user_data[_STATE_STEP] = "doctor"
        await update.message.reply_text(
            "👨‍⚕️ اذكر اسم الطبيب/المُرسل إن أردت، أو أرسل /skip.\n\n"
            "لإلغاء العملية أرسل /cancel.",
            reply_markup=_cancel_markup(),
        )
        return True

    # ---- Step: doctor --------------------------------------------------
    if step == "doctor":
        context.user_data[_STATE_DOCTOR] = None if text == "/skip" else text
        context.user_data[_STATE_STEP] = "event"
        await update.message.reply_text(
            "📅 اذكر موعد الحدث/الاختبار إن وُجد، أو أرسل /skip.\n\n"
            "لإلغاء العملية أرسل /cancel.",
            reply_markup=_cancel_markup(),
        )
        return True

    # ---- Step: event -> create draft -----------------------------------
    context.user_data[_STATE_EVENT] = None if text == "/skip" else text

    title = context.user_data.pop(_STATE_TITLE, None)
    body = context.user_data.pop(_STATE_BODY, None)
    doctor = context.user_data.pop(_STATE_DOCTOR, None)
    event_at = context.user_data.pop(_STATE_EVENT, None)
    context.user_data.pop(_STATE_TYPE, None)
    context.user_data.pop(_STATE_STEP, None)

    if not title:
        await update.message.reply_text(
            "⚠️ تعذّر إنشاء الخبر (عنوان مفقود).",
            reply_markup=_cancel_markup(),
        )
        return True

    news_id = await database.create_news(
        news_type=news_type,
        title=title,
        body=body,
        doctor=doctor,
        event_at=event_at,
        sender_id=update.effective_user.id,
        status="draft",
    )

    if not news_id:
        await update.message.reply_text(
            "⚠️ تعذّر إنشاء الخبر. تحقق من العنوان والنص.",
            reply_markup=_cancel_markup(),
        )
        return True

    await audit.log_action(
        update.effective_user.id, "news_create",
        target_type="news", target_id=news_id, details=f"type={news_type}",
    )

    # A section/resource news must point at a real registry row before it can
    # publish; route straight to the picker so a bogus id can never be typed.
    if news_type == "section":
        next_hint = "اختر القسم الحقيقي من الشجرة أدناه."
    elif news_type == "resource":
        next_hint = "اختر المورد الحقيقي من القائمة أدناه."
    else:
        next_hint = "راجعها ثم اضغط 📢 نشر لإظهارها للطلاب."

    await update.message.reply_text(
        f"✅ تم إنشاء مسودة: <b>{esc(title)}</b>\n\n{next_hint}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [btn("🔎 مراجعة الخبر", f"news_admin_view:{news_id}")],
                [btn("📝 مركز النشر", "admin_news")],
                [btn("🏠 الرئيسية", "home")],
            ]
        ),
    )
    return True


def _clear_state(context):
    for key in _ALL_STATE_KEYS:
        context.user_data.pop(key, None)


async def _publish(query, news_id):
    """Publish a draft, then privately deliver it to subscribers.

    Publishing is authoritative: the row flips to ``published`` first. Delivery
    runs after (best-effort) — a Telegram failure never rolls back the publish,
    it is recorded per recipient and retryable from the delivery log.
    """
    # A section/resource news may not go live without its real reference.
    try:
        news = await database.get_news_detail(news_id)
    except Exception:
        news = None
    if not news:
        await _edit(query, "⚠️ الخبر غير موجود.", _home_keyboard())
        return

    missing = _reference_missing_for(news)
    if missing:
        label = "القسم" if missing == "section" else "المورد"
        await _edit(
            query,
            f"⚠️ لا يمكن النشر قبل اختيار {label} حقيقي من المنصة.",
            InlineKeyboardMarkup(
                [[btn("⬅️ الخبر", f"news_admin_view:{news_id}")],
                 [btn("🏠 الرئيسية", "home")]]
            ),
        )
        return

    ok = await database.publish_news(news_id)
    if ok:
        await audit.log_action(
            query.from_user.id, "news_publish",
            target_type="news", target_id=news_id,
        )
        try:
            await news_delivery.enqueue_publish_delivery(query.get_bot(), news_id)
        except Exception:
            logger.exception("news: publish delivery failed for %s", news_id)
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


async def publish_news_for_resource(bot, content_id, sender_id=None):
    """Auto-generate (once) a 🟢 news row for a registered resource.

    Integration hook for the existing resource system: called after a resource
    is registered (admin upload / approved contribution). It is strictly
    best-effort and idempotent:

    * never raises into the caller — a news failure must not fail the resource
      write, which has already happened by the time this runs;
    * creates at most one news row per content id (`create_resource_news_for_
      content` returns the existing row), so a retry/restart/duplicate event
      cannot spawn a second item;
    * lands the row as ``draft`` and publishes it, then delivers to resource
      subscribers — the publish + delivery are themselves failure-isolated.

    Returns the news id, or None when nothing was created/published.
    """
    try:
        news_id = await database.create_resource_news_for_content(
            content_id, sender_id=sender_id, status="draft"
        )
    except Exception:
        logger.exception("news: auto resource news failed for content %s", content_id)
        return None

    if not news_id:
        return None

    try:
        existing = await database.get_news(news_id)
        if existing and existing.get("status") == "published":
            return news_id  # already handled
    except Exception:
        pass

    try:
        await database.publish_news(news_id)
    except Exception:
        logger.exception("news: could not publish auto resource news %s", news_id)
        return news_id

    try:
        await news_delivery.enqueue_publish_delivery(bot, news_id)
    except Exception:
        logger.exception("news: auto resource delivery failed for %s", news_id)

    return news_id


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

    if data == "news_subs":
        if await _news_hidden_for(query):
            return
        await show_subscriptions(query)
        return

    if data == "news_subs_back":
        if await _news_hidden_for(query):
            return
        await show_subscriptions(query)
        return

    if data.startswith("news_sub:"):
        if await _news_hidden_for(query):
            return
        await toggle_subscription(query, data.split(":", 1)[1])
        return

    if data == "news_subs_sections":
        if await _news_hidden_for(query):
            return
        await show_section_subscriptions(query)
        return

    if data.startswith("news_subs_section:"):
        if await _news_hidden_for(query):
            return
        await toggle_section_subscription(query, data.split(":", 1)[1])
        return

    if data.startswith("news_pick_child:"):
        if await _news_hidden_for(query):
            return
        folder_id = _int_or_none(data.split(":", 1)[1]) or 0
        await show_section_subscriptions(query, parent_id=folder_id)
        return

    if data.startswith("news_pick_root:"):
        if await _news_hidden_for(query):
            return
        parent_id = _int_or_none(data.split(":", 1)[1]) or 0
        await show_section_subscriptions(query, parent_id=parent_id)
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

    if data.startswith("news_admin_deliveries:"):
        news_id = _int_or_none(data.split(":", 1)[1])
        if news_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await show_news_deliveries(query, news_id)
        return

    if data.startswith("news_admin_retry:"):
        news_id = _int_or_none(data.split(":", 1)[1])
        if news_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await retry_deliveries(query, news_id)
        return

    # ---- Admin reference pickers (real registry only) ---------------
    if data.startswith("news_ref_child:"):
        _prefix, news_id, folder_id = _three_parts(data)
        if news_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await pick_section(query, news_id, parent_id=folder_id or 0)
        return

    if data.startswith("news_ref_root:"):
        _prefix, news_id, folder_id = _three_parts(data)
        if news_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await pick_section(query, news_id, parent_id=folder_id or 0)
        return

    if data.startswith("news_ref_set_section:"):
        _prefix, news_id, folder_id = _three_parts(data)
        if news_id is None or folder_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await set_section_reference(query, news_id, folder_id)
        return

    if data.startswith("news_ref_set_resource:"):
        _prefix, news_id, content_id = _three_parts(data)
        if news_id is None or content_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await set_resource_reference(query, news_id, content_id)
        return

    if data.startswith("news_ref_resource:"):
        # Optional folder id: "news_ref_resource:<news_id>" or ":<news_id>:<fid>"
        parts = data.split(":")
        news_id = _int_or_none(parts[1]) if len(parts) > 1 else None
        folder_id = _int_or_none(parts[2]) if len(parts) > 2 else None
        if news_id is None:
            await _edit(query, "⚠️ معرف غير صالح.", _home_keyboard())
            return
        await pick_resource(query, news_id, folder_id=folder_id)
        return

    await _edit(query, "⚠️ إجراء غير معروف.", _home_keyboard())


def _three_parts(data):
    """Parse ``prefix:a:b`` into (prefix, int|None, int|None)."""
    parts = (data or "").split(":")
    a = _int_or_none(parts[1]) if len(parts) > 1 else None
    b = _int_or_none(parts[2]) if len(parts) > 2 else None
    return parts[0], a, b


def register_news_handlers(app):
    """Register the News Center + admin publishing surfaces."""
    app.add_handler(
        CallbackQueryHandler(
            news_callback_handler,
            pattern=r"^(" + "|".join(NEWS_CALLBACKS) + ")",
        )
    )
