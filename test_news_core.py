"""Tests for the MEDBOT News Core (Phase 1).

Runs against a temporary SQLite database only — never the real one — and
exercises real code paths (the database layer, the News Center renderer and the
admin publishing surface). No business logic is mocked.

Covered (mirroring the Phase 1 Definition of Done):
  1.  Migration v13 is additive, idempotent and creates the three tables.
  2.  The News Core entity supports the three kinds and their real references.
  3.  A news row never references a folder/resource that does not exist.
  4.  The News Center feed renders newest-first with pagination.
  5.  Reading a news item is tracked independently (read/unread, no dupes).
  6.  Publish / archive / restore lifecycle and archive filtering.
  7.  Resource access resolves the real content row; section access the real
      folder; a removed resource degrades to no access button.
  8.  The unread badge appears on the home keyboard and clears once read.
  9.  The admin surface is gated by `can_news` and audited.
  10. News never breaks the existing notification broadcast.
  11. The future-compatibility seams (subscriptions table, delivery_scope,
      source) exist without being wired to the student UI.
"""

import os
import tempfile
import unittest

import database
import i18n
import main
import news
import notifications
from test_medbot_router import (
    _FakeContext,
    _FakeQuery,
    _FakeUpdate,
    _MediaUpdate,
    _TextMessage,
)
from test_medbot_fixes import _RecordingBot


class NewsBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-news-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        database.DB_NAME = self.db_path
        await database.init_db()

        main._CONTENT_MESSAGES_BY_USER.clear()

        self.owner_id = 500
        self.sub_id = 501
        self.student_id = 900

        await database.add_sub_admin(self.owner_id, "owner")
        await database.add_sub_admin(self.sub_id, "sub")
        await database.ensure_configured_admin(self.owner_id)

        # A real registry hierarchy: Second Year -> Microbiology -> عملي.
        self.year = await database.add_folder(0, "Second Year", "general")
        self.subject = await database.add_folder(self.year, "Microbiology", "general")
        self.section = await database.add_folder(self.subject, "عملي", "general")
        self.resource = await database.add_content(
            self.section, "Lecture 1", "fid-1", "document"
        )

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        main._CONTENT_MESSAGES_BY_USER.clear()
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


# ---------------------------------------------------------------
# 1. Migration / schema
# ---------------------------------------------------------------


class NewsMigrationTests(NewsBase):
    async def test_news_tables_exist(self):
        db = await database.get_db()
        try:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) as cur:
                tables = {row[0] for row in await cur.fetchall()}
        finally:
            await db.close()
        self.assertIn("news", tables)
        self.assertIn("news_reads", tables)
        self.assertIn("news_subscriptions", tables)

    async def test_migration_is_idempotent(self):
        nid = await self._make(title="Idempotent")
        # Re-running every migration must not drop or duplicate the row.
        await database.init_db()
        self.assertIsNotNone(await database.get_news(nid))

    async def test_existing_tables_are_untouched(self):
        db = await database.get_db()
        try:
            async with db.execute("SELECT COUNT(*) FROM folders") as cur:
                folders = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM content") as cur:
                contents = (await cur.fetchone())[0]
        finally:
            await db.close()
        self.assertEqual(folders, 3)
        self.assertEqual(contents, 1)

    async def test_news_types_cover_the_two_kinds(self):
        # A resource is no longer a news kind: it is an optional linked
        # reference on a 📚 Section News item.
        self.assertEqual(set(database.NEWS_TYPES), {"notify", "section"})
        for key in database.NEWS_TYPES:
            self.assertIn(key, database.NEWS_TYPE_LABELS)

    async def test_indexes_created(self):
        db = await database.get_db()
        try:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ) as cur:
                indexes = {row[0] for row in await cur.fetchall()}
        finally:
            await db.close()
        self.assertIn("idx_news_status_created", indexes)
        self.assertIn("idx_news_reads_user", indexes)


class NewsMigrationV17Tests(NewsBase):
    """Pins migration v17: the legacy `resource` kind is folded into `section`."""

    async def test_resource_rows_become_section(self):
        # Simulate a pre-v17 row written directly with the legacy kind.
        db = await database.get_db()
        try:
            cur = await db.execute(
                "INSERT INTO news (news_type, title, status, resource_id, source) "
                "VALUES ('resource', 'Old file', 'published', ?, 'resource')",
                (self.resource,),
            )
            await db.commit()
            nid = cur.lastrowid
        finally:
            await db.close()
        db = await database.get_db()
        try:
            await database._migrate_v17(db)
        finally:
            await db.close()
        row = await database.get_news(nid)
        self.assertEqual(row["news_type"], "section")
        # The linked resource is preserved, so its access button still resolves.
        self.assertEqual(row["resource_id"], self.resource)

    async def test_resource_type_subscription_becomes_section(self):
        await database.add_news_subscription(self.student_id, "type", "resource")
        db = await database.get_db()
        try:
            await database._migrate_v17(db)
        finally:
            await db.close()
        subs = await database.get_news_subscriptions(self.student_id)
        self.assertIn(("type", "section"), subs)
        self.assertNotIn(("type", "resource"), subs)

    async def test_migration_v17_is_idempotent(self):
        db = await database.get_db()
        try:
            await database._migrate_v17(db)
            await database._migrate_v17(db)
        finally:
            await db.close()
        self.assertEqual(set(database.NEWS_TYPES), {"notify", "section"})


