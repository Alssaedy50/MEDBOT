"""Performance and folder/resource-management regression tests for MEDBOT.

Covers:
  - DB connection pragmas (WAL / busy timeout) that reduce lock contention.
  - Single-connection folder view used by the library screen.
  - add_folder returning the new id (truthy, and usable for navigation).
  - Admin panel navigation into real sub-folders.
  - Newly created sections being immediately usable for resource upload and
    student contributions.

All tests use a temporary SQLite database; no mocks, no network.
"""

import os
import tempfile
import unittest

import database
import search_engine
from test_medbot_router import _FakeContext, _FakeQuery, _FakeUpdate


class PerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-perf-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        self._old_search = search_engine.DB_NAME
        database.DB_NAME = self.db_path
        search_engine.DB_NAME = self.db_path
        await database.init_db()

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        search_engine.DB_NAME = self._old_search
        import shutil

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    async def test_connection_pragmas_applied(self):
        db = await database.get_db()
        try:
            async with db.execute("PRAGMA journal_mode") as cur:
                row = await cur.fetchone()
            self.assertEqual(str(row[0]).lower(), "wal")

            async with db.execute("PRAGMA busy_timeout") as cur:
                row = await cur.fetchone()
            self.assertGreaterEqual(int(row[0]), 1000)
        finally:
            await db.close()

    async def test_get_folder_view_single_connection(self):
        root = await database.add_folder(0, "Root", "general")
        child = await database.add_folder(root, "Child", "general")
        await database.add_content(root, "Doc", "fid", "document")

        folder, children, files, parent_id, breadcrumb = (
            await database.get_folder_view(root)
        )

        self.assertIsNotNone(folder)
        self.assertEqual(folder[2], "Root")
        self.assertEqual([c[0] for c in children], [child])
        self.assertEqual(len(files), 1)
        self.assertEqual(parent_id, 0)
        self.assertIn("Root", breadcrumb)

    async def test_searchable_records_and_breadcrumbs(self):
        root = await database.add_folder(0, "Root", "general")
        await database.add_content(root, "Doc", "fid", "document")

        folders, contents, paths = await database.get_searchable_records()

        self.assertEqual(len(folders), 1)
        self.assertEqual(len(contents), 1)
        self.assertIn(root, paths)


class FolderManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-folders-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        self._old_search = search_engine.DB_NAME
        database.DB_NAME = self.db_path
        search_engine.DB_NAME = self.db_path
        await database.init_db()
        await database.add_sub_admin(700, "admin")

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        search_engine.DB_NAME = self._old_search
        import shutil

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    async def test_add_folder_returns_new_id(self):
        folder_id = await database.add_folder(0, "New Section", "general")
        self.assertIsInstance(folder_id, int)
        self.assertGreater(folder_id, 0)

        folder = await database.get_folder(folder_id)
        self.assertIsNotNone(folder)
        self.assertEqual(folder[2], "New Section")

    async def test_created_section_appears_in_upload_targets(self):
        # A newly created section must be a valid resource-upload target.
        folder_id = await database.add_folder(0, "Uploads", "general")
        self.assertIsNotNone(await database.get_folder(folder_id))

        content_id = await database.add_content(
            folder_id, "Resource", "fid-x", "document"
        )
        files = await database.get_files(folder_id)
        self.assertEqual([f[0] for f in files], [content_id])

    async def test_created_section_appears_in_contribution_path(self):
        folder_id = await database.add_folder(0, "Contribs", "general", 1)

        # Contribution flow only lists folders with accepts_contributions = 1.
        import main

        folders = await main.contribution_folders()
        self.assertIn(folder_id, [f[0] for f in folders])

    async def test_admin_folder_menu_navigates_into_children(self):
        import main

        root = await database.add_folder(0, "Root", "general")
        child = await database.add_folder(root, "Branch A", "general")

        query = _FakeQuery(700, f"admin_folder:{root}")
        await main.callback_router(_FakeUpdate(query), _FakeContext())

        # The management screen for the parent must offer its real child.
        callback_data = [
            b.callback_data
            for row in query.last_markup.inline_keyboard
            for b in row
        ]
        self.assertIn(f"admin_folder:{child}", callback_data)
        self.assertIn(f"admin_upload:{root}", callback_data)

    async def test_new_section_is_immediately_manageable(self):
        import main

        ctx = _FakeContext()
        # Drive the creation flow end to end through the router.
        await main.callback_router(
            _FakeUpdate(_FakeQuery(700, "admin_folder_create")), ctx
        )
        self.assertTrue(ctx.user_data.get("admin_folder_create"))

        await main.request_admin_folder_name(
            _TextUpdate(700, _TextMessage("Cardiology")),
            ctx,
        )
        self.assertEqual(ctx.user_data.get("admin_folder_name"), "Cardiology")

        await main.callback_router(
            _FakeUpdate(_FakeQuery(700, "admin_folder_type:general")), ctx
        )

        confirm = _FakeQuery(700, "admin_folder_accepts:1")
        await main.callback_router(_FakeUpdate(confirm), ctx)

        folders = await database.get_folders(0)
        self.assertEqual([f[1] for f in folders], ["Cardiology"])

        new_id = folders[0][0]

        # The confirmation screen must link straight to the new section's
        # upload and sub-section actions.
        callback_data = [
            b.callback_data
            for row in confirm.last_markup.inline_keyboard
            for b in row
        ]
        self.assertIn(f"admin_upload:{new_id}", callback_data)
        self.assertIn(f"admin_folder:{new_id}", callback_data)


class _TextMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, parse_mode=None, reply_markup=None):
        self.replies.append(text)
        self.last_markup = reply_markup


class _TextUpdate:
    def __init__(self, user_id, message):
        from test_medbot_router import _FakeUser

        self.effective_user = _FakeUser(user_id)
        self.message = message
        self.effective_message = message


class CandidateCacheTests(unittest.IsolatedAsyncioTestCase):
    """The verified AI pool must not be rebuilt on every user message."""

    async def asyncSetUp(self):
        import ai

        self.ai = ai
        self._orig_builder = ai._build_candidates_uncached
        self._orig_cache = ai._CANDIDATE_CACHE.get("pool")
        self._orig_at = ai._CANDIDATE_CACHE_AT
        self.calls = 0

        async def fake_builder():
            self.calls += 1
            return [
                {
                    "provider": "google_gemini",
                    "model": "gemini-test",
                    "endpoint": "e",
                }
            ]

        ai._build_candidates_uncached = fake_builder
        ai._CANDIDATE_CACHE["pool"] = None
        ai._CANDIDATE_CACHE_AT = 0.0

    async def asyncTearDown(self):
        self.ai._build_candidates_uncached = self._orig_builder
        self.ai._CANDIDATE_CACHE["pool"] = self._orig_cache
        self.ai._CANDIDATE_CACHE_AT = self._orig_at

    async def test_pool_is_reused_within_ttl(self):
        first = await self.ai._get_candidates()
        second = await self.ai._get_candidates()

        self.assertEqual(self.calls, 1)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)

    async def test_returned_pool_is_a_copy(self):
        first = await self.ai._get_candidates()
        first.clear()
        second = await self.ai._get_candidates()
        self.assertEqual(len(second), 1)


if __name__ == "__main__":
    unittest.main()
