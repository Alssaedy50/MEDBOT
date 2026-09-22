"""Callback-router tests for MEDBOT.

Simulates Telegram objects in-process so the router, authorization and
malformed-callback handling are exercised without any network access.
"""

import os
import tempfile
import unittest

import database
import main
from test_medbot_system import _FakeDoc, _FakePhoto, _FakeAudio


class _FakeUser:
    def __init__(self, user_id, full_name="Tester", first_name="Tester", username="tester"):
        self.id = user_id
        self.full_name = full_name
        self.first_name = first_name
        self.username = username


class _FakeChat:
    chat_id = 111


class _FakeMessage:
    def __init__(self):
        self.chat_id = 111


class _FakeQuery:
    def __init__(self, user_id, data):
        self.from_user = _FakeUser(user_id)
        self.data = data
        self.message = _FakeMessage()
        self.answered = False
        self.last_text = None
        self.last_markup = None

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None):
        self.last_text = text
        self.last_markup = reply_markup

    def get_bot(self):
        return _FakeBot()


class _FakeBot:
    def __init__(self):
        self.sent = []
        self.documents = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)

    async def send_document(self, **kwargs):
        self.documents.append(kwargs)


class _FakeUpdate:
    def __init__(self, query):
        self.callback_query = query
        self.effective_user = query.from_user
        self.effective_message = None
        self.message = None


class _FakeApplication:
    def __init__(self):
        self.tasks = []

    def create_task(self, coro):
        self.tasks.append(coro)
        coro.close()