# ---------------------------------------------------------------
# 2/3. News Core entity + real references
# ---------------------------------------------------------------


class NewsEntityTests(NewsBase):
    async def test_create_the_two_kinds(self):
        notify = await database.create_news(
            "notify", "Lecture today", "10:00 hall 3", sender_id=self.owner_id,
            event_at="2026-10-01 10:00", status="published",
        )
        section = await database.create_news(
            "section", "Micro عملي", sender_id=self.owner_id,
            subject_folder_id=self.subject, section_folder_id=self.section,
            status="published",
        )
        for nid, kind in ((notify, "notify"), (section, "section")):
            row = await database.get_news(nid)
            self.assertIsNotNone(row)
            self.assertEqual(row["news_type"], kind)

    async def test_legacy_resource_type_is_normalized_to_section(self):
        # The old `resource` kind folds into 📚 Section News, keeping the linked
        # resource reference so the access button still resolves.
        nid = await database.create_news(
            "resource", "New file", sender_id=self.owner_id,
            resource_id=self.resource, status="published",
        )
        row = await database.get_news(nid)
        self.assertEqual(row["news_type"], "section")
        self.assertEqual(row["resource_id"], self.resource)

    async def test_row_exposes_the_full_entity(self):
        nid = await self._make(
            "section", "t", body="b", subject_folder_id=self.subject,
            section_folder_id=self.section, doctor="Dr. A",
            event_at="2026-10-01", resource_id=self.resource,
        )
        row = await database.get_news(nid)
        for key in (
            "id", "news_type", "title", "body", "sender_id",
            "subject_folder_id", "section_folder_id", "folder_id", "doctor",
            "event_at", "resource_id", "visibility", "status",
            "delivery_scope", "source", "created_at", "published_at",
            "archived_at",
        ):
            self.assertIn(key, row)

    async def test_folder_id_anchor_follows_section(self):
        nid = await self._make(
            "section", "anchor", subject_folder_id=self.subject,
            section_folder_id=self.section,
        )
        self.assertEqual((await database.get_news(nid))["folder_id"], self.section)

    async def test_unknown_type_is_rejected(self):
        self.assertIsNone(await database.create_news("bogus", "title"))

    async def test_empty_or_oversized_title_is_rejected(self):
        self.assertIsNone(await database.create_news("notify", "   "))
        self.assertIsNone(
            await database.create_news(
                "notify", "x" * (database.MAX_NEWS_TITLE_LENGTH + 1)
            )
        )

    async def test_nonexistent_folder_reference_is_rejected(self):
        self.assertIsNone(
            await database.create_news(
                "section", "ghost", section_folder_id=999999
            )
        )
        self.assertIsNone(
            await database.create_news(
                "section", "ghost", subject_folder_id=999999
            )
        )

    async def test_nonexistent_resource_reference_is_rejected(self):
        self.assertIsNone(
            await database.create_news(
                "resource", "ghost resource", resource_id=999999
            )
        )

    async def test_invalid_lifecycle_values_rejected(self):
        self.assertIsNone(await database.create_news("notify", "t", status="bogus"))
        self.assertIsNone(
            await database.create_news("notify", "t", visibility="bogus")
        )
        self.assertIsNone(
            await database.create_news("notify", "t", delivery_scope="bogus")
        )

    async def test_detail_resolves_real_registry_names(self):
        nid = await self._make(
            "section", "detail", subject_folder_id=self.subject,
            section_folder_id=self.section,
        )
        detail = await database.get_news_detail(nid)
        self.assertEqual(detail["subject_name"], "Microbiology")
        self.assertEqual(detail["section_name"], "عملي")
        self.assertIn("Microbiology", detail["folder_path"])
        self.assertIn("عملي", detail["folder_path"])

    async def test_detail_tracks_a_renamed_folder(self):
        nid = await self._make(
            "section", "renamed", section_folder_id=self.section,
            subject_folder_id=self.subject,
        )
        await database.update_folder_name(self.section, "Practical")
        detail = await database.get_news_detail(nid)
        self.assertEqual(detail["section_name"], "Practical")

    async def test_detail_marks_removed_resource_absent(self):
        nid = await self._make("resource", "gone", resource_id=self.resource)
        await database.delete_file(self.resource)
        detail = await database.get_news_detail(nid)
        self.assertFalse(detail["resource_present"])
        self.assertIsNone(detail["resource_title"])


