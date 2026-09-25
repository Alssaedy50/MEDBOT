"""Tests for the MEDBOT News Phase 2 (Publishing + Subscriptions + Private
Delivery).

Runs against a temporary SQLite database only — never the real one — and
exercises real code paths (migrations, the database service layer, the
delivery engine, the publishing centre and the student surfaces). Only the
Telegram *bot* boundary is stubbed (its network call is external); all
business logic is real.

Covered (mirroring the Phase 2 Definition of Done):
  1.  Migration v14 is additive, idempotent, and untouched tables stay intact.
  2.  Subscriptions: validated add/remove, per-kind and per-section resolution.
  3.  Private delivery: reserve → send → mark, never a duplicate, failure
      isolated per recipient, retry resends only pending/failed.
  4.  Publishing centre: a section/resource news refuses to publish without its
      real reference; the pickers only accept a live registry row.
  5.  Subscriptions control delivery only — the News Center still lists every
      published item to everyone, and a delivered item stays unread.
  6.  Resource-generated news is created once and is failure-isolated.
  7.  The admin surfaces are `can_news`-gated and audited.
  8.  Non-regression: existing notifications broadcast and resource navigation
      keep working.
"""

import os
import unittest

import database
import i18n
import main
import news
import news_delivery
import notifications
from test_medbot_router import (
    _FakeContext,
    _FakeQuery,
    _FakeUpdate,
    _MediaUpdate,
    _TextMessage,
)
from test_medbot_fixes import _RecordingBot


class NewsPhase2Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile

        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-news2-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        database.DB_NAME = self.db_path
        await database.init_db()

        main._CONTENT_MESSAGES_BY_USER.clear()

        self.owner_id = 500
        self.student_id = 900
        self.student_b_id = 901

        await database.add_sub_admin(self.owner_id, "owner")
        await database.ensure_configured_admin(self.owner_id)

        # Real registry: Second Year -> Microbiology -> عملي (+ a resource).
        self.year = await database.add_folder(0, "Second Year", "general")
        self.subject = await database.add_folder(self.year, "Microbiology", "general")
        self.section = await database.add_folder(self.subject, "عملي", "general")
        self.resource = await database.add_content(
            self.section, "Lecture 1", "fid-1", "document"
        )

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        main._CONTENT_MESSAGES_BY_USER.clear()
        await news_delivery.drain_background(timeout=1.0)
        for name in os.listdir(self.tmp_dir):
            try:
                os.remove(os.path.join(self.tmp_dir, name))
            except OSError:
                pass
        os.rmdir(self.tmp_dir)

    async def _make(self, news_type="notify", title="News", status="published",
                    **kwargs):
        news_id = await database.create_news(
            news_type=news_type, title=title, sender_id=self.owner_id,
            status=status, **kwargs
        )
        if status == "published":
            await database.publish_news(news_id)
        return news_id

    async def _open(self, user_id, data, context=None):
        query = _FakeQuery(user_id, data)
        ctx = context or _FakeContext()
        await news.news_callback_handler(_FakeUpdate(query), ctx)
        return query, ctx

    async def _text(self, user_id, text, ctx):
        msg = _TextMessage(text)
        return await news.handle_news_text(_MediaUpdate(user_id, msg), ctx), msg


# ---------------------------------------------------------------
# 1. Migration v14
# ---------------------------------------------------------------


class NewsDeliveryMigrationTests(NewsPhase2Base):
    async def test_deliveries_table_exists(self):
        db = await database.get_db()
        try:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='news_deliveries'"
            ) as cur:
                self.assertIsNotNone(await cur.fetchone())
        finally:
            await db.close()

    async def test_migration_is_idempotent(self):
        await database._migrate_v14(await database.get_db())
        await database._migrate_v14(await database.get_db())
        db = await database.get_db()
        try:
            async with db.execute("SELECT COUNT(*) FROM news_deliveries") as cur:
                self.assertEqual((await cur.fetchone())[0], 0)
        finally:
            await db.close()

    async def test_existing_news_tables_untouched(self):
        nid = await self._make(title="kept")
        self.assertIsNotNone(await database.get_news(nid))
        self.assertEqual(await database.get_news_subscriptions(self.student_id), [])

    async def test_deliveries_primary_key_prevents_duplicate(self):
        nid = await self._make(title="x")
        await database.reserve_news_deliveries(nid, [self.student_id])
        again = await database.reserve_news_deliveries(nid, [self.student_id])
        self.assertEqual(again, [])
        self.assertEqual(
            (await database.get_news_delivery_counts(nid)).get("pending"), 1
        )