class _FakeContext:
    def __init__(self):
        self.user_data = {}
        self.bot = _FakeBot()
        self.application = _FakeApplication()


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-router-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        database.DB_NAME = self.db_path
        await database.init_db()

        await database.add_folder(0, "Root", "general")
        self.root_id = (await database.get_folders(0))[0][0]
        await database.add_folder(self.root_id, "Child", "general")
        self.child_id = (await database.get_folders(self.root_id))[0][0]

        await database.add_content(self.root_id, "Doc", "fid-1", "document")
        self.content_id = (await database.get_files(self.root_id))[0][0]

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        for name in os.listdir(self.tmp_dir):
            try:
                os.remove(os.path.join(self.tmp_dir, name))
            except OSError:
                pass
        os.rmdir(self.tmp_dir)

    async def _route(self, user_id, data, context=None):
        query = _FakeQuery(user_id, data)
        ctx = context or _FakeContext()
        await main.callback_router(_FakeUpdate(query), ctx)
        return query, ctx

    # ---------------- Malformed callbacks ----------------

    async def test_malformed_callbacks_never_crash(self):
        bad = [
            "folder:abc",
            "file:abc",
            "library:xyz",
            "admin_folder:abc",
            "admin_folder_rename:abc",
            "admin_folder_move:abc",
            "admin_folder_move_to:1",
            "admin_folder_move_to:a:b",
            "admin_folder_delete:abc",
            "admin_folder_child:abc",
            "admin_folder_retype_existing:abc",
            "admin_folder_settype:9:zzz",
            "admin_folder_settype:abc:books",
            "admin_file:abc",
            "admin_file_rename:abc",
            "admin_file_retype:abc",
            "admin_file_settype:abc:video",
            "admin_file_settype:1:zzz",
            "admin_file_move:abc",
            "admin_file_move_to:1",
            "admin_file_delete:abc",
            "admin_upload:abc",
            "review:abc",
            "approve:abc",
            "reject:abc",
            "contrib_folder:abc",
            "unknown_namespace:1",
        ]

        for data in bad:
            query, _ = await self._route(999, data)
            self.assertTrue(query.answered, data)

    # ---------------- Authorization ----------------

    async def test_unauthorized_admin_callbacks_blocked(self):
        admin_callbacks = [
            "admin",
            "admin_folders",
            "admin_folder_create",
            f"admin_folder:{self.root_id}",
            f"admin_folder_rename:{self.root_id}",
            f"admin_folder_toggle:{self.root_id}",
            f"admin_folder_retype_existing:{self.root_id}",
            f"admin_folder_move:{self.root_id}",
            f"admin_folder_delete:{self.root_id}",
            f"admin_folder_child:{self.root_id}",
            f"admin_file:{self.content_id}",
            f"admin_file_rename:{self.content_id}",
            f"admin_file_retype:{self.content_id}",
            f"admin_file_move:{self.content_id}",
            f"admin_file_delete:{self.content_id}",
            f"admin_upload:{self.root_id}",
            "admin_upload_confirm",
            "admin_upload_custom_title",
            "admin_pending",
            "review:1",
            "approve:1",
            "reject:1",
            "admin_runtime",
        ]

        for data in admin_callbacks:
            query, _ = await self._route(999, data)
            self.assertTrue(query.answered, data)
            text = query.last_text or ""
            self.assertTrue(
                ("غير مصرح" in text) or ("مخصصة للمشرفين" in text),
                f"{data}: {text}",
            )

    async def test_unauthorized_cannot_delete_resource(self):
        query, _ = await self._route(999, f"admin_file_delete:{self.content_id}")
        # Resource must still exist.
        self.assertIsNotNone(await database.get_file_record(self.content_id))

    async def test_unauthorized_cannot_create_folder(self):
        query, ctx = await self._route(999, "admin_folder_create")
        # Non-admin must not enter creation state.
        self.assertFalse(ctx.user_data.get("admin_folder_create"))
        before = len(await database.get_folders(0))
        query, ctx = await self._route(999, "admin_folder_select_parent:0")
        self.assertFalse(ctx.user_data.get("admin_folder_create"))
        after = len(await database.get_folders(0))
        self.assertEqual(before, after)

    # ---------------- Admin workflows ----------------

    async def test_admin_move_cycle_protection_via_router(self):
        await database.add_sub_admin(500, "admin")
        query, _ = await self._route(
            500,
            f"admin_folder_move_to:{self.root_id}:{self.child_id}",
        )
        # Move into own descendant must be refused and parent unchanged.
        self.assertEqual(await database.get_parent_id(self.root_id), 0)

    async def test_admin_delete_non_empty_refused(self):
        await database.add_sub_admin(500, "admin")
        await self._route(500, f"admin_folder_delete:{self.root_id}")
        self.assertIsNotNone(await database.get_folder(self.root_id))

    async def test_admin_delete_empty_succeeds(self):
        await database.add_sub_admin(500, "admin")
        await database.add_folder(0, "Empty", "general")
        empty_id = [f[0] for f in await database.get_folders(0) if f[1] == "Empty"][0]
        await self._route(500, f"admin_folder_delete:{empty_id}")
        self.assertIsNone(await database.get_folder(empty_id))

    async def test_admin_retype_persists_via_router(self):
        await database.add_sub_admin(500, "admin")
        await self._route(
            500,
            f"admin_folder_settype:{self.root_id}:summaries",
        )
        self.assertEqual((await database.get_folder(self.root_id))[3], "summaries")

    async def test_admin_file_retype_persists_via_router(self):
        await database.add_sub_admin(500, "admin")
        await self._route(
            500,
            f"admin_file_settype:{self.content_id}:audio",
        )
        self.assertEqual(
            (await database.get_file_record(self.content_id))[4],
            "audio",
        )

    async def test_admin_upload_confirm_without_state_safe(self):
        await database.add_sub_admin(500, "admin")
        query, _ = await self._route(500, "admin_upload_confirm")
        self.assertTrue(query.answered)

    async def test_user_navigation_and_open_invalid(self):
        query, _ = await self._route(999, "file:999999")
        self.assertTrue(query.answered)

        query, _ = await self._route(999, f"folder:{self.root_id}")
        self.assertTrue(query.answered)
        self.assertTrue(query.last_text)


class _TextMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, parse_mode=None, reply_markup=None):
        self.replies.append(text)


class _MediaUpdate:
    def __init__(self, user_id, message):
        self.effective_user = _FakeUser(user_id)
        self.message = message
        self.effective_message = message


class _MediaMessage:
    def __init__(self, document=None, photo=None, audio=None, video=None, voice=None):
        self.document = document
        self.photo = photo
        self.audio = audio
        self.video = video
        self.voice = voice
        self.replies = []

    async def reply_text(self, text, parse_mode=None, reply_markup=None):
        self.replies.append(text)


class UploadStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-upload-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        database.DB_NAME = self.db_path
        await database.init_db()

        await database.add_folder(0, "Root", "general")
        self.root_id = (await database.get_folders(0))[0][0]
        await database.add_sub_admin(500, "admin")
        await database.add_sub_admin(501, "admin")

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        for name in os.listdir(self.tmp_dir):
            try:
                os.remove(os.path.join(self.tmp_dir, name))
            except OSError:
                pass
        os.rmdir(self.tmp_dir)

    def _ctx(self):
        return _FakeContext()

    async def test_admin_upload_document_registers_resource(self):
        ctx = self._ctx()
        query = _FakeQuery(500, f"admin_upload:{self.root_id}")
        await main.callback_router(_FakeUpdate(query), ctx)
        self.assertTrue(ctx.user_data.get("admin_upload"))

        msg = _MediaMessage(document=_FakeDoc("fid-doc", "anatomy.pdf"))
        handled = await main.admin_upload_media_handler(
            _MediaUpdate(500, msg), ctx
        )
        self.assertTrue(handled)
        self.assertIsInstance(ctx.user_data.get("admin_upload_preview"), dict)

        confirm = _FakeQuery(500, "admin_upload_confirm")
        await main.callback_router(_FakeUpdate(confirm), ctx)

        files = await database.get_files(self.root_id)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0][1], "anatomy.pdf")
        self.assertEqual(files[0][3], "document")
        self.assertFalse(ctx.user_data.get("admin_upload"))

    async def test_admin_upload_photo_audio_video_registration(self):
        for media in (
            dict(photo=[_FakePhoto("fid-photo")]),
            dict(audio=_FakeAudio("fid-audio", "lect.mp3")),
            dict(video=_FakeDoc("fid-video", "clip.mp4")),
        ):
            ctx = self._ctx()
            start = _FakeQuery(500, f"admin_upload:{self.root_id}")
            await main.callback_router(_FakeUpdate(start), ctx)

            msg = _MediaMessage(**media)
            await main.admin_upload_media_handler(_MediaUpdate(500, msg), ctx)

            confirm = _FakeQuery(500, "admin_upload_confirm")
            await main.callback_router(_FakeUpdate(confirm), ctx)

        files = await database.get_files(self.root_id)
        types = sorted(f[3] for f in files)
        self.assertEqual(types, ["audio", "photo", "video"])

    async def test_admin_upload_custom_title_and_cancel(self):
        ctx = self._ctx()
        start = _FakeQuery(500, f"admin_upload:{self.root_id}")
        await main.callback_router(_FakeUpdate(start), ctx)

        msg = _MediaMessage(document=_FakeDoc("fid-x", "orig.pdf"))
        await main.admin_upload_media_handler(_MediaUpdate(500, msg), ctx)

        custom = _FakeQuery(500, "admin_upload_custom_title")
        await main.callback_router(_FakeUpdate(custom), ctx)
        self.assertTrue(ctx.user_data.get("admin_upload_waiting_title"))

        title_msg = _TextMessage("Custom Title")
        handled = await main.handle_pending_title_input(
            _MediaUpdate(500, title_msg), ctx
        )
        self.assertTrue(handled)
        self.assertEqual(
            ctx.user_data["admin_upload_preview"]["title"],
            "Custom Title",
        )

        # Cancel must not register anything.
        cancel = _FakeQuery(500, "home")
        await main.callback_router(_FakeUpdate(cancel), ctx)
        self.assertFalse(ctx.user_data.get("admin_upload"))
        self.assertEqual(len(await database.get_files(self.root_id)), 0)

    async def test_upload_state_cleared_on_cancel_command(self):
        ctx = self._ctx()
        start = _FakeQuery(500, f"admin_upload:{self.root_id}")
        await main.callback_router(_FakeUpdate(start), ctx)
        self.assertTrue(ctx.user_data.get("admin_upload"))

        msg = _TextMessage("/cancel")
        update = _MediaUpdate(500, msg)
        await main.cancel_command(update, ctx)
        self.assertFalse(ctx.user_data.get("admin_upload"))

    async def test_upload_rejects_non_admin(self):
        ctx = self._ctx()
        # Non-admin cannot even enter upload state.
        start = _FakeQuery(999, f"admin_upload:{self.root_id}")
        await main.callback_router(_FakeUpdate(start), ctx)
        self.assertFalse(ctx.user_data.get("admin_upload"))

        # Even if state were forced, media is refused.
        ctx.user_data["admin_upload"] = True
        ctx.user_data["admin_upload_folder"] = self.root_id
        msg = _MediaMessage(document=_FakeDoc("fid-hack", "hack.pdf"))
        handled = await main.admin_upload_media_handler(
            _MediaUpdate(999, msg), ctx
        )
        self.assertTrue(handled)
        self.assertEqual(len(await database.get_files(self.root_id)), 0)
        self.assertFalse(ctx.user_data.get("admin_upload"))

    async def test_upload_to_deleted_folder_refused(self):
        ctx = self._ctx()
        await database.add_folder(0, "Temp", "general")
        temp_id = [f[0] for f in await database.get_folders(0) if f[1] == "Temp"][0]

        start = _FakeQuery(500, f"admin_upload:{temp_id}")
        await main.callback_router(_FakeUpdate(start), ctx)
        await database.delete_folder(temp_id)

        msg = _MediaMessage(document=_FakeDoc("fid-late", "late.pdf"))
        handled = await main.admin_upload_media_handler(
            _MediaUpdate(500, msg), ctx
        )
        self.assertTrue(handled)
        self.assertFalse(ctx.user_data.get("admin_upload"))

    async def test_contribution_selected_folder_rejects_media_too_large_title(self):
        ctx = self._ctx()
        btnq = _FakeQuery(500, "contribute")
        await main.callback_router(_FakeUpdate(btnq), ctx)

    async def test_media_without_state_falls_back_safely(self):
        ctx = self._ctx()
        msg = _MediaMessage(document=_FakeDoc("fid-none", "x.pdf"))
        update = _MediaUpdate(999, msg)
        await main.media_router(update, ctx)
        self.assertTrue(msg.replies)

    async def test_document_during_folder_rename_does_not_corrupt(self):
        ctx = self._ctx()
        rename = _FakeQuery(500, f"admin_folder_rename:{self.root_id}")
        await main.callback_router(_FakeUpdate(rename), ctx)
        self.assertTrue(ctx.user_data.get("admin_folder_rename"))

        # A document must not become the folder name and must not crash.
        msg = _MediaMessage(document=_FakeDoc("fid-doc", "evil.pdf"))
        await main.media_router(_MediaUpdate(500, msg), ctx)

        self.assertEqual((await database.get_folder(self.root_id))[2], "Root")
        self.assertTrue(ctx.user_data.get("admin_folder_rename"))

    async def test_rename_state_cleared_after_success(self):
        ctx = self._ctx()
        rename = _FakeQuery(500, f"admin_folder_rename:{self.root_id}")
        await main.callback_router(_FakeUpdate(rename), ctx)

        msg = _TextMessage("Renamed Root")
        handled = await main.request_admin_folder_rename(
            _MediaUpdate(500, msg), ctx
        )
        self.assertTrue(handled)
        self.assertEqual(
            (await database.get_folder(self.root_id))[2],
            "Renamed Root",
        )
        self.assertFalse(ctx.user_data.get("admin_folder_rename"))
        self.assertFalse(ctx.user_data.get("admin_folder_rename_id"))

    async def test_resource_rename_state_cleared_after_success(self):
        ctx = self._ctx()
        await database.add_content(self.root_id, "Old", "fid-r", "document")
        content_id = (await database.get_files(self.root_id))[0][0]

        rename = _FakeQuery(500, f"admin_file_rename:{content_id}")
        await main.callback_router(_FakeUpdate(rename), ctx)
        self.assertTrue(ctx.user_data.get("admin_file_rename_waiting"))

        msg = _TextMessage("New Doc Title")
        handled = await main.handle_pending_title_input(
            _MediaUpdate(500, msg), ctx
        )
        self.assertTrue(handled)
        self.assertEqual(
            (await database.get_file_record(content_id))[2],
            "New Doc Title",
        )
        self.assertFalse(ctx.user_data.get("admin_file_rename"))
        self.assertFalse(ctx.user_data.get("admin_file_rename_id"))

    async def test_contribution_free_media_ignored_by_upload_state(self):
        ctx = self._ctx()
        start = _FakeQuery(500, f"admin_upload:{self.root_id}")
        await main.callback_router(_FakeUpdate(start), ctx)

        # An unrelated text message must not register anything.
        text_msg = _TextMessage("random text")
        handled = await main.handle_pending_title_input(
            _MediaUpdate(500, text_msg), ctx
        )
        self.assertFalse(handled)
        self.assertEqual(len(await database.get_files(self.root_id)), 0)
        self.assertTrue(ctx.user_data.get("admin_upload"))

    async def test_contribution_requires_opted_in_folder(self):
        ctx = self._ctx()
        # root does not accept contributions
        query = _FakeQuery(999, f"contrib_folder:{self.root_id}")
        await main.callback_router(_FakeUpdate(query), ctx)
        self.assertFalse(ctx.user_data.get("contribution_folder"))

        await database.update_folder_accepts_contributions(self.root_id, 1)
        query = _FakeQuery(999, f"contrib_folder:{self.root_id}")
        await main.callback_router(_FakeUpdate(query), ctx)
        self.assertEqual(ctx.user_data.get("contribution_folder"), self.root_id)