# ---------------------------------------------------------------
# 4. News Center feed / ordering / pagination
# ---------------------------------------------------------------


class NewsFeedTests(NewsBase):
    async def test_feed_is_newest_first(self):
        for i in range(3):
            await self._make(title=f"News {i}")
        feed = await database.list_news()
        self.assertEqual([n["title"] for n in feed], ["News 2", "News 1", "News 0"])

    async def test_oldest_order_is_available(self):
        for i in range(3):
            await self._make(title=f"News {i}")
        feed = await database.list_news(order="oldest")
        self.assertEqual([n["title"] for n in feed], ["News 0", "News 1", "News 2"])

    async def test_drafts_and_archived_are_hidden_from_feed(self):
        await self._make(title="visible")
        await self._make(title="draft", status="draft")
        archived = await self._make(title="archived")
        await database.archive_news(archived)
        feed = await database.list_news()
        self.assertEqual([n["title"] for n in feed], ["visible"])

    async def test_admin_listing_can_include_non_archived_states(self):
        await self._make(title="published")
        await self._make(title="draft", status="draft")
        titles = {n["title"] for n in await database.list_news(status="all")}
        self.assertEqual(titles, {"published", "draft"})

    async def test_pagination_offsets_do_not_overlap(self):
        for i in range(7):
            await self._make(title=f"N{i}")
        page1 = await database.list_news(limit=3, offset=0)
        page2 = await database.list_news(limit=3, offset=3)
        self.assertEqual(len(page1), 3)
        self.assertEqual(len(page2), 3)
        self.assertFalse({n["id"] for n in page1} & {n["id"] for n in page2})
        self.assertEqual(await database.count_news(), 7)

    async def test_filter_by_type(self):
        await self._make("notify", "n1")
        await self._make("section", "s1", section_folder_id=self.section,
                         subject_folder_id=self.subject)
        # A legacy `resource` row is normalized to `section` on create, so it
        # is reached by the section filter (a resource is a linked reference,
        # never a kind of its own).
        await self._make("resource", "r1", resource_id=self.resource)
        sections = await database.list_news(news_type="section")
        self.assertEqual(
            sorted(n["title"] for n in sections), ["r1", "s1"]
        )

    async def test_feed_screen_renders_and_lists(self):
        await self._make(title="Hello students")
        query, _ = await self._open(self.student_id, "news")
        self.assertIn("مركز الأخبار", query.last_text)
        callbacks = [
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        ]
        self.assertTrue(any(c.startswith("news_open:") for c in callbacks))

    async def test_feed_screen_empty_state(self):
        query, _ = await self._open(self.student_id, "news")
        self.assertIn(i18n.t("news_empty"), query.last_text)

    async def test_feed_marks_read_items(self):
        nid = await self._make(title="Read me")
        await database.mark_news_read(self.student_id, nid)
        query, _ = await self._open(self.student_id, "news")
        labels = [
            b.text for row in query.last_markup.inline_keyboard for b in row
        ]
        self.assertTrue(any("✅" in label for label in labels))

    async def test_more_button_appears_when_more_pages(self):
        for i in range(database.NEWS_PAGE_SIZE + 2):
            await self._make(title=f"N{i}")
        query, _ = await self._open(self.student_id, "news")
        callbacks = [
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        ]
        self.assertIn("news_more:1", callbacks)

    async def test_type_filter_callback(self):
        await self._make("notify", "only notify")
        query, _ = await self._open(self.student_id, "news_filter:notify")
        self.assertIn("مركز الأخبار", query.last_text)


# ---------------------------------------------------------------
# 5. Read / unread tracking
# ---------------------------------------------------------------


