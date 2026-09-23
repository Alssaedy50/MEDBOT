"""Isolated system tests for the MEDBOT folder/resource/contribution subsystem.

All tests run against a temporary SQLite database. No real data, tokens,
folders, resources or file_ids are created.
"""

import asyncio
import os
import tempfile
import unittest

import database
import search_engine


class _FakeDoc:
    def __init__(self, file_id, file_name=None):
        self.file_id = file_id
        self.file_name = file_name


class _FakePhoto:
    def __init__(self, file_id):
        self.file_id = file_id


class _FakeAudio:
    def __init__(self, file_id, file_name=None, title=None):
        self.file_id = file_id
        self.file_name = file_name
        self.title = title


class _FakeMessage:
    def __init__(self, **kwargs):
        self.document = kwargs.get("document")
        self.photo = kwargs.get("photo")
        self.audio = kwargs.get("audio")
        self.video = kwargs.get("video")
        self.voice = kwargs.get("voice")


class MedbotSystemTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-test-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")

        self._old_db_name = database.DB_NAME
        self._old_search_db_name = search_engine.DB_NAME

        database.DB_NAME = self.db_path
        search_engine.DB_NAME = self.db_path

        await database.init_db()

    async def asyncTearDown(self):
        database.DB_NAME = self._old_db_name
        search_engine.DB_NAME = self._old_search_db_name

        for name in os.listdir(self.tmp_dir):
            try:
                os.remove(os.path.join(self.tmp_dir, name))
            except OSError:
                pass
        os.rmdir(self.tmp_dir)

    # ---------------- Folder management ----------------

    async def test_create_root_and_child_folder(self):
        await database.add_folder(parent_id=0, name="Root A", node_type="general")
        roots = await database.get_folders(0)
        self.assertEqual(len(roots), 1)
        root_id = roots[0][0]
        self.assertEqual(await database.get_parent_id(root_id), 0)

        await database.add_folder(
            parent_id=root_id,
            name="Child A",
            node_type="books",
            accepts_contributions=1,
        )
        children = await database.get_folders(root_id)
        self.assertEqual(len(children), 1)
        child_id = children[0][0]
        self.assertEqual(await database.get_parent_id(child_id), root_id)

        crumbs = await database.get_breadcrumbs(child_id)
        self.assertIn("Root A", crumbs)
        self.assertIn("Child A", crumbs)

    async def test_rename_folder(self):
        await database.add_folder(0, "Old", "general")
        root_id = (await database.get_folders(0))[0][0]

        ok = await database.update_folder_name(root_id, "New")
        self.assertTrue(ok)
        self.assertEqual((await database.get_folder(root_id))[2], "New")

        missing = await database.update_folder_name(999999, "Nope")
        self.assertFalse(missing)

    async def test_change_folder_type_persists(self):
        await database.add_folder(0, "F", "general")
        fid = (await database.get_folders(0))[0][0]

        ok = await database.update_folder_type(fid, "summaries")
        self.assertTrue(ok)
        self.assertEqual((await database.get_folder(fid))[3], "summaries")

        missing = await database.update_folder_type(999999, "video")
        self.assertFalse(missing)

    async def test_contribution_toggle(self):
        await database.add_folder(0, "F", "general")
        fid = (await database.get_folders(0))[0][0]

        self.assertFalse(await database.folder_accepts_contributions(fid))
        await database.update_folder_accepts_contributions(fid, 1)
        self.assertTrue(await database.folder_accepts_contributions(fid))
        await database.update_folder_accepts_contributions(fid, 0)
        self.assertFalse(await database.folder_accepts_contributions(fid))

    async def test_move_folder_valid_and_invalid(self):
        await database.add_folder(0, "Root", "general")
        await database.add_folder(0, "Target", "general")
        roots = {f[1]: f[0] for f in await database.get_folders(0)}
        root_id = roots["Root"]
        target_id = roots["Target"]

        await database.add_folder(root_id, "Child", "general")
        child_id = (await database.get_folders(root_id))[0][0]

        # Self move
        ok, msg = await database.move_folder(root_id, root_id)
        self.assertFalse(ok)

        # Move into own descendant
        ok, msg = await database.move_folder(root_id, child_id)
        self.assertFalse(ok)

        # Invalid target
        ok, msg = await database.move_folder(root_id, 999999)
        self.assertFalse(ok)

        # Valid move preserves the child
        ok, msg = await database.move_folder(child_id, target_id)
        self.assertTrue(ok)
        self.assertEqual(await database.get_parent_id(child_id), target_id)
        self.assertEqual(len(await database.get_folders(target_id)), 1)

        # Move to root
        ok, msg = await database.move_folder(child_id, 0)
        self.assertTrue(ok)
        self.assertEqual(await database.get_parent_id(child_id), 0)

    async def test_delete_folder_safety(self):
        await database.add_folder(0, "Parent", "general")
        pid = (await database.get_folders(0))[0][0]
        await database.add_folder(pid, "Child", "general")

        # Non-empty (child) must be refused.
        self.assertFalse(await database.delete_folder(pid))

        cid = (await database.get_folders(pid))[0][0]
        # Empty child deletes.
        self.assertTrue(await database.delete_folder(cid))
        # Now parent is empty and deletes.
        self.assertTrue(await database.delete_folder(pid))
        # Non-existent refuses safely.
        self.assertFalse(await database.delete_folder(pid))

        # Folder with resources must be refused.
        await database.add_folder(0, "WithResource", "general")
        rid = (await database.get_folders(0))[0][0]
        await database.add_content(rid, "T", "file-id", "document")
        self.assertFalse(await database.delete_folder(rid))

    async def test_delete_folder_with_contribution_refused(self):
        await database.add_folder(0, "Contrib", "general", 1)
        fid = (await database.get_folders(0))[0][0]
        await database.add_contribution(
            5, "Student", fid, "T", "file-id", "document"
        )
        # Must not cascade away the student contribution.
        self.assertFalse(await database.delete_folder(fid))
        self.assertIsNotNone(await database.get_folder(fid))

    # ---------------- Resource management ----------------

    async def test_resource_lifecycle(self):
        await database.add_folder(0, "Root", "general")
        await database.add_folder(0, "Other", "general")
        roots = {f[1]: f[0] for f in await database.get_folders(0)}
        rid, oid = roots["Root"], roots["Other"]

        for ftype in ("document", "photo", "audio", "video"):
            await database.add_content(
                rid,
                f"Title {ftype}",
                f"telegram-file-id-{ftype}",
                ftype,
                created_by=42,
            )

        files = await database.get_files(rid)
        self.assertEqual(len(files), 4)

        first_id = files[0][0]
        record = await database.get_file_record(first_id)
        self.assertEqual(len(record), 8)
        self.assertEqual(record[1], rid)

        self.assertTrue(await database.update_file_title(first_id, "Renamed"))
        self.assertEqual(
            (await database.get_file_record(first_id))[2],
            "Renamed",
        )

        self.assertTrue(await database.update_content_type(first_id, "audio"))
        self.assertEqual(
            (await database.get_file_record(first_id))[4],
            "audio",
        )

        ok, _ = await database.move_content(first_id, oid)
        self.assertTrue(ok)
        self.assertEqual((await database.get_file_record(first_id))[1], oid)

        ok, _ = await database.move_content(first_id, 999999)
        self.assertFalse(ok)

        self.assertTrue(await database.delete_file(first_id))
        self.assertIsNone(await database.get_file_record(first_id))
        self.assertFalse(await database.delete_file(first_id))

    async def test_resource_invalid_ids(self):
        self.assertIsNone(await database.get_file_record(999999))
        self.assertFalse(await database.update_file_title(999999, "x"))
        self.assertFalse(await database.update_content_type(999999, "video"))
        ok, _ = await database.move_content(999999, 1)
        self.assertFalse(ok)

    # ---------------- Contributions ----------------

    async def test_contribution_lifecycle(self):
        await database.add_folder(0, "Contribs", "general", 1)
        fid = (await database.get_folders(0))[0][0]

        cid = await database.add_contribution(
            7, "Student", fid, "Thesis", "file-id-1", "document"
        )
        self.assertTrue(cid)

        pending = await database.get_pending_contributions_list()
        self.assertEqual(len(pending), 1)

        result = await database.approve_contribution(cid)
        self.assertIsNotNone(result)

        files = await database.get_files(fid)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0][1], "Thesis")

        # Duplicate approval blocked.
        self.assertIsNone(await database.approve_contribution(cid))
        # Rejecting an approved item blocked.
        self.assertIsNone(await database.reject_contribution(cid))
        # No duplicate publication.
        self.assertEqual(len(await database.get_files(fid)), 1)

    async def test_contribution_reject_does_not_publish(self):
        await database.add_folder(0, "Contribs", "general", 1)
        fid = (await database.get_folders(0))[0][0]

        cid = await database.add_contribution(
            8, "Student", fid, "Bad", "file-id-2", "document"
        )
        result = await database.reject_contribution(cid)
        self.assertIsNotNone(result)
        self.assertEqual(len(await database.get_files(fid)), 0)

        # Duplicate reject blocked.
        self.assertIsNone(await database.reject_contribution(cid))
        # Approving a rejected item blocked.
        self.assertIsNone(await database.approve_contribution(cid))

    async def test_contribution_invalid_ids(self):
        self.assertIsNone(await database.approve_contribution(999999))
        self.assertIsNone(await database.reject_contribution(999999))

    # ---------------- Security ----------------

    async def test_admin_authorization(self):
        self.assertFalse(await database.is_user_admin(12345))
        await database.add_sub_admin(12345, "admin")
        self.assertTrue(await database.is_user_admin(12345))
        await database.remove_sub_admin(12345)
        self.assertFalse(await database.is_user_admin(12345))

    # ---------------- Pure helpers (no Telegram network) ----------------

    async def test_extract_media_info(self):
        import main

        doc = _FakeMessage(document=_FakeDoc("fid-doc", "notes.pdf"))
        self.assertEqual(main._extract_media_info(doc), ("fid-doc", "document", "notes.pdf"))

        photo = _FakeMessage(photo=[_FakePhoto("small"), _FakePhoto("big")])
        self.assertEqual(main._extract_media_info(photo), ("big", "photo", "Photo"))

        audio = _FakeMessage(audio=_FakeAudio("fid-audio", "lecture.mp3"))
        self.assertEqual(main._extract_media_info(audio), ("fid-audio", "audio", "lecture.mp3"))

        video = _FakeMessage(video=_FakeDoc("fid-video", "clip.mp4"))
        self.assertEqual(main._extract_media_info(video), ("fid-video", "video", "clip.mp4"))

        voice = _FakeMessage(voice=_FakeDoc("fid-voice"))
        self.assertEqual(main._extract_media_info(voice), ("fid-voice", "audio", "Voice Note"))

        empty = _FakeMessage()
        self.assertEqual(main._extract_media_info(empty), (None, None, None))

    async def test_admin_state_cleanup(self):
        import main

        class _Ctx:
            def __init__(self):
                self.user_data = {}

        ctx = _Ctx()
        ctx.user_data.update(
            {
                "admin_upload": True,
                "admin_upload_folder": 3,
                "admin_upload_preview": {"file_id": "x"},
                "admin_file_rename": True,
                "admin_file_rename_id": 5,
                "admin_file_move_id": 6,
                "admin_folder_move_id": 7,
                "unrelated": "keep",
            }
        )

        main._clear_admin_state(ctx)

        for key in (
            "admin_upload",
            "admin_upload_folder",
            "admin_upload_preview",
            "admin_file_rename",
            "admin_file_rename_id",
            "admin_file_move_id",
            "admin_folder_move_id",
        ):
            self.assertNotIn(key, ctx.user_data)

        self.assertEqual(ctx.user_data.get("unrelated"), "keep")

    async def test_search_finds_published_content(self):
        await database.add_folder(0, "Anatomy", "general")
        fid = (await database.get_folders(0))[0][0]
        await database.add_content(fid, "Heart Lecture", "file-x", "document")

        summary = await search_engine.search_library_summary("Heart")
        self.assertIsInstance(summary, dict)
        self.assertGreaterEqual(summary["result_count"], 1)