# ---------------------------------------------------------------
# 2. Subscriptions
# ---------------------------------------------------------------


class NewsSubscriptionTests(NewsPhase2Base):
    async def test_subscribe_to_a_type(self):
        ok = await database.add_news_subscription(
            self.student_id, database.NEWS_SUB_TYPE, "notify"
        )
        self.assertTrue(ok)
        self.assertIn(
            ("type", "notify"),
            await database.get_news_subscriptions(self.student_id),
        )

    async def test_subscribe_idempotent(self):
        await database.add_news_subscription(self.student_id, "type", "notify")
        await database.add_news_subscription(self.student_id, "type", "notify")
        self.assertEqual(
            await database.get_news_subscriptions(self.student_id), [("type", "notify")]
        )

    async def test_reject_bogus_kind_value(self):
        self.assertFalse(
            await database.add_news_subscription(self.student_id, "type", "bogus")
        )
        self.assertFalse(
            await database.add_news_subscription(self.student_id, "kind", "x")
        )
        self.assertFalse(
            await database.add_news_subscription(self.student_id, "section", "999999")
        )

    async def test_subscribe_to_real_section(self):
        ok = await database.add_news_subscription(
            self.student_id, database.NEWS_SUB_SECTION, str(self.section)
        )
        self.assertTrue(ok)
        mapping = await database.get_news_subscriptions_map([self.student_id])
        self.assertIn(("section", str(self.section)), mapping[self.student_id])

    async def test_remove_subscription(self):
        await database.add_news_subscription(self.student_id, "type", "notify")
        self.assertTrue(
            await database.remove_news_subscription(self.student_id, "type", "notify")
        )
        self.assertEqual(await database.get_news_subscriptions(self.student_id), [])
        self.assertFalse(
            await database.remove_news_subscription(self.student_id, "type", "notify")
        )

    async def test_resolve_recipients_by_type(self):
        await database.add_news_subscription(self.student_id, "type", "notify")
        await database.add_news_subscription(self.student_b_id, "type", "section")
        self.assertEqual(
            await database.resolve_news_recipients("notify"), [self.student_id]
        )

    async def test_resolve_section_includes_type_and_section(self):
        await database.add_news_subscription(self.student_id, "type", "section")
        await database.add_news_subscription(
            self.student_b_id, "section", str(self.section)
        )
        recipients = await database.resolve_news_recipients("section", self.section)
        self.assertEqual(sorted(recipients), sorted([self.student_id, self.student_b_id]))

    async def test_resolve_section_no_ref_only_broad(self):
        await database.add_news_subscription(self.student_id, "type", "section")
        await database.add_news_subscription(
            self.student_b_id, "section", str(self.section)
        )
        self.assertEqual(
            await database.resolve_news_recipients("section", None), [self.student_id]
        )

    async def test_resolve_recipients_deduplicates(self):
        await database.add_news_subscription(self.student_id, "type", "section")
        await database.add_news_subscription(
            self.student_id, "section", str(self.section)
        )
        recipients = await database.resolve_news_recipients("section", self.section)
        self.assertEqual(recipients, [self.student_id])

    async def test_subscriptions_do_not_affect_the_feed(self):
        # A non-subscriber still sees the published item in the News Center.
        await self._make(title="Visible to all")
        query, _ = await self._open(self.student_id, "news")
        labels = [b.text for row in query.last_markup.inline_keyboard for b in row]
        self.assertTrue(any("Visible to all" in label for label in labels))


# ---------------------------------------------------------------
# 3. Private delivery engine
# ---------------------------------------------------------------