class NewsReadTests(NewsBase):
    async def test_mark_read_and_query(self):
        nid = await self._make(title="r")
        self.assertFalse(await database.is_news_read(self.student_id, nid))
        await database.mark_news_read(self.student_id, nid)
        self.assertTrue(await database.is_news_read(self.student_id, nid))

    async def test_duplicate_read_records_prevented(self):
        nid = await self._make(title="dup")
        for _ in range(5):
            await database.mark_news_read(self.student_id, nid)
        db = await database.get_db()
        try:
            async with db.execute(
                "SELECT COUNT(*) FROM news_reads WHERE user_id = ? AND news_id = ?",
                (self.student_id, nid),
            ) as cur:
                count = (await cur.fetchone())[0]
        finally:
            await db.close()
        self.assertEqual(count, 1)

    async def test_unread_count_is_per_user(self):
        await self._make(title="a")
        await self._make(title="b")
        self.assertEqual(await database.get_unread_news_count(self.student_id), 2)
        self.assertEqual(await database.get_unread_news_count(self.sub_id), 2)

        nid = (await database.list_news())[0]["id"]
        await database.mark_news_read(self.student_id, nid)
        self.assertEqual(await database.get_unread_news_count(self.student_id), 1)
        self.assertEqual(await database.get_unread_news_count(self.sub_id), 2)

    async def test_unread_count_ignores_drafts_and_archived(self):
        await self._make(title="published")
        await self._make(title="draft", status="draft")
        archived = await self._make(title="archived")
        await database.archive_news(archived)
        self.assertEqual(await database.get_unread_news_count(self.student_id), 1)

    async def test_get_read_ids_subset(self):
        a = await self._make(title="a")
        b = await self._make(title="b")
        c = await self._make(title="c")
        await database.mark_news_read(self.student_id, b)
        read = await database.get_read_news_ids(self.student_id, [a, b, c])
        self.assertEqual(read, {b})

    async def test_mark_all_read(self):
        for i in range(3):
            await self._make(title=f"n{i}")
        await database.mark_all_news_read(self.student_id)
        self.assertEqual(await database.get_unread_news_count(self.student_id), 0)

    async def test_opening_news_marks_it_read(self):
        nid = await self._make(title="open me")
        await self._open(self.student_id, f"news_open:{nid}")
        self.assertTrue(await database.is_news_read(self.student_id, nid))

    async def test_read_all_callback(self):
        await self._make(title="x")
        query, _ = await self._open(self.student_id, "news_readall")
        self.assertEqual(await database.get_unread_news_count(self.student_id), 0)
        self.assertIn("مركز الأخبار", query.last_text)


# ---------------------------------------------------------------
# 6. Lifecycle: publish / archive / restore / delete
# ---------------------------------------------------------------


class NewsLifecycleTests(NewsBase):
    async def test_draft_becomes_visible_only_after_publish(self):
        nid = await database.create_news(
            "notify", "draft first", sender_id=self.owner_id, status="draft"
        )
        self.assertEqual(await database.list_news(), [])
        await database.publish_news(nid)
        self.assertEqual(len(await database.list_news()), 1)

    async def test_repeated_publish_keeps_published_at(self):
        nid = await self._make(title="stable")
        first = (await database.get_news(nid))["published_at"]
        await database.publish_news(nid)
        self.assertEqual((await database.get_news(nid))["published_at"], first)

    async def test_archive_hides_from_feed_but_keeps_row(self):
        nid = await self._make(title="arch")
        await database.archive_news(nid)
        self.assertEqual(await database.list_news(), [])
        row = await database.get_news(nid)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "archived")
        self.assertIsNotNone(row["archived_at"])

    async def test_restore_returns_to_draft_not_published(self):
        nid = await self._make(title="restore")
        await database.archive_news(nid)
        await database.restore_news(nid)
        self.assertEqual((await database.get_news(nid))["status"], "draft")
        self.assertEqual(await database.list_news(), [])

    async def test_delete_removes_row_and_reads(self):
        nid = await self._make(title="del")
        await database.mark_news_read(self.student_id, nid)
        await database.delete_news(nid)
        self.assertIsNone(await database.get_news(nid))
        db = await database.get_db()
        try:
            async with db.execute(
                "SELECT COUNT(*) FROM news_reads WHERE news_id = ?", (nid,)
            ) as cur:
                self.assertEqual((await cur.fetchone())[0], 0)
        finally:
            await db.close()

    async def test_update_changes_only_editable_fields(self):
        nid = await self._make(title="old")
        await database.update_news(nid, title="new", status="published")
        row = await database.get_news(nid)
        self.assertEqual(row["title"], "new")
        self.assertEqual(row["status"], "published")

    async def test_update_rejects_bogus_reference(self):
        nid = await self._make(title="up")
        self.assertFalse(
            await database.update_news(nid, section_folder_id=999999)
        )


# ---------------------------------------------------------------
# 7. Resource / section access
# ---------------------------------------------------------------


