"""Regression tests for the MEDBOT Emergency Resource Archive.

Everything runs against a temporary SQLite database (never the real one) and
exercises real code paths — no mocks of the database or of business logic. The
only stub is a fake Telegram bot, because the archive's whole job is to talk to
Telegram.

Covered (mirroring the feature requirements):
  1.  A newly registered resource is published automatically.
  2.  A restart / re-publish does not create a duplicate.
  3.  A Telegram publication failure never fails resource creation.
  4.  Retry publishes a previously failed resource exactly once.
  5.  Resync mirrors resources registered before the archive existed.
  6.  The post reflects MEDBOT's real path and hierarchy order.
  7.  No section, path or resource that is absent from the registry is invented.
  8.  The channel is configured only by environment (never hardcoded).
  9.  Deleting a resource in MEDBOT never deletes the archived copy.
  10. Admin surface is gated by `can_archive`.
"""

import os
import tempfile
import unittest

import archive
import database
import main
from test_medbot_router import _FakeContext, _FakeQuery, _FakeUpdate


class _FakeSent:
    def __init__(self, message_id):
        self.message_id = message_id


class _RecordingBot:
    """Minimal Telegram bot stub that records every archive send.

    `fail_types` makes chosen send_* calls raise, so the failure path can be
    exercised without any network access.
    """

    def __init__(self, fail_types=None):
        self.sent = []
        self.fail_types = set(fail_types or [])
        self._next = 1000

    def _record(self, kind, chat_id, payload):
        if kind in self.fail_types:
            raise RuntimeError(f"simulated Telegram failure: {kind}")
        self._next += 1
        self.sent.append(
            {
                "kind": kind,
                "chat_id": chat_id,
                "payload": payload,
                "message_id": self._next,
            }
        )
        return _FakeSent(self._next)

    async def send_document(self, chat_id=None, document=None, caption=None, parse_mode=None):
        return self._record("document", chat_id, {"file_id": document, "caption": caption})

    async def send_photo(self, chat_id=None, photo=None, caption=None, parse_mode=None):
        return self._record("photo", chat_id, {"file_id": photo, "caption": caption})

    async def send_audio(self, chat_id=None, audio=None, caption=None, parse_mode=None):
        return self._record("audio", chat_id, {"file_id": audio, "caption": caption})

    async def send_video(self, chat_id=None, video=None, caption=None, parse_mode=None):
        return self._record("video", chat_id, {"file_id": video, "caption": caption})

    async def send_message(self, chat_id=None, text=None, parse_mode=None):
        return self._record("text", chat_id, {"text": text})

    def kinds(self):
        return [entry["kind"] for entry in self.sent]

    def captions(self):
        return [
            entry["payload"].get("caption") or entry["payload"].get("text")
            for entry in self.sent
        ]


class ArchiveBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-archive-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old_db = database.DB_NAME
        database.DB_NAME = self.db_path
        await database.init_db()

        # Config is environment-only; provide a fake channel for the test.
        self._env_backup = {
            name: os.environ.get(name)
            for name in archive.ARCHIVE_CHANNEL_ENV_VARS
        }
        os.environ["MEDBOT_ARCHIVE_CHANNEL"] = "@medbot_archive_test"
        for name in archive.ARCHIVE_CHANNEL_ENV_VARS:
            if name != "MEDBOT_ARCHIVE_CHANNEL":
                os.environ.pop(name, None)

        archive._locks.clear()

        self.bot = _RecordingBot()

        # A small but real hierarchy: Root -> Block -> Subject -> نظري.
        self.root = await database.add_folder(0, "Second Year", "general")
        self.block = await database.add_folder(self.root, "Semester 7", "general")
        self.subject = await database.add_folder(self.block, "PHYSIOLOGY", "general")
        self.theory = await database.add_folder(self.subject, "نظري", "general")

        await database.add_sub_admin(500, "owner")
        await database.ensure_configured_admin(500)

    async def asyncTearDown(self):
        for name, value in self._env_backup.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        database.DB_NAME = self._old_db
        for name in os.listdir(self.tmp_dir):
            try:
                os.remove(os.path.join(self.tmp_dir, name))
            except OSError:
                pass
        os.rmdir(self.tmp_dir)


# ---------------------------------------------------------------
# 1. Automatic publication on resource creation
# ---------------------------------------------------------------