# ============================================================
# Admin drill-down navigation
# ============================================================


class AdminNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-nav-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        database.DB_NAME = self.db_path
        await database.init_db()

        await database.add_folder(0, "Second Year", "general")
        self.year_id = (await database.get_folders(0))[0][0]

        await database.add_folder(self.year_id, "Pathology", "general")
        self.path_id = (await database.get_folders(self.year_id))[0][0]

        await database.add_folder(self.path_id, "Summaries", "summaries")
        self.leaf_id = (await database.get_folders(self.path_id))[0][0]

        await database.add_content(self.leaf_id, "Lecture 1", "fid-l1", "document")
        self.content_id = (await database.get_files(self.leaf_id))[0][0]

        await database.add_sub_admin(500, "admin")

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        for name in os.listdir(self.tmp_dir):
            try:
                os.remove(os.path.join(self.tmp_dir, name))
            except OSError:
                pass
        os.rmdir(self.tmp_dir)

    async def _open(self, folder_id, user_id=500):
        query = _FakeQuery(user_id, f"admin_folder:{folder_id}")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        return query

    def _callbacks(self, markup):
        found = []
        for row in markup.inline_keyboard:
            for button in row:
                found.append(button.callback_data)
        return found

    async def test_root_menu_lists_child_folders(self):
        query = _FakeQuery(500, "admin_folders")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        self.assertIn(
            f"admin_folder:{self.year_id}", self._callbacks(query.last_markup)
        )

    async def test_drill_down_reaches_deepest_leaf(self):
        # Second Year -> Pathology
        query = await self._open(self.year_id)
        self.assertIn(f"admin_folder:{self.path_id}", self._callbacks(query.last_markup))

        # Pathology -> Summaries
        query = await self._open(self.path_id)
        self.assertIn(f"admin_folder:{self.leaf_id}", self._callbacks(query.last_markup))

        # Summaries leaf lists its resource.
        query = await self._open(self.leaf_id)
        self.assertIn(f"admin_file:{self.content_id}", self._callbacks(query.last_markup))

    async def test_admin_folder_shows_files_as_buttons(self):
        query = await self._open(self.leaf_id)
        self.assertIn(f"admin_file:{self.content_id}", self._callbacks(query.last_markup))

    async def test_admin_folder_shows_parent_back_button(self):
        query = await self._open(self.path_id)
        self.assertIn(
            f"admin_folder:{self.year_id}", self._callbacks(query.last_markup)
        )

    async def test_admin_folder_shows_full_action_set(self):
        query = await self._open(self.leaf_id)
        callbacks = self._callbacks(query.last_markup)

        for expected in (
            f"admin_upload:{self.leaf_id}",
            f"admin_folder_child:{self.leaf_id}",
            f"admin_folder_rename:{self.leaf_id}",
            f"admin_folder_retype_existing:{self.leaf_id}",
            f"admin_folder_toggle:{self.leaf_id}",
            f"admin_folder_move:{self.leaf_id}",
            f"admin_folder_delete:{self.leaf_id}",
            "home",
        ):
            self.assertIn(expected, callbacks)

    async def test_admin_folder_survives_missing_folder(self):
        query = await self._open(999999)
        self.assertTrue(query.answered)
        self.assertIn("غير موجود", query.last_text)

    async def test_non_admin_cannot_open_admin_folder(self):
        query = await self._open(self.year_id, user_id=999)
        self.assertIn("غير مصرح", query.last_text)


