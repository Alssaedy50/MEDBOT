"""Targeted tests for the Phase 2 review fixes.

Scope is exactly the two CRITICAL issues and two IMPORTANT issues raised in the
review of Phase 2, plus their regression surface:

* CRITICAL 1/2 — delivery crash/restart recovery (`news_delivery.py`,
  `database.claim_news_delivery` / `reset_stale_news_deliveries` /
  `list_recoverable_news_ids`, `main.post_init`).
* IMPORTANT 1 — Telegram-aware rate limiting + `RetryAfter`.
* IMPORTANT 2 — DB-level idempotency for resource-generated news
  (migration v15's partial unique index).

Runs against a temporary SQLite database only and exercises real code paths;
only the Telegram bot boundary is stubbed.
"""

import os
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

from telegram.error import RetryAfter

import database
import news_delivery
from test_medbot_fixes import _RecordingBot


class _ScriptedBot(_RecordingBot):
    """Bot stub whose per-chat behaviour can be scripted (errors, 429s)."""

    def __init__(self, fail_for=(), retry_after_for=(), retry_after_times=1):
        super().__init__()
        self.fail_for = set(fail_for)
        self.retry_after_for = dict(retry_after_for)  # chat_id -> seconds
        self.retry_after_times = retry_after_times
        self._retry_attempts = {}
        self.calls = []

    async def send_message(self, chat_id=None, text=None, **kwargs):
        self.calls.append(chat_id)
        if chat_id in self.retry_after_for:
            seen = self._retry_attempts.get(chat_id, 0)
            if seen < self.retry_after_times:
                self._retry_attempts[chat_id] = seen + 1
                raise RetryAfter(self.retry_after_for[chat_id])
        if chat_id in self.fail_for:
            raise RuntimeError("blocked")
        return await super().send_message(chat_id=chat_id, text=text, **kwargs)


class NewsPhase2FixBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-news2fix-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        database.DB_NAME = self.db_path
        await database.init_db()

        self.owner_id = 500
        self.student_id = 900
        self.student_b_id = 901
        await database.add_sub_admin(self.owner_id, "owner")
        await database.ensure_configured_admin(self.owner_id)

        self.year = await database.add_folder(0, "Second Year", "general")
        self.subject = await database.add_folder(self.year, "Microbiology", "general")
        self.section = await database.add_folder(self.subject, "عملي", "general")
        self.resource = await database.add_content(
            self.section, "Lecture 1", "fid-1", "document"
        )

        # Keep the recovery worker / background set isolated per test.
        news_delivery._recovery_task = None
        news_delivery._background_tasks.clear()

    async def asyncTearDown(self):
        await news_delivery.drain_background(timeout=2.0)
        news_delivery._recovery_task = None
        news_delivery._background_tasks.clear()
        database.DB_NAME = self._old
        for name in os.listdir(self.tmp_dir):
            try:
                os.remove(os.path.join(self.tmp_dir, name))
            except OSError:
                pass
        os.rmdir(self.tmp_dir)

    async def _published(self, title="News", **kwargs):
        nid = await database.create_news(
            news_type=kwargs.pop("news_type", "notify"), title=title,
            sender_id=self.owner_id, **kwargs,
        )
        await database.publish_news(nid)
        return nid


# ---------------------------------------------------------------
# CRITICAL 1/2 — crash / restart recovery
# ---------------------------------------------------------------


