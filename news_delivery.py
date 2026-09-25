"""Private News delivery (توصيل الأخبار) for MEDBOT.

Phase 2 transport between a published news row and the students who asked for
it. It is a *service* module: no Telegram handlers, no callback namespace, no
user-facing menu. `news.py` owns the surfaces and calls in here when an admin
publishes (or retries) an item.

Design rules held here:

* Subscriptions decide *private delivery only*. The News Center still lists
  every published item to everyone (see `news.show_news_feed`); an unread item
  stays unread until the student opens it, so reaching a Telegram inbox never
  flips its read state.
* Delivery is idempotent. `news_deliveries` is keyed by ``(news_id, user_id)``,
  so a repeated publish, a retry, a restart or a crash converges on one row and
  one message per recipient. A ``sent`` row is terminal and never resent.
* Failure is isolated. One blocked chat never aborts the batch and never rolls
  back the publish: the row is recorded ``failed`` (with its error) and can be
  retried, while everyone else still receives the item.
* Never blocking. A small audience is delivered inline (so the publish reply is
  accurate); a large one is handed to an `asyncio` task that yields between
  batches, so the Telegram polling loop is not stalled by thousands of sends.
* Grounded in the real registry. The private message carries the item's real
  section/resource references and only offers a button for one that still
  exists — exactly like the News Center detail view.
"""

import asyncio
import logging

from telegram import InlineKeyboardButton
from telegram.constants import ParseMode

import database

logger = logging.getLogger(__name__)

# Recipients up to this count are delivered inline so the admin's publish
# reply can state a truthful result. Beyond it the work is handed to a
# background task to keep the polling loop responsive.
INLINE_DELIVERY_LIMIT = 50

# Messages are sent in small batches with a yield between them: enough to be
# polite to Telegram's rate limits without a real queue or extra dependencies.
DELIVERY_BATCH_SIZE = 25

# A retry resends only `pending`/`failed` rows — never a `sent` one.
MAX_RETRY_PER_RUN = 500

# Background tasks are kept referenced until done (otherwise the loop may
# garbage-collect a running task) and expose a drain hook for tests.
_background_tasks: set = set()


def esc(value) -> str:
    import html

    return html.escape(str(value if value is not None else ""))


def _button(text, callback_data):
    return InlineKeyboardButton(text, callback_data=callback_data)


def delivery_kind(news) -> str:
    """A short label for *why* a user is being reached (delivery log)."""
    if news.get("news_type") == "section" and news.get("section_folder_id"):
        return f"section:{news['section_folder_id']}"
    return news.get("news_type") or "notify"


def build_delivery_text(news) -> str:
    """The private Telegram message body for one news item.

    Mirrors the News Center detail (type, title, real section, timestamp) so a
    student sees the same facts wherever the item appears, plus a pointer to
    the News Center where it stays unread until opened.
    """
    lines = [
        "📰 <b>خبر جديد</b>",
        "",
        f"{database.NEWS_TYPE_ICONS.get(news.get('news_type'), '📰')} "
        f"<b>{esc(news.get('title'))}</b>",
        f"🏷 {esc(database.NEWS_TYPE_LABELS.get(news.get('news_type'), ''))}",
    ]

    if news.get("subject_name"):
        lines.append(f"🧪 المادة: {esc(news['subject_name'])}")
    if news.get("section_name"):
        lines.append(f"🗂 القسم: {esc(news['section_name'])}")
    if news.get("doctor"):
        lines.append(f"👨‍⚕️ {esc(news['doctor'])}")
    if news.get("event_at"):
        lines.append(f"📅 {esc(news['event_at'])}")

    stamp = news.get("published_at") or news.get("created_at")
    if stamp:
        lines.append(f"🕒 {esc(stamp)}")

    body = (news.get("body") or "").strip()
    if body:
        lines.extend(["", esc(body[:600])])

    if news.get("news_type") == "resource" and news.get("resource_present"):
        lines.extend(["", f"📄 {esc(news.get('resource_title') or '')}"])

    lines.extend(["", "افتحه من 📰 مركز الأخبار للاطلاع الكامل."])
    return "\n".join(lines)


def build_delivery_markup(news):
    """Keyboard for the private message: open-in-center plus real access.

    Only a reference that still exists in the registry yields a direct button,
    so a removed resource/section degrades to the News Center entry rather than
    a dead link.
    """
    rows = [[_button("📰 عرض في مركز الأخبار", f"news_open:{news['id']}")]]

    if news.get("news_type") == "resource" and news.get("resource_present"):
        rows.append([_button("📂 عرض المورد", f"file:{news['resource_id']}")])
    elif news.get("folder_id"):
        rows.append([_button("🗂 فتح القسم", f"folder:{news['folder_id']}")])

    return rows