class NewsDeliveryEngineTests(NewsPhase2Base):
    async def test_plan_delivery_reserves_subscribers(self):
        nid = await self._make(title="Important")
        await database.add_news_subscription(self.student_id, "type", "notify")
        await database.add_news_subscription(self.student_b_id, "type", "notify")
        news_row, reserved = await news_delivery.plan_delivery(nid)
        self.assertEqual(sorted(reserved), sorted([self.student_id, self.student_b_id]))
        self.assertEqual(
            (await database.get_news_delivery_counts(nid)).get("pending"), 2
        )

    async def test_plan_delivery_ignores_drafts(self):
        nid = await database.create_news("notify", "draft", status="draft")
        await database.add_news_subscription(self.student_id, "type", "notify")
        _row, reserved = await news_delivery.plan_delivery(nid)
        self.assertEqual(reserved, [])

    async def test_deliver_marks_sent_and_reports(self):
        nid = await self._make(title="Important")
        await database.add_news_subscription(self.student_id, "type", "notify")
        await news_delivery.plan_delivery(nid)
        bot = _RecordingBot()
        result = await news_delivery.deliver(bot, nid)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(
            (await database.get_news_delivery_counts(nid)).get("sent"), 1
        )
        self.assertEqual(len(bot.messages), 1)
        self.assertIn("Important", bot.messages[0][1])

    async def test_delivery_is_idempotent(self):
        nid = await self._make(title="Once")
        await database.add_news_subscription(self.student_id, "type", "notify")
        await news_delivery.plan_delivery(nid)
        bot = _RecordingBot()
        await news_delivery.deliver(bot, nid)
        # A second deliver with no pending rows sends nothing new.
        result = await news_delivery.deliver(bot, nid)
        self.assertEqual(result["total"], 0)
        self.assertEqual(len(bot.messages), 1)

    async def test_republish_does_not_duplicate_delivery(self):
        nid = await self._make(title="Once")
        await database.add_news_subscription(self.student_id, "type", "notify")
        bot = _RecordingBot()
        await news_delivery.enqueue_publish_delivery(bot, nid)
        await news_delivery.enqueue_publish_delivery(bot, nid)
        await news_delivery.drain_background(timeout=2.0)
        counts = await database.get_news_delivery_counts(nid)
        self.assertEqual(counts.get("sent"), 1)
        self.assertEqual(len(bot.messages), 1)

    async def test_failure_is_isolated_per_recipient(self):
        nid = await self._make(title="Important")
        await database.add_news_subscription(self.student_id, "type", "notify")
        await database.add_news_subscription(self.student_b_id, "type", "notify")
        await news_delivery.plan_delivery(nid)

        bot_blocked = self.student_id

        class _Bot(_RecordingBot):
            async def send_message(self, chat_id=None, text=None, **kwargs):
                if chat_id == bot_blocked:
                    raise RuntimeError("blocked")
                return await super().send_message(chat_id=chat_id, text=text, **kwargs)

        result = await news_delivery.deliver(_Bot(), nid)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 1)
        counts = await database.get_news_delivery_counts(nid)
        self.assertEqual(counts.get("sent"), 1)
        self.assertEqual(counts.get("failed"), 1)

    async def test_retry_resends_only_failed(self):
        nid = await self._make(title="Retry me")
        await database.add_news_subscription(self.student_id, "type", "notify")
        await database.add_news_subscription(self.student_b_id, "type", "notify")
        await news_delivery.plan_delivery(nid)

        bot_blocked = self.student_id

        class _Bot(_RecordingBot):
            async def send_message(self, chat_id=None, text=None, **kwargs):
                if chat_id == bot_blocked:
                    raise RuntimeError("blocked")
                return await super().send_message(chat_id=chat_id, text=text, **kwargs)

        await news_delivery.deliver(_Bot(), nid)
        self.assertEqual(
            (await database.get_news_delivery_counts(nid)).get("failed"), 1
        )

        # Now the blocked user is reachable: retry delivers only them.
        good = _RecordingBot()
        result = await news_delivery.retry_failed(good, nid)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(len(good.messages), 1)
        self.assertEqual(good.messages[0][0], self.student_id)

    async def test_large_audience_runs_in_background(self):
        nid = await self._make(title="Big")
        for uid in range(1000, 1005):
            await database.add_news_subscription(uid, "type", "notify")

        old_limit = news_delivery.INLINE_DELIVERY_LIMIT
        news_delivery.INLINE_DELIVERY_LIMIT = 2
        try:
            bot = _RecordingBot()
            result = await news_delivery.enqueue_publish_delivery(bot, nid)
            self.assertTrue(result.get("background"))
            await news_delivery.drain_background(timeout=5.0)
        finally:
            news_delivery.INLINE_DELIVERY_LIMIT = old_limit
        self.assertEqual(
            (await database.get_news_delivery_counts(nid)).get("sent"), 5
        )

    async def test_delivery_does_not_mark_read(self):
        nid = await self._make(title="Unread")
        await database.add_news_subscription(self.student_id, "type", "notify")
        await news_delivery.enqueue_publish_delivery(_RecordingBot(), nid)
        await news_delivery.drain_background(timeout=2.0)
        self.assertEqual(await database.get_unread_news_count(self.student_id), 1)

    async def test_message_carries_real_reference_button(self):
        nid = await self._make(
            news_type="resource", title="New file", resource_id=self.resource
        )
        await database.add_news_subscription(self.student_id, "type", "resource")
        await news_delivery.enqueue_publish_delivery(_RecordingBot(), nid)
        markup = news_delivery.build_delivery_markup(
            await database.get_news_detail(nid)
        )
        callbacks = [b.callback_data for row in markup for b in row]
        self.assertIn(f"file:{self.resource}", callbacks)

    async def test_removed_resource_has_no_dead_button(self):
        nid = await self._make(
            news_type="resource", title="Gone", resource_id=self.resource
        )
        await database.delete_file(self.resource)
        markup = news_delivery.build_delivery_markup(
            await database.get_news_detail(nid)
        )
        callbacks = [b.callback_data for row in markup for b in row]
        self.assertFalse(any(c.startswith("file:") for c in callbacks))