class AutoPublishTests(ArchiveBase):
    async def test_new_resource_publishes_automatically(self):
        cid = await database.add_content(
            self.theory, "L1 Cardiac Cycle", "fid-cardiac", "document"
        )
        outcome = await archive.publish_resource(self.bot, cid, with_header=True)
        self.assertEqual(outcome["status"], "published")
        self.assertEqual(self.bot.kinds(), ["text", "document"])
        self.assertEqual(self.bot.sent[0]["chat_id"], "@medbot_archive_test")

    async def test_published_row_records_full_sync_metadata(self):
        cid = await database.add_content(
            self.theory, "L2 ECG", "fid-ecg", "document"
        )
        outcome = await archive.publish_resource(self.bot, cid)
        row = await database.get_archive_sync(outcome["fingerprint"])
        # id, object_type, fingerprint, folder_id, content_ids, channel_id,
        # channel_message_id, status, attempts, ...
        self.assertEqual(row[3], self.theory)
        self.assertEqual(row[4], str(cid))
        self.assertEqual(row[5], "@medbot_archive_test")
        self.assertIsNotNone(row[6])
        self.assertEqual(row[7], "published")
        self.assertGreaterEqual(row[8], 1)

    async def test_non_file_resource_is_posted_as_text_when_needed(self):
        # An audio resource must go through send_audio, not send_document.
        cid = await database.add_content(
            self.theory, "L3 Recording", "fid-audio", "audio"
        )
        await archive.publish_resource(self.bot, cid)
        self.assertIn("audio", self.bot.kinds())

    async def test_main_upload_path_publishes(self):
        """End-to-end through the real admin upload handler."""
        context = _FakeContext()
        context.bot = self.bot

        # Seed the upload preview exactly as admin_upload_media_handler would.
        context.user_data["admin_upload_preview"] = {
            "file_id": "fid-direct",
            "file_type": "document",
            "folder_id": self.theory,
            "title": "Direct Upload",
        }

        query = _FakeQuery(500, "admin_upload_confirm")
        await main._register_admin_upload(query, context, context.user_data["admin_upload_preview"])

        files = await database.get_files(self.theory)
        self.assertEqual(len(files), 1)
        self.assertEqual(self.bot.kinds(), ["text", "document"])


# ---------------------------------------------------------------
# 2. Restart / retry safety (no duplicates)
# ---------------------------------------------------------------


class NoDuplicateTests(ArchiveBase):
    async def test_republish_after_restart_is_a_noop(self):
        cid = await database.add_content(
            self.theory, "L4 Renal", "fid-renal", "document"
        )
        await archive.publish_resource(self.bot, cid)
        first_count = len(self.bot.sent)

        # Simulate a restart: fresh lock registry, same database.
        archive._locks.clear()
        second_bot = _RecordingBot()
        outcome = await archive.publish_resource(second_bot, cid)

        self.assertEqual(outcome["status"], "published")
        self.assertEqual(len(second_bot.sent), 0)
        self.assertEqual(len(self.bot.sent), first_count)

    async def test_resync_does_not_duplicate_published(self):
        cid = await database.add_content(
            self.theory, "L5 Acid Base", "fid-acid", "document"
        )
        await archive.publish_resource(self.bot, cid)
        before = len(self.bot.sent)

        stats = await archive.resync(self.bot)
        self.assertEqual(stats["published"], 0)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(len(self.bot.sent), before)

    async def test_same_file_in_two_folders_maps_to_one_post(self):
        other = await database.add_folder(self.subject, "عملي", "general")
        cid_a = await database.add_content(
            self.theory, "Shared File", "fid-shared", "document"
        )
        cid_b = await database.add_content(
            other, "Shared File", "fid-shared", "document"
        )
        await archive.publish_resource(self.bot, cid_a)
        outcome = await archive.publish_resource(self.bot, cid_b)
        self.assertEqual(outcome["status"], "published")
        # Only the first publish produced a document send.
        self.assertEqual(self.bot.kinds().count("document"), 1)

    async def test_concurrent_publish_posts_once(self):
        import asyncio

        cid = await database.add_content(
            self.theory, "Concurrent", "fid-conc", "document"
        )
        results = await asyncio.gather(
            archive.publish_resource(self.bot, cid),
            archive.publish_resource(self.bot, cid),
            archive.publish_resource(self.bot, cid),
        )
        self.assertEqual(self.bot.kinds().count("document"), 1)
        self.assertTrue(all(r["status"] == "published" for r in results))