# ============================================================
# Contextual contribution breadcrumbs
# ============================================================


class ContributionBreadcrumbTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-contrib-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        database.DB_NAME = self.db_path
        await database.init_db()

        await database.add_folder(0, "باطنة", "general")
        self.parent_id = (await database.get_folders(0))[0][0]

        await database.add_folder(self.parent_id, "عملي", "general", 1)
        self.child_id = (await database.get_folders(self.parent_id))[0][0]

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        for name in os.listdir(self.tmp_dir):
            try:
                os.remove(os.path.join(self.tmp_dir, name))
            except OSError:
                pass
        os.rmdir(self.tmp_dir)

    async def _labels(self):
        query = _FakeQuery(999, "contribute")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        return [
            button.text
            for row in query.last_markup.inline_keyboard
            for button in row
        ]

    async def test_contribution_label_includes_parent(self):
        labels = await self._labels()
        self.assertTrue(
            any("باطنة" in label and "عملي" in label for label in labels),
            labels,
        )

    async def test_contribution_label_respects_length_limit(self):
        await database.add_folder(
            self.parent_id, "ط" * 60, "general", 1
        )
        labels = await self._labels()
        for label in labels:
            body = label.replace("📥 ", "")
            self.assertLessEqual(len(body), 40, label)

    async def test_contribution_label_handles_root_folder(self):
        await database.add_folder(0, "مستقل", "general", 1)
        labels = await self._labels()
        self.assertTrue(any("مستقل" in label for label in labels), labels)