# ---------------------------------------------------------------
# 4. Publishing centre
# ---------------------------------------------------------------


class NewsPublishingTests(NewsPhase2Base):
    async def test_section_news_requires_reference_before_publish(self):
        nid = await database.create_news("section", "Micro news", status="draft")
        query, _ = await self._open(self.owner_id, f"news_admin_pub:{nid}")
        self.assertIn("قبل اختيار القسم", query.last_text)
        self.assertEqual((await database.get_news(nid))["status"], "draft")

    async def test_resource_news_requires_reference_before_publish(self):
        nid = await database.create_news("resource", "File news", status="draft")
        query, _ = await self._open(self.owner_id, f"news_admin_pub:{nid}")
        self.assertIn("قبل اختيار المورد", query.last_text)
        self.assertEqual((await database.get_news(nid))["status"], "draft")

    async def test_notify_news_publishes_without_reference(self):
        nid = await database.create_news("notify", "No ref", status="draft")
        await self._open(self.owner_id, f"news_admin_pub:{nid}")
        self.assertEqual((await database.get_news(nid))["status"], "published")

    async def test_publish_delivers_to_subscribers(self):
        await database.add_news_subscription(self.student_id, "type", "notify")
        nid = await database.create_news("notify", "Deliver me", status="draft")
        ctx = _FakeContext()
        ctx.bot = _RecordingBot()
        query = _FakeQuery(self.owner_id, f"news_admin_pub:{nid}")
        await news.news_callback_handler(_FakeUpdate(query), ctx)
        await news_delivery.drain_background(timeout=3.0)
        self.assertEqual(
            (await database.get_news_delivery_counts(nid)).get("sent"), 1
        )

    async def test_section_picker_sets_real_reference(self):
        nid = await database.create_news("section", "Micro news", status="draft")
        query, _ = await self._open(
            self.owner_id, f"news_ref_set_section:{nid}:{self.section}"
        )
        row = await database.get_news(nid)
        self.assertEqual(row["section_folder_id"], self.section)
        self.assertEqual(row["subject_folder_id"], self.subject)

    async def test_resource_picker_sets_real_reference(self):
        nid = await database.create_news("resource", "File news", status="draft")
        query, _ = await self._open(
            self.owner_id, f"news_ref_set_resource:{nid}:{self.resource}"
        )
        self.assertEqual((await database.get_news(nid))["resource_id"], self.resource)

    async def test_section_picker_rejects_unknown_folder(self):
        nid = await database.create_news("section", "Micro news", status="draft")
        query, _ = await self._open(self.owner_id, f"news_ref_set_section:{nid}:99999")
        self.assertIn("غير موجود", query.last_text)
        self.assertIsNone((await database.get_news(nid))["section_folder_id"])

    async def test_resource_picker_rejects_unknown_content(self):
        nid = await database.create_news("resource", "File news", status="draft")
        query, _ = await self._open(
            self.owner_id, f"news_ref_set_resource:{nid}:99999"
        )
        self.assertIn("غير موجود", query.last_text)
        self.assertIsNone((await database.get_news(nid))["resource_id"])

    async def test_picker_browses_live_tree(self):
        nid = await database.create_news("section", "Micro news", status="draft")
        query, _ = await self._open(self.owner_id, f"news_ref_root:{nid}:0")
        labels = [b.text for row in query.last_markup.inline_keyboard for b in row]
        callbacks = [
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        ]
        self.assertTrue(any("Second Year" in label for label in labels))
        self.assertTrue(any(c.startswith("news_ref_child:") for c in callbacks))
        self.assertTrue(any(c.startswith(f"news_ref_set_section:{nid}:") for c in callbacks))

    async def test_draft_menu_offers_picker_not_publish(self):
        nid = await database.create_news("section", "Micro news", status="draft")
        query, _ = await self._open(self.owner_id, f"news_admin_view:{nid}")
        callbacks = [
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        ]
        self.assertTrue(any(c.startswith("news_ref_root:") for c in callbacks))
        self.assertFalse(any(c.startswith("news_admin_pub:") for c in callbacks))

    async def test_published_menu_has_delivery_log(self):
        nid = await self._make(title="Published")
        query, _ = await self._open(self.owner_id, f"news_admin_view:{nid}")
        callbacks = [
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        ]
        self.assertIn(f"news_admin_deliveries:{nid}", callbacks)

    async def test_delivery_log_renders_counts(self):
        nid = await self._make(title="With log")
        await database.add_news_subscription(self.student_id, "type", "notify")
        await news_delivery.enqueue_publish_delivery(_RecordingBot(), nid)
        await news_delivery.drain_background(timeout=2.0)
        query, _ = await self._open(self.owner_id, f"news_admin_deliveries:{nid}")
        self.assertIn("سجل التوصيل", query.last_text)

    async def test_create_is_audited(self):
        ctx = _FakeContext()
        await self._open(self.owner_id, "news_new:notify", ctx)
        await self._text(self.owner_id, "Audited", ctx)
        await self._text(self.owner_id, "/skip", ctx)
        await self._text(self.owner_id, "/skip", ctx)
        await self._text(self.owner_id, "/skip", ctx)
        entries = await database.get_audit_entries(limit=20)
        self.assertTrue(any(e[3] == "news_create" for e in entries))

    async def test_publish_is_audited(self):
        nid = await database.create_news("notify", "Audited pub", status="draft")
        await self._open(self.owner_id, f"news_admin_pub:{nid}")
        entries = await database.get_audit_entries(limit=20)
        self.assertTrue(any(e[3] == "news_publish" for e in entries))