async def plan_delivery(news_id):
    """Resolve the recipients for `news_id` and reserve their delivery rows.

    Returns ``(news, recipients_reserved)``. A non-published row, an unknown
    id or an item with no subscribers yields an empty list — publishing must
    never depend on there being an audience.
    """
    try:
        news = await database.get_news_detail(news_id)
    except Exception:
        logger.exception("news_delivery: could not load news %s", news_id)
        return None, []

    if not news or news.get("status") != "published":
        return news, []

    try:
        recipients = await database.resolve_news_recipients(
            news.get("news_type"), news.get("section_folder_id")
        )
    except Exception:
        logger.exception("news_delivery: could not resolve recipients")
        return news, []

    if not recipients:
        return news, []

    try:
        reserved = await database.reserve_news_deliveries(
            news_id, recipients, kind=delivery_kind(news)
        )
    except Exception:
        logger.exception("news_delivery: could not reserve deliveries")
        return news, []

    return news, reserved


async def _send_batch(bot, news, user_ids) -> dict:
    """Send one batch, isolating every per-recipient failure.

    Returns ``{"sent": n, "failed": n}``. A failure is recorded (``failed`` +
    error) and the loop continues, so a single blocked chat never stops the
    rest of the audience.
    """
    result = {"sent": 0, "failed": 0}
    text = build_delivery_text(news)
    markup = None
    rows = build_delivery_markup(news)
    if rows:
        from telegram import InlineKeyboardMarkup

        markup = InlineKeyboardMarkup(rows)

    for user_id in user_ids:
        try:
            sent = await bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
            message_id = getattr(sent, "message_id", None)
            await database.mark_news_delivery(
                news["id"], user_id, "sent", channel_message_id=message_id
            )
            result["sent"] += 1
        except Exception as exc:
            logger.warning(
                "news_delivery: send failed news=%s user=%s", news["id"], user_id
            )
            try:
                await database.mark_news_delivery(
                    news["id"], user_id, "failed", error=str(exc)
                )
            except Exception:
                logger.exception("news_delivery: could not record failure")
            result["failed"] += 1

    return result


async def deliver(bot, news_id, user_ids=None) -> dict:
    """Deliver `news_id` to `user_ids` (or its reserved pending rows).

    Batched and throttled via ``asyncio.sleep(0)`` between batches; every
    recipient's outcome is persisted. Returns ``{"sent","failed","total"}``.
    Never raises into the caller.
    """
    try:
        news = await database.get_news_detail(news_id)
    except Exception:
        logger.exception("news_delivery: deliver could not load news %s", news_id)
        return {"sent": 0, "failed": 0, "total": 0}

    if not news or news.get("status") != "published":
        return {"sent": 0, "failed": 0, "total": 0}

    if user_ids is None:
        try:
            user_ids = await database.get_pending_news_deliveries(news_id)
        except Exception:
            logger.exception("news_delivery: could not load pending rows")
            return {"sent": 0, "failed": 0, "total": 0}

    user_ids = list(dict.fromkeys(user_ids or []))
    totals = {"sent": 0, "failed": 0, "total": len(user_ids)}

    for start in range(0, len(user_ids), DELIVERY_BATCH_SIZE):
        chunk = user_ids[start:start + DELIVERY_BATCH_SIZE]
        outcome = await _send_batch(bot, news, chunk)
        totals["sent"] += outcome["sent"]
        totals["failed"] += outcome["failed"]
        await asyncio.sleep(0)

    return totals


def _track(task) -> None:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def enqueue_publish_delivery(bot, news_id, wait_small=True) -> dict:
    """Deliver a freshly published item to its subscribers.

    Small audiences are delivered inline (``wait_small``) so the caller can
    report a real count; large ones are scheduled as a background task so the
    polling loop is not blocked. Either way the outcome is persisted and a
    failure never propagates into the publish itself.
    """
    news, reserved = await plan_delivery(news_id)
    if not reserved:
        return {"sent": 0, "failed": 0, "total": 0}

    if wait_small and len(reserved) <= INLINE_DELIVERY_LIMIT:
        return await deliver(bot, news_id, reserved)

    task = asyncio.create_task(deliver(bot, news_id, reserved))
    _track(task)
    return {"sent": 0, "failed": 0, "total": len(reserved), "background": True}


async def retry_failed(bot, news_id) -> dict:
    """Retry the pending/failed deliveries of one item (bounded).

    Already-sent recipients are never contacted again, so a retry cannot
    duplicate a delivery.
    """
    try:
        recipients = await database.get_pending_news_deliveries(news_id)
    except Exception:
        logger.exception("news_delivery: retry could not load rows")
        return {"sent": 0, "failed": 0, "total": 0}

    recipients = recipients[:MAX_RETRY_PER_RUN]
    if not recipients:
        return {"sent": 0, "failed": 0, "total": 0}
    return await deliver(bot, news_id, recipients)


async def drain_background(timeout: float = 10.0) -> None:
    """Await any outstanding background deliveries (test/teardown hook)."""
    tasks = [t for t in list(_background_tasks) if not t.done()]
    if not tasks:
        return
    try:
        await asyncio.wait(tasks, timeout=timeout)
    except Exception:
        logger.debug("news_delivery: drain_background failed", exc_info=True)