class NewsAccessTests(NewsBase):
    async def test_resource_news_offers_real_resource_button(self):
        nid = await self._make("resource", "New resource", resource_id=self.resource)
        query, _ = await self._open(self.student_id, f"news_open:{nid}")
        callbacks = [
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        ]
        self.assertIn(f"file:{self.resource}", callbacks)

    async def test_resource_news_without_live_row_offers_no_file_button(self):
        nid = await self._make("resource", "gone", resource_id=self.resource)
        await database.delete_file(self.resource)
        query, _ = await self._open(self.student_id, f"news_open:{nid}")
        callbacks = [
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        ]
        self.assertFalse(any(c.startswith("file:") for c in callbacks))

    async def test_section_news_offers_real_folder_button(self):
        nid = await self._make(
            "section", "Section news", subject_folder_id=self.subject,
            section_folder_id=self.section,
        )
        query, _ = await self._open(self.student_id, f"news_open:{nid}")
        callbacks = [
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        ]
        self.assertIn(f"folder:{self.section}", callbacks)

    async def test_notification_without_reference_has_no_access_button(self):
        nid = await self._make("notify", "Just a notice")
        query, _ = await self._open(self.student_id, f"news_open:{nid}")
        callbacks = [
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        ]
        self.assertFalse(any(c.startswith("folder:") for c in callbacks))
        self.assertFalse(any(c.startswith("file:") for c in callbacks))

    async def test_missing_news_fails_safely(self):
        query, _ = await self._open(self.student_id, "news_open:999999")
        self.assertIn("غير موجود", query.last_text)

    async def test_malformed_news_id_fails_safely(self):
        query, _ = await self._open(self.student_id, "news_open:abc")
        self.assertIn("غير صالح", query.last_text)

    # ---- H1: a student may only open published news -------------------

    async def test_student_cannot_open_draft(self):
        nid = await self._make("notify", "SECRET DRAFT", status="draft")
        query, _ = await self._open(self.student_id, f"news_open:{nid}")
        self.assertIn("غير موجود", query.last_text)
        self.assertNotIn("SECRET DRAFT", query.last_text)

    async def test_student_cannot_open_archived(self):
        nid = await self._make("notify", "SECRET ARCHIVED")
        await database.archive_news(nid)
        query, _ = await self._open(self.student_id, f"news_open:{nid}")
        self.assertIn("غير موجود", query.last_text)
        self.assertNotIn("SECRET ARCHIVED", query.last_text)

    async def test_refused_open_creates_no_read_record(self):
        draft = await self._make("notify", "draft", status="draft")
        archived = await self._make("notify", "old")
        await database.archive_news(archived)

        await self._open(self.student_id, f"news_open:{draft}")
        await self._open(self.student_id, f"news_open:{archived}")

        self.assertFalse(await database.is_news_read(self.student_id, draft))
        self.assertFalse(await database.is_news_read(self.student_id, archived))
        self.assertEqual(
            await database.get_unread_news_count(self.student_id), 0
        )

    async def test_published_open_creates_read_record(self):
        nid = await self._make("notify", "visible")
        await self._open(self.student_id, f"news_open:{nid}")
        self.assertTrue(await database.is_news_read(self.student_id, nid))

    async def test_admin_can_preview_draft_without_recording_a_read(self):
        nid = await self._make("notify", "SECRET DRAFT", status="draft")
        query, _ = await self._open(
            self.owner_id, f"news_admin_view:{nid}", _FakeContext()
        )
        self.assertIn("SECRET DRAFT", query.last_text)
        self.assertFalse(await database.is_news_read(self.owner_id, nid))

    async def test_admin_can_preview_archived(self):
        nid = await self._make("notify", "OLD NOTICE")
        await database.archive_news(nid)
        query, _ = await self._open(
            self.owner_id, f"news_admin_view:{nid}", _FakeContext()
        )
        self.assertIn("OLD NOTICE", query.last_text)

    async def test_admin_student_style_preview_is_readonly(self):
        nid = await self._make("notify", "draft text", status="draft")
        query, _ = await self._open(
            self.owner_id, f"news_admin_preview:{nid}", _FakeContext()
        )
        self.assertIn("draft text", query.last_text)
        self.assertIn("معاينة", query.last_text)
        self.assertFalse(await database.is_news_read(self.owner_id, nid))
        # Lifecycle actions stay reachable right after the preview.
        callbacks = [
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        ]
        self.assertIn(f"news_admin_pub:{nid}", callbacks)


# ---------------------------------------------------------------
# 8. Home unread badge
# ---------------------------------------------------------------


class NewsHomeBadgeTests(NewsBase):
    @staticmethod
    def _labels(markup):
        return [
            (b.text, b.callback_data)
            for row in markup.inline_keyboard
            for b in row
        ]

    async def test_home_shows_unread_badge(self):
        await self._make(title="a")
        await self._make(title="b")
        markup = await main.home_for(_FakeUpdate(_FakeQuery(self.student_id, "home")))
        callbacks = {c: t for t, c in self._labels(markup)}
        self.assertIn("news", callbacks)
        self.assertIn("2", callbacks["news"])

    async def test_badge_clears_once_all_read(self):
        nid = await self._make(title="a")
        await database.mark_news_read(self.student_id, nid)
        markup = await main.home_for(_FakeUpdate(_FakeQuery(self.student_id, "home")))
        callbacks = {c: t for t, c in self._labels(markup)}
        self.assertNotIn("🔴", callbacks["news"])

    async def test_news_entry_present_on_public_keyboard(self):
        callbacks = [c for _t, c in self._labels(main.home_keyboard("ar"))]
        self.assertIn("news", callbacks)

    async def test_news_feature_is_hideable_and_blocks_callback(self):
        await database.set_hidden_features({"news"})
        query = _FakeQuery(self.student_id, "news")
        ctx = _FakeContext()
        # News registers its own handler: the hidden gate lives there.
        await news.news_callback_handler(_FakeUpdate(query), ctx)
        self.assertIn("غير متاح مؤقتاً", query.last_text)

    async def test_admin_bypasses_news_hidden_gate(self):
        await database.set_hidden_features({"news"})
        query, _ = await self._open(self.owner_id, "news")
        self.assertNotIn("غير متاح مؤقتاً", query.last_text)