# ============================================================
# RBAC — sub-admin permissions
# ============================================================


class RbacTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-rbac-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        self._old_owner = os.environ.get("OWNER_ID")
        database.DB_NAME = self.db_path
        os.environ["OWNER_ID"] = "700"
        await database.init_db()

        await database.add_folder(0, "Root", "general")
        self.root_id = (await database.get_folders(0))[0][0]

        # Sub-admin with only contribution rights.
        await database.add_sub_admin(600, "reviewer")
        await database.add_sub_admin_record(
            600,
            permissions={
                "can_folders": False,
                "can_content": False,
                "can_contributions": True,
                "can_ai": False,
            },
            added_by=700,
        )

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        if self._old_owner is None:
            os.environ.pop("OWNER_ID", None)
        else:
            os.environ["OWNER_ID"] = self._old_owner

        for name in os.listdir(self.tmp_dir):
            try:
                os.remove(os.path.join(self.tmp_dir, name))
            except OSError:
                pass
        os.rmdir(self.tmp_dir)

    async def test_owner_detected_from_env(self):
        self.assertTrue(await database.is_owner(700))
        self.assertFalse(await database.is_owner(600))

    async def test_admin_id_zero_is_not_owner(self):
        os.environ["ADMIN_ID"] = "0"
        try:
            self.assertFalse(await database.is_owner(0))
        finally:
            os.environ.pop("ADMIN_ID", None)

    async def test_permission_roundtrip(self):
        await database.update_admin_permissions(
            600, {"can_folders": True, "can_ai": True}
        )
        permissions = await database.get_admin_permissions(600)
        self.assertTrue(permissions["can_folders"])
        self.assertTrue(permissions["can_ai"])
        self.assertFalse(permissions["can_content"])

    async def test_owner_always_has_all_permissions(self):
        permissions = await database.get_admin_permissions(700)
        self.assertTrue(all(permissions.values()))

    async def test_subadmin_without_row_defaults_to_permitted(self):
        await database.add_sub_admin(601, "legacy")
        permissions = await database.get_admin_permissions(601)
        self.assertTrue(all(permissions.values()))

    async def test_restricted_subadmin_blocked_from_folder_callback(self):
        query = _FakeQuery(600, f"admin_folder:{self.root_id}")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        self.assertIn("لا تملك الصلاحية", query.last_text)

    async def test_restricted_subadmin_blocked_from_upload(self):
        query = _FakeQuery(600, f"admin_upload:{self.root_id}")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        self.assertIn("لا تملك الصلاحية", query.last_text)

    async def test_restricted_subadmin_blocked_from_ai_registry(self):
        query = _FakeQuery(600, "admin_ai")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        self.assertIn("لا تملك الصلاحية", query.last_text)

    async def test_subadmin_allowed_contribution_surface(self):
        query = _FakeQuery(600, "admin_pending")
        ctx = _FakeContext()
        await main.callback_router(_FakeUpdate(query), ctx)
        self.assertNotIn("لا تملك الصلاحية", query.last_text or "")

    async def test_subadmin_cannot_reach_owner_callbacks(self):
        for data in (
            "admin_subadmins",
            "admin_db_backup",
            "admin_broadcast",
        ):
            query = _FakeQuery(600, data)
            await main.callback_router(_FakeUpdate(query), _FakeContext())
            self.assertIn("مالك النظام", query.last_text, data)

    async def test_permission_grant_reenables_access(self):
        await database.update_admin_permissions(
            600, {"can_folders": True}
        )
        query = _FakeQuery(600, f"admin_folder:{self.root_id}")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        self.assertNotIn("لا تملك الصلاحية", query.last_text or "")

    async def test_show_admin_hides_unauthorised_sections(self):
        query = _FakeQuery(600, "admin")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        callbacks = [
            button.callback_data
            for row in query.last_markup.inline_keyboard
            for button in row
        ]
        self.assertNotIn("admin_folders", callbacks)
        self.assertNotIn("admin_ai", callbacks)
        self.assertIn("admin_pending", callbacks)

    async def test_owner_sees_power_tools(self):
        query = _FakeQuery(700, "admin")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        callbacks = [
            button.callback_data
            for row in query.last_markup.inline_keyboard
            for button in row
        ]
        self.assertIn("admin_subadmins", callbacks)
        self.assertIn("admin_db_backup", callbacks)
        self.assertIn("admin_broadcast", callbacks)

    async def test_owner_sees_power_tools_even_without_admin_row(self):
        # Owner identity comes from env, not from the admins table.
        os.environ["OWNER_ID"] = "9999"
        try:
            query = _FakeQuery(9999, "admin")
            await main.callback_router(_FakeUpdate(query), _FakeContext())
            callbacks = [
                button.callback_data
                for row in query.last_markup.inline_keyboard
                for button in row
            ]
            self.assertIn("admin_subadmins", callbacks)
            self.assertIn("admin_db_backup", callbacks)
            self.assertIn("admin_broadcast", callbacks)
        finally:
            os.environ["OWNER_ID"] = "700"

    async def test_subadmin_management_flow(self):
        # Owner adds a new sub-admin by numeric id.
        ctx = _FakeContext()
        start = _FakeQuery(700, "admin_subadmin_add")
        await main.callback_router(_FakeUpdate(start), ctx)
        self.assertTrue(ctx.user_data.get("admin_subadmin_add"))

        msg = _TextMessage("800")
        update = _MediaUpdate(700, msg)
        handled = await main.handle_pending_subadmin_input(update, ctx)
        self.assertTrue(handled)
        self.assertFalse(ctx.user_data.get("admin_subadmin_add"))

        self.assertTrue(await database.is_user_admin(800))

        # Toggling a permission persists.
        query = _FakeQuery(700, "admin_subadmin_toggle:800:can_ai")
        await main.callback_router(_FakeUpdate(query), ctx)
        permissions = await database.get_admin_permissions(800)
        self.assertFalse(permissions["can_ai"])

        # Revoke removes the admin.
        revoke = _FakeQuery(700, "admin_subadmin_revoke:800")
        await main.callback_router(_FakeUpdate(revoke), ctx)
        self.assertFalse(await database.is_user_admin(800))

    async def test_malformed_subadmin_callbacks_never_crash(self):
        for data in (
            "admin_subadmin_view:abc",
            "admin_subadmin_revoke:abc",
            "admin_subadmin_toggle:abc",
            "admin_subadmin_toggle:1",
            "admin_subadmin_toggle:1:bogus",
            "admin_subadmin_toggle:1:can_ai:extra",
        ):
            query = _FakeQuery(700, data)
            await main.callback_router(_FakeUpdate(query), _FakeContext())
            self.assertTrue(query.answered, data)