class DeliveryRecoveryTests(NewsPhase2FixBase):
    async def test_pending_row_is_recovered_after_restart(self):
        # Simulate a publish that reserved recipients but crashed before
        # sending: rows exist as `pending`, no message was ever sent.
        nid = await self._published(title="Recover me")
        await database.add_news_subscription(self.student_id, "type", "notify")
        await database.reserve_news_deliveries(nid, [self.student_id])

        bot = _RecordingBot()
        summary = await news_delivery.recover_pending_deliveries(bot)

        self.assertEqual(summary["news"], 1)
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(len(bot.messages), 1)
        self.assertEqual(
            (await database.get_news_delivery_counts(nid)).get("sent"), 1
        )

    async def test_failed_row_is_recovered(self):
        nid = await self._published(title="Failed once")
        await database.add_news_subscription(self.student_id, "type", "notify")
        await database.reserve_news_deliveries(nid, [self.student_id])
        await database.mark_news_delivery(nid, self.student_id, "failed", error="x")

        bot = _RecordingBot()
        summary = await news_delivery.recover_pending_deliveries(bot)
        self.assertEqual(summary["sent"], 1)

    async def test_crashed_sending_claim_is_reset_and_redelivered(self):
        # Telegram accepted, process died before mark(sent): the row is stuck
        # `sending`. Recovery must reset it (stale) and redeliver — this is the
        # at-least-once crash window, documented, not hidden.
        nid = await self._published(title="Crashed")
        await database.add_news_subscription(self.student_id, "type", "notify")
        await database.reserve_news_deliveries(nid, [self.student_id])
        await database.claim_news_delivery(nid, self.student_id)

        # Fresh claim does not count as stale.
        self.assertEqual(await database.reset_stale_news_deliveries(900), 0)

        # Age the claim past the stale threshold.
        db = await database.get_db()
        try:
            await db.execute(
                "UPDATE news_deliveries SET updated_at = datetime('now', '-1 hour') "
                "WHERE news_id = ? AND user_id = ?",
                (nid, self.student_id),
            )
            await db.commit()
        finally:
            await db.close()

        self.assertEqual(await database.reset_stale_news_deliveries(900), 1)
        bot = _RecordingBot()
        summary = await news_delivery.recover_pending_deliveries(bot)
        self.assertEqual(summary["sent"], 1)

    async def test_sent_row_is_never_resent_by_recovery(self):
        nid = await self._published(title="Already sent")
        await database.add_news_subscription(self.student_id, "type", "notify")
        await database.reserve_news_deliveries(nid, [self.student_id])
        await database.mark_news_delivery(nid, self.student_id, "sent")

        bot = _RecordingBot()
        summary = await news_delivery.recover_pending_deliveries(bot)
        self.assertEqual(summary["news"], 0)
        self.assertEqual(bot.calls if hasattr(bot, "calls") else bot.messages, [])

    async def test_skipped_row_is_never_resent_by_recovery(self):
        nid = await self._published(title="Skipped")
        await database.add_news_subscription(self.student_id, "type", "notify")
        await database.reserve_news_deliveries(nid, [self.student_id])
        await database.mark_news_delivery(nid, self.student_id, "skipped")

        bot = _RecordingBot()
        summary = await news_delivery.recover_pending_deliveries(bot)
        self.assertEqual(summary["news"], 0)
        self.assertEqual(bot.messages, [])

    async def test_startup_recovery_resets_fresh_orphan_claim(self):
        # A freshly started process holds no in-flight send, so even a young
        # `sending` row is orphaned and must be recovered (not left stuck).
        nid = await self._published(title="Fresh crash")
        await database.reserve_news_deliveries(nid, [self.student_id])
        await database.claim_news_delivery(nid, self.student_id)
        # Not stale yet under the bounded reset.
        self.assertEqual(await database.reset_stale_news_deliveries(900), 0)

        bot = _RecordingBot()
        summary = await news_delivery.recover_pending_deliveries(
            bot, reset_all_sending=True
        )
        self.assertEqual(summary["reset"], 1)
        self.assertEqual(summary["sent"], 1)

    async def test_recovery_ignores_unpublished_news(self):
        nid = await database.create_news("notify", "Draft", status="draft")
        await database.reserve_news_deliveries(nid, [self.student_id])
        self.assertEqual(await database.list_recoverable_news_ids(), [])

    async def test_start_recovery_is_non_blocking(self):
        # Startup must not await the deliveries: the worker is a task and the
        # caller returns immediately.
        nid = await self._published(title="Non blocking")
        await database.add_news_subscription(self.student_id, "type", "notify")
        await database.reserve_news_deliveries(nid, [self.student_id])

        bot = _RecordingBot()
        started = news_delivery.start_recovery(bot)
        self.assertTrue(started)
        self.assertIsNotNone(news_delivery._recovery_task)
        # The task is only scheduled, not completed, when start_recovery returns.
        await news_delivery.drain_background(timeout=3.0)
        self.assertEqual(len(bot.messages), 1)

    async def test_start_recovery_is_idempotent(self):
        bot = _RecordingBot()
        self.assertTrue(news_delivery.start_recovery(bot))
        first = news_delivery._recovery_task
        self.assertFalse(news_delivery.start_recovery(bot))
        self.assertIs(news_delivery._recovery_task, first)
        await news_delivery.drain_background(timeout=3.0)

    async def test_single_failure_does_not_stop_other_recipients(self):
        nid = await self._published(title="Isolated")
        await database.add_news_subscription(self.student_id, "type", "notify")
        await database.add_news_subscription(self.student_b_id, "type", "notify")
        await database.reserve_news_deliveries(
            nid, [self.student_id, self.student_b_id]
        )

        bot = _ScriptedBot(fail_for={self.student_id})
        result = await news_delivery.deliver(bot, nid)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 1)
        counts = await database.get_news_delivery_counts(nid)
        self.assertEqual(counts.get("sent"), 1)
        self.assertEqual(counts.get("failed"), 1)

    async def test_recovery_does_not_duplicate_after_multiple_passes(self):
        nid = await self._published(title="Once")
        await database.add_news_subscription(self.student_id, "type", "notify")
        await database.reserve_news_deliveries(nid, [self.student_id])

        bot = _RecordingBot()
        await news_delivery.recover_pending_deliveries(bot)
        await news_delivery.recover_pending_deliveries(bot)
        self.assertEqual(len(bot.messages), 1)

    async def test_crash_window_semantics_are_documented_not_exactly_once(self):
        # The module must state the at-least-once reality and explicitly deny an
        # exactly-once guarantee; it must not *claim* one.
        doc = news_delivery.__doc__.lower()
        self.assertIn("at-least-once", doc)
        self.assertIn("no external exactly-once guarantee", doc)
        # And the design proves it: a crashed `sending` claim is redelivered.
        nid = await self._published(title="Crash window")
        await database.reserve_news_deliveries(nid, [self.student_id])
        await database.claim_news_delivery(nid, self.student_id)
        db = await database.get_db()
        try:
            await db.execute(
                "UPDATE news_deliveries SET updated_at = datetime('now', '-1 hour') "
                "WHERE news_id = ?", (nid,),
            )
            await db.commit()
        finally:
            await db.close()
        self.assertEqual(await database.reset_stale_news_deliveries(900), 1)
        bot = _RecordingBot()
        await news_delivery.recover_pending_deliveries(bot)
        self.assertEqual(len(bot.messages), 1)