# ---------------------------------------------------------------
# 9. Admin publishing surface
# ---------------------------------------------------------------


class NewsAdminTests(NewsBase):
    async def test_owner_can_open_news_admin(self):
        query, _ = await self._open(self.owner_id, "admin_news", _FakeContext())
        self.assertIn("الأخبار", query.last_text)
        callbacks = [
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        ]
        self.assertIn("news_new", callbacks)

    async def test_student_cannot_open_news_admin(self):
        query, _ = await self._open(self.student_id, "admin_news", _FakeContext())
        self.assertIn("غير مصرح", query.last_text)

    async def test_admin_without_can_news_denied(self):
        perms = await database.get_admin_permissions(self.sub_id)
        perms = dict(perms)
        perms["can_news"] = False
        await database.update_admin_permissions(self.sub_id, perms)
        query, _ = await self._open(self.sub_id, "admin_news", _FakeContext())
        self.assertIn("غير مصرح", query.last_text)

    async def test_publish_is_audited(self):
        nid = await self._make(title="audit", status="draft")
        await self._open(self.owner_id, f"news_admin_pub:{nid}", _FakeContext())
        self.assertEqual(
            (await database.get_news(nid))["status"], "published"
        )
        entries = await database.get_audit_entries(action="news_publish")
        self.assertEqual(len(entries), 1)

    async def test_archive_and_restore_are_audited(self):
        nid = await self._make(title="lifecycle")
        await self._open(self.owner_id, f"news_admin_archive:{nid}", _FakeContext())
        self.assertEqual((await database.get_news(nid))["status"], "archived")
        await self._open(self.owner_id, f"news_admin_restore:{nid}", _FakeContext())
        self.assertEqual((await database.get_news(nid))["status"], "draft")
        self.assertEqual(len(await database.get_audit_entries(action="news_archive")), 1)
        self.assertEqual(len(await database.get_audit_entries(action="news_restore")), 1)

    async def test_delete_is_audited(self):
        nid = await self._make(title="delete me")
        await self._open(self.owner_id, f"news_admin_delete:{nid}", _FakeContext())
        self.assertIsNone(await database.get_news(nid))
        self.assertEqual(len(await database.get_audit_entries(action="news_delete")), 1)

    async def test_student_cannot_publish(self):
        nid = await self._make(title="safe", status="draft")
        await self._open(self.student_id, f"news_admin_pub:{nid}", _FakeContext())
        self.assertEqual((await database.get_news(nid))["status"], "draft")

    async def test_admin_panel_lists_news_once_for_owner(self):
        query = _FakeQuery(self.owner_id, "admin")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        callbacks = [
            b.callback_data
            for row in query.last_markup.inline_keyboard
            for b in row
        ]
        self.assertEqual(callbacks.count("admin_news"), 1)

    # ---- H2: the admin list is fully navigable ------------------------

    async def test_admin_list_rows_are_clickable(self):
        draft = await self._make("notify", "draft one", status="draft")
        published = await self._make("notify", "live one")
        query, _ = await self._open(self.owner_id, "admin_news", _FakeContext())
        callbacks = [
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        ]
        self.assertIn(f"news_admin_view:{draft}", callbacks)
        self.assertIn(f"news_admin_view:{published}", callbacks)

    async def test_admin_list_exposes_required_entries(self):
        query, _ = await self._open(self.owner_id, "admin_news", _FakeContext())
        callbacks = [
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        ]
        self.assertIn("news_admin_archived", callbacks)
        self.assertIn("news_admin_published", callbacks)
        self.assertIn("news_new", callbacks)

    async def test_archived_items_are_reachable_and_restorable(self):
        nid = await self._make("notify", "old notice")
        await database.archive_news(nid)

        # The default working set hides it; the archive view reaches it.
        query, _ = await self._open(self.owner_id, "admin_news", _FakeContext())
        callbacks = [
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        ]
        self.assertNotIn(f"news_admin_view:{nid}", callbacks)

        query, _ = await self._open(self.owner_id, "news_admin_archived", _FakeContext())
        callbacks = [
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        ]
        self.assertIn(f"news_admin_view:{nid}", callbacks)

        query, _ = await self._open(self.owner_id, f"news_admin_view:{nid}", _FakeContext())
        callbacks = [
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        ]
        self.assertIn(f"news_admin_restore:{nid}", callbacks)
        self.assertIn("news_admin_archived", callbacks)

    async def test_draft_item_menu_actions(self):
        nid = await self._make("notify", "d", status="draft")
        query, _ = await self._open(self.owner_id, f"news_admin_view:{nid}", _FakeContext())
        callbacks = {
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        }
        self.assertIn(f"news_admin_pub:{nid}", callbacks)
        self.assertIn(f"news_admin_delete:{nid}", callbacks)
        self.assertIn(f"news_admin_preview:{nid}", callbacks)
        self.assertNotIn(f"news_admin_archive:{nid}", callbacks)

    async def test_published_item_menu_actions(self):
        nid = await self._make("notify", "p")
        query, _ = await self._open(self.owner_id, f"news_admin_view:{nid}", _FakeContext())
        callbacks = {
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        }
        self.assertIn(f"news_admin_archive:{nid}", callbacks)
        self.assertIn(f"news_admin_preview:{nid}", callbacks)
        self.assertNotIn(f"news_admin_pub:{nid}", callbacks)
        self.assertNotIn(f"news_admin_restore:{nid}", callbacks)

    async def test_news_admin_all_view_lists_archived_too(self):
        nid = await self._make("notify", "old")
        await database.archive_news(nid)
        query, _ = await self._open(self.owner_id, "news_admin_all", _FakeContext())
        callbacks = [
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        ]
        self.assertIn(f"news_admin_view:{nid}", callbacks)

    async def test_student_cannot_reach_admin_list_or_preview(self):
        nid = await self._make("notify", "SECRET", status="draft")
        for data in (
            "admin_news",
            "news_admin_all",
            "news_admin_archived",
            f"news_admin_view:{nid}",
            f"news_admin_preview:{nid}",
        ):
            query, _ = await self._open(self.student_id, data, _FakeContext())
            self.assertIn("غير مصرح", query.last_text)
            self.assertNotIn("SECRET", query.last_text)