# ---------------------------------------------------------------
# 3. Publication failure never fails the resource
# ---------------------------------------------------------------


class FailureIsolationTests(ArchiveBase):
    async def test_telegram_failure_still_creates_resource(self):
        failing = _RecordingBot(fail_types={"document"})
        context = _FakeContext()
        context.bot = failing

        preview = {
            "file_id": "fid-fail",
            "file_type": "document",
            "folder_id": self.theory,
            "title": "Failing Upload",
        }
        context.user_data["admin_upload_preview"] = preview

        query = _FakeQuery(500, "admin_upload_confirm")
        await main._register_admin_upload(query, context, preview)

        # The MEDBOT resource exists regardless of the archive failure.
        files = await database.get_files(self.theory)
        self.assertEqual(len(files), 1)

        fingerprint = archive.content_fingerprint(
            await database.get_resource_snapshot(files[0][0])
        )
        row = await database.get_archive_sync(fingerprint)
        self.assertEqual(row[7], "failed")
        self.assertIsNotNone(row[9])

    async def test_publish_never_raises_on_bad_bot(self):
        class _ExplodingBot:
            async def send_document(self, **kwargs):
                raise RuntimeError("boom")

        cid = await database.add_content(
            self.theory, "Explode", "fid-explode", "document"
        )
        outcome = await archive.publish_resource(_ExplodingBot(), cid)
        self.assertEqual(outcome["status"], "failed")
        # Still a real MEDBOT resource.
        self.assertIsNotNone(await database.get_file_record(cid))

    async def test_publish_of_missing_resource_is_skipped(self):
        outcome = await archive.publish_resource(self.bot, 999999)
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(self.bot.sent, [])


# ---------------------------------------------------------------
# 4. Retry publishes exactly once
# ---------------------------------------------------------------


class RetryTests(ArchiveBase):
    async def test_retry_publishes_failed_once(self):
        cid = await database.add_content(
            self.theory, "Retry Me", "fid-retry", "document"
        )
        failing = _RecordingBot(fail_types={"document"})
        await archive.publish_resource(failing, cid)

        fingerprint = archive.content_fingerprint(
            await database.get_resource_snapshot(cid)
        )
        self.assertEqual(
            (await database.get_archive_sync(fingerprint))[7], "failed"
        )

        stats = await archive.retry_failed(self.bot)
        self.assertEqual(stats["published"], 1)
        self.assertEqual(self.bot.kinds().count("document"), 1)

        # A second retry does nothing: the row is published now.
        stats2 = await archive.retry_failed(self.bot)
        self.assertEqual(stats2["total"], 0)
        self.assertEqual(self.bot.kinds().count("document"), 1)

    async def test_retry_skips_unrecoverable_rows(self):
        # A failed folder header has no content id, so it is skipped, not faked.
        await database.upsert_archive_pending(
            archive.folder_fingerprint(self.theory),
            folder_id=self.theory,
            content_id=None,
            object_type="folder",
        )
        await database.mark_archive_failed(
            archive.folder_fingerprint(self.theory), "boom"
        )
        stats = await archive.retry_failed(self.bot)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(self.bot.sent, [])


# ---------------------------------------------------------------
# 5. Resync for pre-existing resources
# ---------------------------------------------------------------


class ResyncTests(ArchiveBase):
    async def test_resync_publishes_legacy_resources(self):
        cid_a = await database.add_content(
            self.theory, "Legacy A", "fid-la", "document"
        )
        cid_b = await database.add_content(
            self.theory, "Legacy B", "fid-lb", "document"
        )

        stats = await archive.resync(self.bot)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["published"], 2)
        # One folder header + two resources.
        self.assertEqual(self.bot.kinds().count("text"), 1)
        self.assertEqual(self.bot.kinds().count("document"), 2)

        for cid in (cid_a, cid_b):
            fingerprint = archive.content_fingerprint(
                await database.get_resource_snapshot(cid)
            )
            self.assertEqual(
                (await database.get_archive_sync(fingerprint))[7], "published"
            )

    async def test_resync_is_idempotent(self):
        await database.add_content(self.theory, "Once", "fid-once", "document")
        await archive.resync(self.bot)
        first = len(self.bot.sent)
        stats = await archive.resync(self.bot)
        self.assertEqual(stats["published"], 0)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(len(self.bot.sent), first)

    async def test_resync_only_once_per_folder_header(self):
        await database.add_content(self.theory, "A", "fid-a", "document")
        await database.add_content(self.theory, "B", "fid-b", "document")
        await database.add_content(self.subject, "C", "fid-c", "document")
        await archive.resync(self.bot)
        # Two distinct folders touched -> exactly two headers.
        self.assertEqual(self.bot.kinds().count("text"), 2)