# ---------------------------------------------------------------
# IMPORTANT 1 — rate limiting + RetryAfter
# ---------------------------------------------------------------


class DeliveryRateLimitTests(NewsPhase2FixBase):
    async def _deliver_with_recorder(self, nid, user_ids, batch_size):
        waits = []

        async def _fake_sleep(seconds):
            waits.append(seconds)

        old_size = news_delivery.DELIVERY_BATCH_SIZE
        news_delivery.DELIVERY_BATCH_SIZE = batch_size
        try:
            with mock.patch.object(news_delivery, "_sleep", _fake_sleep):
                await news_delivery.deliver(_RecordingBot(), nid, user_ids)
        finally:
            news_delivery.DELIVERY_BATCH_SIZE = old_size
        return waits

    async def test_rate_limiting_waits_between_batches_not_after_last(self):
        nid = await self._published(title="Throttled")
        users = [1000, 1001, 1002, 1003, 1004]
        waits = await self._deliver_with_recorder(nid, users, batch_size=2)

        # 5 recipients / batch 2 -> 3 batches -> 2 inter-batch waits.
        self.assertEqual(waits, [news_delivery.DELIVERY_BATCH_DELAY] * 2)
        self.assertGreater(news_delivery.DELIVERY_BATCH_DELAY, 0)

    async def test_single_batch_never_waits(self):
        nid = await self._published(title="One batch")
        waits = await self._deliver_with_recorder(nid, [1000, 1001], batch_size=25)
        self.assertEqual(waits, [])

    async def test_retry_after_is_honoured_and_eventually_succeeds(self):
        nid = await self._published(title="429")
        await database.reserve_news_deliveries(nid, [self.student_id])

        waits = []

        async def _fake_sleep(seconds):
            waits.append(seconds)

        bot = _ScriptedBot(retry_after_for={self.student_id: 3}, retry_after_times=1)
        with mock.patch.object(news_delivery, "_sleep", _fake_sleep):
            result = await news_delivery.deliver(bot, nid, [self.student_id])

        self.assertEqual(result["sent"], 1)
        self.assertEqual(waits, [3])  # waited exactly what Telegram asked
        self.assertEqual(
            (await database.get_news_delivery_counts(nid)).get("sent"), 1
        )

    async def test_retry_after_is_capped(self):
        self.assertEqual(news_delivery._retry_after_seconds(RetryAfter(10)), 10.0)
        self.assertEqual(
            news_delivery._retry_after_seconds(RetryAfter(99999)),
            news_delivery.DELIVERY_RETRY_AFTER_CAP,
        )
        self.assertEqual(
            news_delivery._retry_after_seconds(RetryAfter(timedelta(seconds=5))), 5.0
        )

    async def test_persistent_retry_after_records_failed(self):
        nid = await self._published(title="Rate limited")
        await database.reserve_news_deliveries(nid, [self.student_id])

        async def _fake_sleep(seconds):
            return None

        bot = _ScriptedBot(
            retry_after_for={self.student_id: 2},
            retry_after_times=99,
        )
        with mock.patch.object(news_delivery, "_sleep", _fake_sleep):
            result = await news_delivery.deliver(bot, nid, [self.student_id])

        self.assertEqual(result["failed"], 1)
        self.assertEqual(
            (await database.get_news_delivery_counts(nid)).get("failed"), 1
        )