# ---------------------------------------------------------------
# 5. Subscriptions UI
# ---------------------------------------------------------------


class NewsSubscriptionUITests(NewsPhase2Base):
    async def test_subscriptions_screen_lists_three_kinds(self):
        query, _ = await self._open(self.student_id, "news_subs")
        self.assertIn("اشتراكات الأخبار", query.last_text)
        callbacks = [
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        ]
        for kind in database.NEWS_TYPES:
            self.assertIn(f"news_sub:{kind}", callbacks)

    async def test_feed_has_subscriptions_entry(self):
        await self._make(title="Any")
        query, _ = await self._open(self.student_id, "news")
        callbacks = [
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        ]
        self.assertIn("news_subs", callbacks)

    async def test_toggle_type_subscription(self):
        await self._open(self.student_id, "news_sub:notify")
        self.assertIn(
            ("type", "notify"),
            await database.get_news_subscriptions(self.student_id),
        )
        await self._open(self.student_id, "news_sub:notify")
        self.assertEqual(await database.get_news_subscriptions(self.student_id), [])

    async def test_section_browser_lists_real_folders(self):
        query, _ = await self._open(self.student_id, "news_subs_sections")
        self.assertIn("اشتراكات الأقسام", query.last_text)
        labels = [b.text for row in query.last_markup.inline_keyboard for b in row]
        self.assertTrue(any("Second Year" in label for label in labels))

    async def test_toggle_section_subscription(self):
        await self._open(self.student_id, f"news_subs_section:{self.section}")
        self.assertIn(
            ("section", str(self.section)),
            await database.get_news_subscriptions(self.student_id),
        )

    async def test_section_browser_rejects_unknown(self):
        query, _ = await self._open(self.student_id, "news_subs_section:99999")
        self.assertIn("غير موجود", query.last_text)


# ---------------------------------------------------------------
# 6. Resource-generated news
# ---------------------------------------------------------------