# ---------------------------------------------------------------
# 6/7. Real path and hierarchy order; nothing invented
# ---------------------------------------------------------------


class PathAndOrderTests(ArchiveBase):
    async def test_caption_contains_real_registered_path(self):
        cid = await database.add_content(
            self.theory, "Cardiac Physiology", "fid-path", "document"
        )
        await archive.publish_resource(self.bot, cid)
        caption = self.bot.captions()[-1]
        self.assertIn("Cardiac Physiology", caption)
        # Every real ancestor is present.
        for part in ("Second Year", "Semester 7", "PHYSIOLOGY", "نظري"):
            self.assertIn(part, caption)

    async def test_published_order_follows_registry_folder_order(self):
        # Resync emits resources ordered by the registry's own folder id, so a
        # resource added in an earlier-registered folder is mirrored first.
        # (`self.theory` was created in setUp, so its id precedes `early`'s.)
        early = await database.add_folder(0, "First Year", "general")
        await database.add_content(early, "FirstRes", "fid-first", "document")
        await database.add_content(self.theory, "LaterRes", "fid-later", "document")

        await archive.resync(self.bot)

        joined = "\n".join(str(c) for c in self.bot.captions())
        self.assertLess(joined.index("LaterRes"), joined.index("FirstRes"))

    async def test_folder_header_precedes_its_resources(self):
        await database.add_content(self.theory, "Child Resource", "fid-child", "document")
        await archive.resync(self.bot)
        kinds = self.bot.kinds()
        self.assertEqual(kinds, ["text", "document"])

    async def test_no_invented_section_in_caption(self):
        cid = await database.add_content(
            self.theory, "Only Real", "fid-real", "document"
        )
        await archive.publish_resource(self.bot, cid)
        caption = self.bot.captions()[-1]
        # A section that does not exist in the registry must never appear.
        self.assertNotIn("First Year", caption)
        self.assertNotIn("Semester 1", caption)

    async def test_folder_header_uses_registered_name_and_path(self):
        await archive.publish_folder_header(
            self.bot,
            await database.get_folder(self.subject),
            "الرئيسية 🏠 ⬅️ Second Year ⬅️ Semester 7 ⬅️ PHYSIOLOGY",
        )
        text = self.bot.captions()[-1]
        self.assertIn("PHYSIOLOGY", text)
        self.assertIn("Second Year", text)

    async def test_folder_header_posted_once(self):
        folder = await database.get_folder(self.subject)
        path = "الرئيسية 🏠 ⬅️ Second Year ⬅️ Semester 7 ⬅️ PHYSIOLOGY"
        await archive.publish_folder_header(self.bot, folder, path)
        await archive.publish_folder_header(self.bot, folder, path)
        self.assertEqual(self.bot.kinds().count("text"), 1)


# ---------------------------------------------------------------
# 8. Environment-only configuration
# ---------------------------------------------------------------


class ConfigTests(ArchiveBase):
    async def test_no_hardcoded_channel(self):
        # The channel resolves strictly from the environment.
        self.assertEqual(archive.resolve_channel(), "@medbot_archive_test")

    async def test_unset_channel_disables_feature(self):
        for name in archive.ARCHIVE_CHANNEL_ENV_VARS:
            os.environ.pop(name, None)
        self.assertFalse(archive.is_configured())

        cid = await database.add_content(
            self.theory, "Disabled", "fid-disabled", "document"
        )
        outcome = await archive.publish_resource(self.bot, cid)
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(self.bot.sent, [])

    async def test_numeric_channel_is_sent_as_int(self):
        os.environ["MEDBOT_ARCHIVE_CHANNEL"] = "-1001234567890"
        self.assertEqual(archive._channel_for_send(), -1001234567890)

    async def test_username_channel_is_sent_as_string(self):
        os.environ["MEDBOT_ARCHIVE_CHANNEL"] = "@some_channel"
        self.assertEqual(archive._channel_for_send(), "@some_channel")

    async def test_legacy_env_var_alias(self):
        os.environ.pop("MEDBOT_ARCHIVE_CHANNEL", None)
        os.environ["ARCHIVE_CHANNEL_ID"] = "@alias_channel"
        self.assertEqual(archive.resolve_channel(), "@alias_channel")