# ---------------------------------------------------------------
# IMPORTANT 2 — resource news DB-level idempotency
# ---------------------------------------------------------------


class ResourceNewsIdempotencyTests(NewsPhase2FixBase):
    async def test_first_creation_makes_one_news(self):
        nid = await database.create_resource_news_for_content(self.resource)
        rows = await database.list_news(status="all")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], nid)

    async def test_repeat_returns_same_row(self):
        first = await database.create_resource_news_for_content(self.resource)
        second = await database.create_resource_news_for_content(self.resource)
        self.assertEqual(first, second)
        self.assertEqual(len(await database.list_news(status="all")), 1)

    async def test_concurrent_creation_makes_no_duplicate(self):
        import asyncio

        ids = await asyncio.gather(
            database.create_resource_news_for_content(self.resource),
            database.create_resource_news_for_content(self.resource),
            database.create_resource_news_for_content(self.resource),
        )
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(await database.list_news(status="all")), 1)

    async def test_direct_duplicate_auto_insert_returns_existing_row(self):
        # Even a direct create_news(source='resource') call is idempotent: the
        # partial unique index turns the race into "return the winner", never a
        # second auto row.
        first = await database.create_resource_news_for_content(self.resource)
        dup = await database.create_news(
            news_type="resource", title="dup", resource_id=self.resource,
            source=database.NEWS_SOURCE_AUTO,
        )
        self.assertEqual(dup, first)
        autos = [r for r in await database.list_news(status="all")
                 if r["source"] == database.NEWS_SOURCE_AUTO]
        self.assertEqual(len(autos), 1)

    async def test_manual_news_for_same_resource_still_allowed(self):
        await database.create_resource_news_for_content(self.resource)
        manual_a = await database.create_news(
            news_type="resource", title="Manual A", resource_id=self.resource,
        )
        manual_b = await database.create_news(
            news_type="resource", title="Manual B", resource_id=self.resource,
        )
        self.assertTrue(manual_a and manual_b)
        rows = await database.list_news(status="all")
        self.assertEqual(len(rows), 3)  # 1 auto + 2 manual

    async def test_migration_idempotent(self):
        db = await database.get_db()
        try:
            await database._migrate_v15(db)
            await database._migrate_v15(db)
        finally:
            await db.close()
        # Still exactly one auto row possible afterwards.
        a = await database.create_resource_news_for_content(self.resource)
        b = await database.create_resource_news_for_content(self.resource)
        self.assertEqual(a, b)

    async def test_migration_collapses_preexisting_duplicates(self):
        # Build a database that already holds duplicate auto rows (as the old
        # non-atomic path could), then run the migration on it.
        db = await database.get_db()
        try:
            await db.execute(
                "DROP INDEX IF EXISTS idx_news_auto_resource_unique"
            )
            for title in ("auto one", "auto two"):
                await db.execute(
                    "INSERT INTO news (news_type, title, resource_id, source, "
                    "status) VALUES ('resource', ?, ?, 'resource', 'published')",
                    (title, self.resource),
                )
            await db.commit()
        finally:
            await db.close()

        self.assertEqual(
            len([r for r in await database.list_news(status="all")
                 if r["source"] == database.NEWS_SOURCE_AUTO]),
            2,
        )

        db = await database.get_db()
        try:
            await database._migrate_v15(db)
        finally:
            await db.close()

        autos = [r for r in await database.list_news(status="all")
                 if r["source"] == database.NEWS_SOURCE_AUTO]
        self.assertEqual(len(autos), 1)
        # The survivor is the earliest row.
        self.assertEqual(autos[0]["title"], "auto one")

    async def test_migration_preserves_unrelated_news(self):
        manual = await database.create_news(
            news_type="notify", title="Untouched", status="published"
        )
        db = await database.get_db()
        try:
            await database._migrate_v15(db)
        finally:
            await db.close()
        self.assertIsNotNone(await database.get_news(manual))