class NewsResourceAutoTests(NewsPhase2Base):
    async def test_auto_news_created_and_published_once(self):
        bot = _RecordingBot()
        nid = await news.publish_news_for_resource(bot, self.resource)
        self.assertIsNotNone(nid)
        row = await database.get_news(nid)
        self.assertEqual(row["news_type"], "resource")
        self.assertEqual(row["status"], "published")
        self.assertEqual(row["resource_id"], self.resource)
        self.assertEqual(row["section_folder_id"], self.section)
        self.assertEqual(row["subject_folder_id"], self.subject)

        again = await news.publish_news_for_resource(bot, self.resource)
        self.assertEqual(again, nid)
        self.assertEqual(len(await database.list_news(status="all")), 1)

    async def test_auto_news_delivers_to_resource_subscribers(self):
        await database.add_news_subscription(self.student_id, "type", "resource")
        bot = _RecordingBot()
        nid = await news.publish_news_for_resource(bot, self.resource)
        await news_delivery.drain_background(timeout=2.0)
        self.assertEqual(
            (await database.get_news_delivery_counts(nid)).get("sent"), 1
        )

    async def test_auto_news_unknown_content_returns_none(self):
        self.assertIsNone(
            await news.publish_news_for_resource(_RecordingBot(), 999999)
        )

    async def test_auto_news_never_raises_on_failure(self):
        class _Boom:
            async def send_message(self, **kwargs):
                raise RuntimeError("down")

        # A broken bot must not make the helper raise into the caller.
        nid = await news.publish_news_for_resource(_Boom(), self.resource)
        self.assertIsNotNone(nid)  # news row still created/published


# ---------------------------------------------------------------
# 7. Security / gating
# ---------------------------------------------------------------


class NewsPhase2SecurityTests(NewsPhase2Base):
    async def test_student_cannot_open_reference_picker(self):
        nid = await database.create_news("section", "Micro", status="draft")
        query, _ = await self._open(self.student_id, f"news_ref_root:{nid}:0")
        self.assertIn("غير مصرح", query.last_text)

    async def test_student_cannot_set_reference(self):
        query, _ = await self._open(
            self.student_id, f"news_ref_set_section:1:{self.section}"
        )
        self.assertIn("غير مصرح", query.last_text)

    async def test_student_cannot_view_delivery_log(self):
        nid = await self._make(title="x")
        query, _ = await self._open(self.student_id, f"news_admin_deliveries:{nid}")
        self.assertIn("غير مصرح", query.last_text)

    async def test_student_cannot_retry_deliveries(self):
        nid = await self._make(title="x")
        query, _ = await self._open(self.student_id, f"news_admin_retry:{nid}")
        self.assertIn("غير مصرح", query.last_text)

    async def test_student_subscription_is_their_own(self):
        await self._open(self.student_id, "news_sub:notify")
        self.assertEqual(
            await database.get_news_subscriptions(self.student_b_id), []
        )


# ---------------------------------------------------------------
# 8. Non-regression
# ---------------------------------------------------------------


class NewsPhase2RegressionTests(NewsPhase2Base):
    async def test_notifications_broadcast_still_works(self):
        bot = _RecordingBot()
        await database.register_user(700, "s", "Student")
        recipients, delivered = await notifications.broadcast_notification(
            bot, "old-style broadcast", sender_id=self.owner_id
        )
        self.assertGreaterEqual(recipients, 1)
        self.assertEqual(delivered, recipients)

    async def test_resource_navigation_unaffected(self):
        query = _FakeQuery(self.student_id, f"file:{self.resource}")
        ctx = _FakeContext()
        ctx.bot = _RecordingBot()
        await main.callback_router(_FakeUpdate(query), ctx)
        self.assertTrue(await database.get_file_record(self.resource))

    async def test_i18n_keys_resolve_in_both_languages(self):
        for key in ("news_subs", "news_back_feed", "menu_news"):
            for lang in ("ar", "en"):
                value = i18n.t(key, lang)
                self.assertTrue(value and value != key, (key, lang))

    async def test_news_feature_still_hideable(self):
        await database.set_hidden_features(["news"])
        try:
            query, _ = await self._open(self.student_id, "news_subs")
            self.assertIn("غير متاح", query.last_text)
        finally:
            await database.set_hidden_features([])


if __name__ == "__main__":
    unittest.main()