# ============================================================
# Schema migration safety — existing data must survive
# ============================================================


class MigrationSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-migrate-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        database.DB_NAME = self.db_path

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        for name in os.listdir(self.tmp_dir):
            try:
                os.remove(os.path.join(self.tmp_dir, name))
            except OSError:
                pass
        os.rmdir(self.tmp_dir)

    async def test_migration_is_idempotent_and_preserves_data(self):
        await database.init_db()

        await database.add_folder(0, "Cardio", "general")
        folder_id = (await database.get_folders(0))[0][0]
        await database.add_content(folder_id, "Existing Doc", "fid-keep", "document")

        # Re-running migrations (e.g. on a later boot) must not fail or drop data.
        await database.init_db()
        await database.init_db()

        folders = await database.get_folders(0)
        self.assertEqual(len(folders), 1)
        self.assertEqual(folders[0][1], "Cardio")

        files = await database.get_files(folder_id)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0][1], "Existing Doc")

    async def test_new_tables_exist_after_migration(self):
        await database.init_db()

        db = await database.get_db()
        try:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) as cur:
                tables = {row[0] for row in await cur.fetchall()}
        finally:
            await db.close()

        self.assertIn("mcq_questions", tables)
        self.assertIn("users", tables)
        self.assertIn("content", tables)

    async def test_notifications_enabled_defaults_to_true(self):
        await database.init_db()
        await database.register_user(4242, "u", "User")

        # A freshly registered user is opted in by default.
        self.assertTrue(await database.notifications_enabled(4242))

    async def test_unknown_mcq_folder_still_stored_as_null(self):
        await database.init_db()

        # No folder chosen: the question belongs to the general bank.
        qid = await database.add_mcq_question(
            None, "Loose question?", ["a", "b"], 0, None
        )
        self.assertIsNotNone(qid)

        questions = await database.get_mcq_questions()
        self.assertEqual(len(questions), 1)


if __name__ == "__main__":
    unittest.main()