# ---------------------------------------------------------------
# Regression — the admin retry path keeps working with the new engine
# ---------------------------------------------------------------


class DeliveryRetryRegressionTests(NewsPhase2FixBase):
    async def test_admin_retry_uses_engine_and_records(self):
        nid = await self._published(title="Admin retry")
        await database.add_news_subscription(self.student_id, "type", "notify")
        await database.reserve_news_deliveries(nid, [self.student_id])
        await database.mark_news_delivery(nid, self.student_id, "failed", error="x")

        bot = _RecordingBot()
        result = await news_delivery.retry_failed(bot, nid)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(len(bot.messages), 1)

    async def test_recovery_summary_shape(self):
        bot = _RecordingBot()
        summary = await news_delivery.recover_pending_deliveries(bot)
        for key in ("reset", "news", "sent", "failed", "skipped"):
            self.assertIn(key, summary)

    async def test_explicit_user_ids_delivery_creates_its_row(self):
        # Backward compatibility: deliver(bot, nid, [uid]) with no prior
        # reservation still sends and records one `sent` row.
        nid = await self._published(title="Explicit")
        bot = _RecordingBot()
        result = await news_delivery.deliver(bot, nid, [self.student_id])
        self.assertEqual(result["sent"], 1)
        self.assertEqual(len(bot.messages), 1)
        self.assertEqual(
            (await database.get_news_delivery_counts(nid)).get("sent"), 1
        )


if __name__ == "__main__":
    unittest.main()