# ---------------------------------------------------------------
# 10. Admin draft wizard (typed input)
# ---------------------------------------------------------------


class NewsWizardTests(NewsBase):
    async def _text(self, user_id, text, ctx):
        msg = _TextMessage(text)
        return await news.handle_news_text(_MediaUpdate(user_id, msg), ctx), msg

    async def test_wizard_creates_a_draft(self):
        ctx = _FakeContext()
        await self._open(self.owner_id, "news_new:notify", ctx)
        self.assertEqual(ctx.user_data.get(news._STATE_TYPE), "notify")

        handled, _ = await self._text(self.owner_id, "Lecture today", ctx)
        self.assertTrue(handled)
        handled, msg = await self._text(self.owner_id, "10:00 in hall 3", ctx)
        self.assertTrue(handled)
        # Phase 2: doctor and event are optional steps (skip both).
        await self._text(self.owner_id, "Dr. Ali", ctx)
        await self._text(self.owner_id, "12:00", ctx)

        drafts = await database.list_news(status="draft")
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["title"], "Lecture today")
        self.assertEqual(drafts[0]["body"], "10:00 in hall 3")
        self.assertEqual(drafts[0]["doctor"], "Dr. Ali")
        self.assertEqual(drafts[0]["event_at"], "12:00")
        self.assertEqual(drafts[0]["status"], "draft")

    async def test_wizard_skip_body(self):
        ctx = _FakeContext()
        await self._open(self.owner_id, "news_new:section", ctx)
        await self._text(self.owner_id, "Micro news", ctx)
        await self._text(self.owner_id, "/skip", ctx)
        await self._text(self.owner_id, "/skip", ctx)
        await self._text(self.owner_id, "/skip", ctx)
        draft = (await database.list_news(status="draft"))[0]
        self.assertIsNone(draft["body"])
        self.assertIsNone(draft["doctor"])
        self.assertIsNone(draft["event_at"])

    async def test_wizard_cancel_creates_nothing(self):
        ctx = _FakeContext()
        await self._open(self.owner_id, "news_new:notify", ctx)
        await self._text(self.owner_id, "/cancel", ctx)
        self.assertEqual(await database.list_news(status="all"), [])

    async def test_wizard_owner_only(self):
        ctx = _FakeContext()
        query, _ = await self._open(self.student_id, "news_new:notify", ctx)
        self.assertIn("غير مصرح", query.last_text)
        self.assertFalse(ctx.user_data)

    async def test_wizard_state_cleared_on_exit(self):
        ctx = _FakeContext()
        await self._open(self.owner_id, "news_new:notify", ctx)
        await self._text(self.owner_id, "Title", ctx)
        await self._text(self.owner_id, "Body", ctx)
        await self._text(self.owner_id, "/skip", ctx)
        await self._text(self.owner_id, "/skip", ctx)
        for key in news._ALL_STATE_KEYS:
            self.assertNotIn(key, ctx.user_data)


