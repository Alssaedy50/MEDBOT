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
* Delivery is best-effort / **at-least-once** in the crash window. The database
  guarantees a caller never queues two reservations for one ``(news, user)``
  and that a ``sent`` row is terminal, but it cannot see the gap between
  "Telegram accepted the message" and "we recorded ``sent``". A crash in that
  window leaves a recoverable ``sending`` row and the item may be delivered
  again after restart. There is no external exactly-once guarantee, by design.
* Failure is isolated. One blocked chat never aborts the batch and never rolls
  back the publish: the row is recorded ``failed`` (with its error) and can be
  retried, while everyone else still receives the item.
* Rate-limited and non-blocking. Sends run in bounded batches with a small
  delay between them and honour Telegram ``RetryAfter``; a small audience is
  delivered inline (so the publish reply is accurate) and a large one is handed
  to a tracked `asyncio` task, so the Telegram polling loop is never stalled.
* Grounded in the real registry. The private message carries the item's real
  section/resource references and only offers a button for one that still
  exists — exactly like the News Center detail view.
"""

import asyncio
import logging

from telegram import InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.error import RetryAfter

import database

logger = logging.getLogger(__name__)

# Recipients up to this count are delivered inline so the admin's publish
# reply can state a truthful result. Beyond it the work is handed to a
# background task to keep the polling loop responsive.
INLINE_DELIVERY_LIMIT = 50

# Rate limiting. Sends are grouped into batches; the loop yields between
# batches (never after the last) so the polling loop stays responsive while
# Telegram's per-chat/burst limits are respected. Tune here, not per call site.
DELIVERY_BATCH_SIZE = 25
DELIVERY_BATCH_DELAY = 1.0  # seconds between batches

# A retry resends only `pending`/`failed` rows — never a `sent` one, and never
# more than this many per run (keeps a single call bounded).
MAX_RETRY_PER_RUN = 500

# Telegram 429 handling: wait what it asks, but never longer than this cap, and
# re-attempt each recipient at most this many times before recording `failed`.
DELIVERY_RETRY_AFTER_CAP = 60.0  # seconds
DELIVERY_MAX_SEND_RETRIES = 2

# Startup recovery is bounded so guests are served first and a big backlog is
# worked through over several passes rather than one thundering herd.
RECOVERY_MAX_NEWS = 50

# Indirection so tests can observe/replace the wait without real sleeping.
_sleep = asyncio.sleep

# Background tasks are kept referenced until done (otherwise the loop may
# garbage-collect a running task) and expose a drain hook for tests.
_background_tasks: set = set()

# The single recovery worker. `start_recovery` is idempotent: a second startup
# call never spawns a competing worker.
_recovery_task = None


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

    if news.get("resource_present") and news.get("resource_id"):
        lines.extend(["", f"📄 {esc(news.get('resource_title') or '')}"])

    lines.extend(["", "افتحه من 📰 مركز الأخبار للاطلاع الكامل."])
    return "\n".join(lines)


def build_delivery_markup(news):
    """Keyboard for the private message: open-in-center plus real access.

    Only a reference that still exists in the registry yields a direct button,
    so a removed resource/section degrades to the News Center entry rather than
    a dead link. A linked resource is offered first; the section is always
    offered when it still exists.
    """
    rows = [[_button("📰 عرض في مركز الأخبار", f"news_open:{news['id']}")]]

    if news.get("resource_present") and news.get("resource_id"):
        rows.append([_button("📂 عرض المورد", f"file:{news['resource_id']}")])
    if news.get("folder_id"):
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


def _retry_after_seconds(exc) -> float:
    """Seconds Telegram asked us to wait, capped and never negative."""
    value = getattr(exc, "retry_after", None)
    if value is None:
        return DELIVERY_RETRY_AFTER_CAP
    if hasattr(value, "total_seconds"):
        value = value.total_seconds()
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DELIVERY_RETRY_AFTER_CAP
    return max(0.0, min(seconds, DELIVERY_RETRY_AFTER_CAP))


async def _send_one(bot, news, user_id, text, markup) -> str:
    """Send to one recipient; return ``"sent"`` or ``"failed"``.

    Claims the row (``sending``) before the Telegram call so a crash mid-send
    is recoverable. A ``RetryAfter`` is honoured (capped) and retried a bounded
    number of times; any other error records ``failed`` and moves on.
    """
    try:
        claimed = await database.claim_news_delivery(news["id"], user_id)
    except Exception:
        logger.exception("news_delivery: could not claim delivery")
        return "failed"

    if not claimed:
        # Already `sent`/`skipped` — never resend a terminal delivery.
        return "skipped"

    for attempt in range(DELIVERY_MAX_SEND_RETRIES + 1):
        try:
            sent = await bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
            await database.mark_news_delivery(
                news["id"], user_id, "sent",
                channel_message_id=getattr(sent, "message_id", None),
            )
            return "sent"
        except RetryAfter as exc:
            wait = _retry_after_seconds(exc)
            if attempt >= DELIVERY_MAX_SEND_RETRIES:
                break
            logger.info(
                "news_delivery: RetryAfter %.1fs news=%s user=%s",
                wait, news["id"], user_id,
            )
            await _sleep(wait)
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
            return "failed"

    try:
        await database.mark_news_delivery(
            news["id"], user_id, "failed", error="RetryAfter exhausted"
        )
    except Exception:
        logger.exception("news_delivery: could not record RetryAfter failure")
    return "failed"


async def _send_batch(bot, news, user_ids) -> dict:
    """Send one batch, isolating every per-recipient failure.

    Returns ``{"sent": n, "failed": n, "skipped": n}``. A failure is recorded
    (``failed`` + error) and the loop continues, so a single blocked chat never
    stops the rest of the audience.
    """
    result = {"sent": 0, "failed": 0, "skipped": 0}
    text = build_delivery_text(news)
    rows = build_delivery_markup(news)
    markup = None
    if rows:
        from telegram import InlineKeyboardMarkup

        markup = InlineKeyboardMarkup(rows)

    for user_id in user_ids:
        outcome = await _send_one(bot, news, user_id, text, markup)
        result[outcome] += 1

    return result


async def deliver(bot, news_id, user_ids=None, throttle: bool = True) -> dict:
    """Deliver `news_id` to `user_ids` (or its reserved pending rows).

    Batched and rate-limited: the loop waits ``DELIVERY_BATCH_DELAY`` between
    batches (never after the last) so the polling loop keeps running while
    Telegram's limits are respected. Every recipient's outcome is persisted.
    Returns ``{"sent","failed","skipped","total"}``. Never raises into the
    caller.
    """
    try:
        news = await database.get_news_detail(news_id)
    except Exception:
        logger.exception("news_delivery: deliver could not load news %s", news_id)
        return {"sent": 0, "failed": 0, "skipped": 0, "total": 0}

    if not news or news.get("status") != "published":
        return {"sent": 0, "failed": 0, "skipped": 0, "total": 0}

    if user_ids is None:
        try:
            user_ids = await database.get_pending_news_deliveries(news_id)
        except Exception:
            logger.exception("news_delivery: could not load pending rows")
            return {"sent": 0, "failed": 0, "skipped": 0, "total": 0}

    user_ids = list(dict.fromkeys(user_ids or []))
    totals = {"sent": 0, "failed": 0, "skipped": 0, "total": len(user_ids)}

    chunks = [
        user_ids[i:i + DELIVERY_BATCH_SIZE]
        for i in range(0, len(user_ids), DELIVERY_BATCH_SIZE)
    ]
    for index, chunk in enumerate(chunks):
        outcome = await _send_batch(bot, news, chunk)
        totals["sent"] += outcome["sent"]
        totals["failed"] += outcome["failed"]
        totals["skipped"] += outcome["skipped"]
        if throttle and index < len(chunks) - 1:
            await _sleep(DELIVERY_BATCH_DELAY)

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
        return {"sent": 0, "failed": 0, "skipped": 0, "total": 0}

    if wait_small and len(reserved) <= INLINE_DELIVERY_LIMIT:
        return await deliver(bot, news_id, reserved)

    task = asyncio.create_task(deliver(bot, news_id, reserved))
    _track(task)
    return {
        "sent": 0, "failed": 0, "skipped": 0,
        "total": len(reserved), "background": True,
    }


async def retry_failed(bot, news_id) -> dict:
    """Retry the pending/failed deliveries of one item (bounded).

    Already-sent recipients are never contacted again, so a retry cannot
    duplicate a delivery.
    """
    try:
        recipients = await database.get_pending_news_deliveries(news_id)
    except Exception:
        logger.exception("news_delivery: retry could not load rows")
        return {"sent": 0, "failed": 0, "skipped": 0, "total": 0}

    recipients = recipients[:MAX_RETRY_PER_RUN]
    if not recipients:
        return {"sent": 0, "failed": 0, "skipped": 0, "total": 0}
    return await deliver(bot, news_id, recipients)


async def recover_pending_deliveries(bot, reset_all_sending: bool = False) -> dict:
    """Finish deliveries left unfinished by a crash/restart (bounded pass).

    * resets crashed ``sending`` claims (Telegram accepted, process died before
      the DB write) back to ``pending``;
    * walks the oldest published items that still have ``pending``/``failed``
      rows, up to ``RECOVERY_MAX_NEWS`` per pass, and runs them through the same
      rate-limited delivery engine.

    ``reset_all_sending`` is for the startup path: a freshly started process
    has no in-flight send, so every ``sending`` row it finds is orphaned and is
    reset immediately. The default (staleness-bounded) reset is used when this
    runs mid-process, where a young claim may legitimately be in flight.
    ``sent``/``skipped`` rows are never selected, so recovery cannot resend a
    completed delivery. Best-effort: failures are logged and never raised.
    """
    summary = {"reset": 0, "news": 0, "sent": 0, "failed": 0, "skipped": 0}
    try:
        summary["reset"] = await database.reset_stale_news_deliveries(
            0 if reset_all_sending else None
        )
    except Exception:
        logger.exception("news_delivery: recovery could not reset stale claims")

    try:
        news_ids = await database.list_recoverable_news_ids(RECOVERY_MAX_NEWS)
    except Exception:
        logger.exception("news_delivery: recovery could not list work")
        return summary

    for news_id in news_ids:
        try:
            outcome = await deliver(bot, news_id)
        except Exception:
            logger.exception(
                "news_delivery: recovery failed for news %s", news_id
            )
            continue
        if outcome.get("total"):
            summary["news"] += 1
            summary["sent"] += outcome.get("sent", 0)
            summary["failed"] += outcome.get("failed", 0)
            summary["skipped"] += outcome.get("skipped", 0)

    if summary["news"] or summary["reset"]:
        logger.info(
            "news_delivery: recovery pass done news=%s reset=%s sent=%s failed=%s",
            summary["news"], summary["reset"], summary["sent"], summary["failed"],
        )
    return summary


def start_recovery(bot) -> bool:
    """Start the delivery-recovery worker once (non-blocking, idempotent).

    Returns True when a worker was started, False when one is already running.
    Never blocks startup: the caller returns immediately and Telegram polling
    proceeds while recovery runs in the background.
    """
    global _recovery_task
    if _recovery_task is not None and not _recovery_task.done():
        return False  # a recovery worker is already active

    async def _run():
        try:
            await recover_pending_deliveries(bot, reset_all_sending=True)
        except Exception:
            logger.exception("news_delivery: recovery worker crashed")

    _recovery_task = asyncio.create_task(_run())
    _track(_recovery_task)
    return True


async def drain_background(timeout: float = 10.0) -> None:
    """Await any outstanding background deliveries (test/teardown hook)."""
    tasks = [t for t in list(_background_tasks) if not t.done()]
    if not tasks:
        return
    try:
        await asyncio.wait(tasks, timeout=timeout)
    except Exception:
        logger.debug("news_delivery: drain_background failed", exc_info=True)