# ============================================================
# Owner power tools — stats & backup
# ============================================================


class OwnerToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-owner-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        self._old_owner = os.environ.get("OWNER_ID")
        database.DB_NAME = self.db_path
        os.environ["OWNER_ID"] = "700"
        await database.init_db()

        await database.register_user(800, "student", "Student One")
        await database.add_folder(0, "Root", "general")
        self.root_id = (await database.get_folders(0))[0][0]
        await database.add_content(self.root_id, "Doc", "fid-1", "document")
        await database.add_sub_admin(700, "owner")

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        if self._old_owner is None:
            os.environ.pop("OWNER_ID", None)
        else:
            os.environ["OWNER_ID"] = self._old_owner

        for name in os.listdir(self.tmp_dir):
            try:
                os.remove(os.path.join(self.tmp_dir, name))
            except OSError:
                pass
        os.rmdir(self.tmp_dir)

    async def test_system_stats_reflect_real_counts(self):
        stats = await database.get_system_stats()
        self.assertEqual(stats["total_users"], 1)
        self.assertEqual(stats["total_folders"], 1)
        self.assertEqual(stats["total_resources"], 1)
        self.assertEqual(stats["pending_contributions"], 0)

    async def test_stats_callback_renders(self):
        query = _FakeQuery(700, "admin_stats")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        self.assertIn("إحصائيات النظام", query.last_text)
        self.assertIn("1", query.last_text)

    async def test_backup_sends_document_to_owner(self):
        ctx = _FakeContext()
        query = _FakeQuery(700, "admin_db_backup")
        await main.callback_router(_FakeUpdate(query), ctx)
        self.assertEqual(len(ctx.bot.documents), 1)
        self.assertEqual(ctx.bot.documents[0]["chat_id"], 700)

    async def test_backup_blocked_for_non_owner(self):
        ctx = _FakeContext()
        query = _FakeQuery(800, "admin_db_backup")
        await main.callback_router(_FakeUpdate(query), ctx)
        self.assertEqual(ctx.bot.documents, [])
        self.assertIn("مالك النظام", query.last_text)

    async def test_backup_handles_missing_db_file(self):
        database.DB_NAME = os.path.join(self.tmp_dir, "missing.sqlite3")
        ctx = _FakeContext()
        query = _FakeQuery(700, "admin_db_backup")
        await main.callback_router(_FakeUpdate(query), ctx)
        self.assertEqual(ctx.bot.documents, [])
        self.assertIn("لم يتم العثور", query.last_text)

    async def test_ai_registry_empty_state_and_refresh(self):
        query = _FakeQuery(700, "admin_ai")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        self.assertIn("لا توجد نماذج", query.last_text)
        callbacks = [
            button.callback_data
            for row in query.last_markup.inline_keyboard
            for button in row
        ]
        self.assertIn("admin_ai", callbacks)

    async def test_broadcast_workflow_captures_and_confirms(self):
        ctx = _FakeContext()
        start = _FakeQuery(700, "admin_broadcast")
        await main.callback_router(_FakeUpdate(start), ctx)
        self.assertTrue(ctx.user_data.get("admin_broadcast"))

        msg = _TextMessage("إعلان مهم")
        handled = await main.handle_pending_broadcast_input(
            _MediaUpdate(700, msg), ctx
        )
        self.assertTrue(handled)
        self.assertFalse(ctx.user_data.get("admin_broadcast"))
        self.assertEqual(
            ctx.user_data.get("admin_broadcast_confirm"), "إعلان مهم"
        )

    async def test_broadcast_cancel_clears_state(self):
        ctx = _FakeContext()
        start = _FakeQuery(700, "admin_broadcast")
        await main.callback_router(_FakeUpdate(start), ctx)
        self.assertTrue(ctx.user_data.get("admin_broadcast"))

        cancel = _FakeQuery(700, "admin_broadcast_cancel")
        await main.callback_router(_FakeUpdate(cancel), ctx)
        self.assertFalse(ctx.user_data.get("admin_broadcast"))

    async def test_broadcast_input_ignored_for_non_owner(self):
        ctx = _FakeContext()
        ctx.user_data["admin_broadcast"] = True
        msg = _TextMessage("hack")
        handled = await main.handle_pending_broadcast_input(
            _MediaUpdate(800, msg), ctx
        )
        self.assertFalse(handled)

    async def test_broadcast_confirm_dispatches_background_task(self):
        ctx = _FakeContext()
        ctx.user_data["admin_broadcast_confirm"] = "test announcement"
        query = _FakeQuery(700, "admin_broadcast_confirm")
        await main.callback_router(_FakeUpdate(query), ctx)
        self.assertEqual(len(ctx.application.tasks), 1)
        self.assertFalse(ctx.user_data.get("admin_broadcast_confirm"))

    async def test_broadcast_confirm_without_text_is_safe(self):
        ctx = _FakeContext()
        query = _FakeQuery(700, "admin_broadcast_confirm")
        await main.callback_router(_FakeUpdate(query), ctx)
        self.assertEqual(ctx.application.tasks, [])
        self.assertIn("لا يوجد تعميم", query.last_text)

    async def test_dispatch_broadcast_sends_to_all_users(self):
        await database.register_user(801, "s2", "Student Two")
        recipients = await database.get_all_user_ids()
        self.assertGreaterEqual(len(recipients), 2)

        ctx = _FakeContext()
        await main._dispatch_broadcast(ctx, recipients, "hello students", 700)

        sent_ids = [call["chat_id"] for call in ctx.bot.sent]
        for user_id in recipients:
            self.assertIn(user_id, sent_ids)
        # Owner receives a completion summary too.
        self.assertIn(700, sent_ids)

    async def test_dispatch_broadcast_survives_send_failures(self):
        class _FlakyBot:
            def __init__(self):
                self.sent = []

            async def send_message(self, **kwargs):
                if kwargs["chat_id"] == 801:
                    raise RuntimeError("blocked by user")
                self.sent.append(kwargs)

        ctx = _FakeContext()
        ctx.bot = _FlakyBot()
        await database.register_user(801, "s2", "Student Two")

        recipients = await database.get_all_user_ids()
        await main._dispatch_broadcast(ctx, recipients, "hello", 700)
        self.assertTrue(ctx.bot.sent)


if __name__ == "__main__":
    unittest.main()