# ---------------------------------------------------------------
# 9. Deletion never propagates to the archive
# ---------------------------------------------------------------


class DeletionIsolationTests(ArchiveBase):
    async def test_deleting_resource_keeps_archive_row(self):
        cid = await database.add_content(
            self.theory, "Disaster Recovery", "fid-dr", "document"
        )
        outcome = await archive.publish_resource(self.bot, cid)
        fingerprint = outcome["fingerprint"]

        await database.delete_file(cid)

        # MEDBOT row is gone; the archive mirror record survives.
        self.assertIsNone(await database.get_file_record(cid))
        row = await database.get_archive_sync(fingerprint)
        self.assertIsNotNone(row)
        self.assertEqual(row[7], "published")
        self.assertIsNotNone(row[6])


# ---------------------------------------------------------------
# 10. Admin surface gating + status
# ---------------------------------------------------------------


class AdminSurfaceTests(ArchiveBase):
    async def test_owner_sees_archive_entry_once(self):
        query = _FakeQuery(500, "admin")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        callbacks = [
            b.callback_data
            for row in query.last_markup.inline_keyboard
            for b in row
        ]
        self.assertEqual(callbacks.count("admin_archive"), 1)

    async def test_archive_surface_hidden_without_permission(self):
        await database.add_sub_admin(600, "admin")
        await database.apply_role_preset(600, "admin")
        perms = dict(await database.get_admin_permissions(600))
        perms["can_archive"] = False
        await database.update_admin_permissions(600, perms)

        query = _FakeQuery(600, "admin")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        callbacks = [
            b.callback_data
            for row in query.last_markup.inline_keyboard
            for b in row
        ]
        self.assertNotIn("admin_archive", callbacks)

    async def test_unauthorized_user_gets_denied(self):
        # A plain student is not an admin at all -> no capability.
        query = _FakeQuery(700, "admin_archive")
        await archive.show_archive(query)
        self.assertIn("غير مصرح", query.last_text or "")

    async def test_admin_without_capability_cannot_resync(self):
        await database.add_sub_admin(701, "reviewer")
        # Reviewer preset has no can_archive; permissions must be set too.
        await database.apply_role_preset(701, "reviewer")
        query = _FakeQuery(701, "archive_resync")
        await archive.run_resync(query)
        self.assertIn("غير مصرح", query.last_text or "")

    async def test_status_screen_lists_rows(self):
        cid = await database.add_content(
            self.theory, "Status Row", "fid-status", "document"
        )
        await archive.publish_resource(self.bot, cid)
        query = _FakeQuery(500, "archive_status")
        await archive.show_status(query)
        self.assertIn("published", query.last_text or "")

    async def test_counts_reflect_publications(self):
        cid = await database.add_content(
            self.theory, "Counted", "fid-count", "document"
        )
        await archive.publish_resource(self.bot, cid)
        counts = await database.get_archive_sync_counts()
        self.assertEqual(counts["published"], 1)
        self.assertEqual(counts["total"], 1)


# ---------------------------------------------------------------
# Schema / permission invariants
# ---------------------------------------------------------------


class SchemaTests(ArchiveBase):
    async def test_archive_permission_key_registered(self):
        self.assertIn("can_archive", database.PERMISSION_KEYS)
        self.assertIn("can_archive", database.PERMISSION_LABELS)
        self.assertIn("can_archive", database.PERMISSION_SHORT_LABELS)

    async def test_owner_preset_grants_archive(self):
        self.assertTrue(
            database.ROLE_PERMISSION_PRESETS["owner"]["can_archive"]
        )
        self.assertTrue(
            database.ROLE_PERMISSION_PRESETS["admin"]["can_archive"]
        )
        self.assertFalse(
            database.ROLE_PERMISSION_PRESETS["reviewer"]["can_archive"]
        )

    async def test_migration_is_idempotent(self):
        # Re-running init_db on the same file must not error or duplicate.
        await database.init_db()
        counts = await database.get_archive_sync_counts()
        self.assertEqual(counts["total"], 0)


if __name__ == "__main__":
    unittest.main()