class NewsUXTests(NewsBase):
    """Pins the merged News/Admin UX: explicit labels, no colour-only filters,
    no "resource" news type, and the required admin/student entries."""

    async def _callbacks(self, user_id, data, ctx=None):
        query, _ = await self._open(user_id, data, ctx or _FakeContext())
        return [
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        ]

    async def test_feed_filters_use_explicit_labels(self):
        callbacks = await self._callbacks(self.student_id, "news")
        labels = [
            b.text for row in (await self._open(self.student_id, "news"))[0]
            .last_markup.inline_keyboard for b in row
        ]
        # The two real kinds + All, with explicit wording.
        self.assertIn("news_filter:notify", callbacks)
        self.assertIn("news_filter:section", callbacks)
        self.assertIn("news_filter:all", callbacks)
        self.assertNotIn("news_filter:resource", callbacks)
        self.assertTrue(any("هام" in label for label in labels))
        self.assertTrue(any("أخبار الأقسام" in label for label in labels))
        self.assertTrue(any("كل الأخبار" in label for label in labels))

    async def test_publish_form_offers_the_two_types(self):
        callbacks = await self._callbacks(self.owner_id, "news_new")
        self.assertIn("news_new:notify", callbacks)
        self.assertIn("news_new:section", callbacks)
        self.assertNotIn("news_new:resource", callbacks)

    async def test_subscriptions_show_state(self):
        query, _ = await self._open(self.student_id, "news_subs")
        labels = [b.text for row in query.last_markup.inline_keyboard for b in row]
        self.assertTrue(any("غير مشترك" in label for label in labels))
        self.assertIn("news_subs_sections", [
            b.callback_data for row in query.last_markup.inline_keyboard for b in row
        ])
        # After subscribing, the state flips to the subscribed marker.
        await self._open(self.student_id, "news_sub:notify")
        query, _ = await self._open(self.student_id, "news_subs")
        labels = [b.text for row in query.last_markup.inline_keyboard for b in row]
        self.assertTrue(any("مشترك ✓" in label for label in labels))

    async def test_admin_menu_has_three_entries(self):
        callbacks = await self._callbacks(self.owner_id, "admin_news")
        self.assertIn("news_new", callbacks)
        self.assertIn("news_admin_published", callbacks)
        self.assertIn("news_admin_archived", callbacks)

    async def test_linked_resource_is_not_a_separate_type(self):
        # A resource linked to a section item never introduces a third kind.
        nid = await database.create_news(
            "section", "Linked", section_folder_id=self.section,
            resource_id=self.resource,
        )
        row = await database.get_news(nid)
        self.assertEqual(row["news_type"], "section")
        self.assertNotIn("resource", database.NEWS_TYPES)


# ---------------------------------------------------------------
# 11. Future-compatibility seams + non-regression
# ---------------------------------------------------------------


class NewsFutureCompatibilityTests(NewsBase):
    async def test_subscriptions_table_is_reserved_and_unused(self):
        # Nothing writes it on the student path, but the write path exists so
        # Phase 2 needs no schema change.
        self.assertEqual(await database.get_news_subscriptions(self.student_id), [])
        await database.add_news_subscription(self.student_id, "type", "section")
        self.assertEqual(
            await database.get_news_subscriptions(self.student_id), [("type", "section")]
        )

    async def test_delivery_scope_and_source_columns_exist(self):
        nid = await database.create_news(
            "notify", "future", delivery_scope="subscribed", source="resource"
        )
        row = await database.get_news(nid)
        self.assertEqual(row["delivery_scope"], "subscribed")
        self.assertEqual(row["source"], "resource")

    async def test_payload_column_reserved(self):
        nid = await database.create_news("notify", "p", payload='{"k": 1}')
        self.assertEqual((await database.get_news(nid))["payload"], '{"k": 1}')

    async def test_news_never_touches_notifications_broadcast(self):
        # The existing broadcast must keep working unchanged alongside news.
        bot = _RecordingBot()
        await database.register_user(700, "s", "Student")
        recipients, delivered = await notifications.broadcast_notification(
            bot, "old-style broadcast", sender_id=self.owner_id
        )
        self.assertGreaterEqual(recipients, 1)
        self.assertEqual(delivered, recipients)
        self.assertEqual(await database.get_notifications_count(), 1)

    async def test_news_does_not_break_resource_navigation(self):
        query = _FakeQuery(self.student_id, f"file:{self.resource}")
        ctx = _FakeContext()
        ctx.bot = _RecordingBot()
        await main.callback_router(_FakeUpdate(query), ctx)
        # The resource screen path ran without raising; content registry intact.
        self.assertTrue(await database.get_file_record(self.resource))

    async def test_i18n_keys_resolve_in_both_languages(self):
        for key in ("menu_news", "news_title", "news_open", "news_view_resource"):
            for lang in ("ar", "en"):
                value = i18n.t(key, lang)
                self.assertTrue(value and value != key, (key, lang))


if __name__ == "__main__":
    unittest.main()
