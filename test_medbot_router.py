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
        self.photos = []
        self.audios = []
        self.videos = []
        self.sent_methods = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        self.sent_methods.append("send_message")

    async def send_document(self, **kwargs):
        self.documents.append(kwargs)
        self.sent_methods.append("send_document")

    async def send_photo(self, **kwargs):
        self.photos.append(kwargs)
        self.sent_methods.append("send_photo")

    async def send_audio(self, **kwargs):
        self.audios.append(kwargs)
        self.sent_methods.append("send_audio")

    async def send_video(self, **kwargs):
        self.videos.append(kwargs)
        self.sent_methods.append("send_video")


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

    async def test_create_child_folder_end_to_end(self):
        ctx = _FakeContext()

        # Open the child-creation flow for Pathology.
        start = _FakeQuery(500, f"admin_folder_child:{self.path_id}")
        await main.callback_router(_FakeUpdate(start), ctx)
        self.assertTrue(ctx.user_data.get("admin_folder_create"))
        self.assertEqual(ctx.user_data.get("admin_folder_parent"), self.path_id)

        # Type the name, choose a type, then confirm contribution policy.
        typed = _TextMessage("New Branch")
        self.assertTrue(
            await main.request_admin_folder_name(_MediaUpdate(500, typed), ctx)
        )

        type_btn = _FakeQuery(500, "admin_folder_type:summaries")
        await main.callback_router(_FakeUpdate(type_btn), ctx)

        finish = _FakeQuery(500, "admin_folder_accepts:1")
        await main.callback_router(_FakeUpdate(finish), ctx)

        children = await database.get_folders(self.path_id)
        created = [c for c in children if c[1] == "New Branch"]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][2], "summaries")
        self.assertEqual(created[0][3], 1)
        self.assertFalse(ctx.user_data.get("admin_folder_create"))
        self.assertIsNone(ctx.user_data.get("admin_folder_name"))

    async def test_create_folder_survives_text_after_state_cleared(self):
        ctx = _FakeContext()
        start = _FakeQuery(500, f"admin_folder_child:{self.path_id}")
        await main.callback_router(_FakeUpdate(start), ctx)

        # Admin presses the in-flow "❌ إلغاء" button (-> admin_folders).
        cancel = _FakeQuery(500, "admin_folders")
        await main.callback_router(_FakeUpdate(cancel), ctx)
        self.assertFalse(ctx.user_data.get("admin_folder_create"))

        # A later stray text must not create a folder.
        before = len(await database.get_folders(self.path_id))
        typed = _TextMessage("Ghost Folder")
        handled = await main.request_admin_folder_name(_MediaUpdate(500, typed), ctx)
        self.assertFalse(handled)
        after = len(await database.get_folders(self.path_id))
        self.assertEqual(before, after)

    async def test_leaving_rename_state_via_folder_view_cancels(self):
        ctx = _FakeContext()

        rename = _FakeQuery(500, f"admin_folder_rename:{self.leaf_id}")
        await main.callback_router(_FakeUpdate(rename), ctx)
        self.assertTrue(ctx.user_data.get("admin_folder_rename"))

        # Navigate back into a folder view instead of typing a name.
        leave = _FakeQuery(500, f"admin_folder:{self.path_id}")
        await main.callback_router(_FakeUpdate(leave), ctx)
        self.assertFalse(ctx.user_data.get("admin_folder_rename"))

        # The original name is untouched.
        self.assertEqual((await database.get_folder(self.leaf_id))[2], "Summaries")

    async def test_toggle_contributions_persists(self):
        ctx = _FakeContext()

        self.assertEqual(
            await database.folder_accepts_contributions(self.leaf_id), 0
        )

        toggle = _FakeQuery(500, f"admin_folder_toggle:{self.leaf_id}")
        await main.callback_router(_FakeUpdate(toggle), ctx)

        self.assertEqual(
            await database.folder_accepts_contributions(self.leaf_id), 1
        )

        # Toggling again flips it back.
        toggle = _FakeQuery(500, f"admin_folder_toggle:{self.leaf_id}")
        await main.callback_router(_FakeUpdate(toggle), ctx)
        self.assertEqual(
            await database.folder_accepts_contributions(self.leaf_id), 0
        )

    async def test_move_folder_to_valid_parent_succeeds(self):
        ctx = _FakeContext()

        # Move the leaf out of Pathology and up to Second Year.
        move = _FakeQuery(500, f"admin_folder_move_to:{self.leaf_id}:{self.year_id}")
        await main.callback_router(_FakeUpdate(move), ctx)

        self.assertEqual(
            await database.get_parent_id(self.leaf_id), self.year_id
        )
        # Its resource travels with it.
        files = await database.get_files(self.leaf_id)
        self.assertEqual([f[0] for f in files], [self.content_id])

    async def test_delete_empty_folder_via_router(self):
        ctx = _FakeContext()
        await database.add_folder(self.path_id, "Doomed", "general")
        doomed_id = [
            f[0] for f in await database.get_folders(self.path_id) if f[1] == "Doomed"
        ][0]

        delete = _FakeQuery(500, f"admin_folder_delete:{doomed_id}")
        await main.callback_router(_FakeUpdate(delete), ctx)

        self.assertIsNone(await database.get_folder(doomed_id))


# ============================================================
# Contextual contribution breadcrumbs
# ============================================================


class ContributionBreadcrumbTests(unittest.IsolatedAsyncioTestCase):
    """Hierarchical contribution navigation + contextual labels."""

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

    async def _open_contributions(self, context=None):
        context = context or _FakeContext()
        query = _FakeQuery(999, "contribute")
        await main.callback_router(_FakeUpdate(query), context)
        return query, context

    def _buttons(self, query):
        return [
            (button.text, button.callback_data)
            for row in query.last_markup.inline_keyboard
            for button in row
        ]

    async def test_contribute_enables_contribution_mode(self):
        _, context = await self._open_contributions()
        self.assertTrue(context.user_data.get("contribution_mode"))

    async def test_root_level_shows_only_root_folders(self):
        query, _ = await self._open_contributions()
        texts = [text for text, _ in self._buttons(query)]

        self.assertTrue(any("باطنة" in text for text in texts), texts)
        # The opted-in child is not listed at the root level any more.
        self.assertFalse(any("عملي" in text for text in texts), texts)

    async def test_drill_down_reaches_opted_in_leaf(self):
        context = _FakeContext()

        # Root -> parent shows the child.
        parent_query = _FakeQuery(999, f"contrib_folder:{self.parent_id}")
        await main.callback_router(_FakeUpdate(parent_query), context)

        labels = self._buttons(parent_query)
        texts = [text for text, _ in labels]
        self.assertTrue(any("عملي" in text for text in texts), texts)

        # Parent -> child: opted-in leaf becomes the send target.
        child_query = _FakeQuery(999, f"contrib_folder:{self.child_id}")
        await main.callback_router(_FakeUpdate(child_query), context)

        self.assertEqual(
            context.user_data.get("contribution_folder"), self.child_id
        )

    async def test_leaf_accepting_folder_shows_send_button(self):
        await database.update_folder_accepts_contributions(self.parent_id, 1)

        query = _FakeQuery(999, f"contrib_folder:{self.parent_id}")
        context = _FakeContext()
        await main.callback_router(_FakeUpdate(query), context)

        callbacks = [cb for _, cb in self._buttons(query)]
        self.assertIn(f"contrib_submit_here:{self.parent_id}", callbacks)

    async def test_submit_here_sets_target_folder(self):
        await database.update_folder_accepts_contributions(self.parent_id, 1)

        query = _FakeQuery(999, f"contrib_submit_here:{self.parent_id}")
        context = _FakeContext()
        await main.callback_router(_FakeUpdate(query), context)

        self.assertEqual(
            context.user_data.get("contribution_folder"), self.parent_id
        )

    async def test_navigation_back_to_root(self):
        context = _FakeContext()
        query = _FakeQuery(999, f"contrib_folder:{self.parent_id}")
        await main.callback_router(_FakeUpdate(query), context)

        root_query = _FakeQuery(999, "contribute_root")
        await main.callback_router(_FakeUpdate(root_query), context)

        texts = [text for text, _ in self._buttons(root_query)]
        self.assertTrue(any("باطنة" in text for text in texts), texts)

    async def test_contribution_label_respects_length_limit(self):
        await database.add_folder(self.parent_id, "ط" * 60, "general", 1)

        query = _FakeQuery(999, f"contrib_folder:{self.parent_id}")
        await main.callback_router(_FakeUpdate(query), _FakeContext())

        for text, _ in self._buttons(query):
            body = text.replace("📁 ", "").replace("📥 ", "")
            self.assertLessEqual(len(body), 40, text)

    async def test_missing_folder_does_not_crash_or_keep_state(self):
        context = _FakeContext()
        query = _FakeQuery(999, "contrib_folder:999999")
        await main.callback_router(_FakeUpdate(query), context)

        self.assertFalse(context.user_data.get("contribution_folder"))
        self.assertTrue(query.answered)

    async def test_media_submission_clears_contribution_state(self):
        context = _FakeContext()
        context.user_data["contribution_folder"] = self.child_id
        context.user_data["contribution_mode"] = True

        msg = _MediaMessage(document=_FakeDoc("fid-contrib", "note.pdf"))
        await main.media_router(_MediaUpdate(999, msg), context)

        self.assertFalse(context.user_data.get("contribution_folder"))
        self.assertFalse(context.user_data.get("contribution_mode"))
        self.assertTrue(msg.replies)


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


# ============================================================
# Advanced suite — MCQ, leaderboard, filters, notifications
# ============================================================


class AdvancedSuiteTests(unittest.IsolatedAsyncioTestCase):
    """Shared isolated database for the advanced feature set."""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-adv-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        self._old_search = __import__("search_engine").DB_NAME
        database.DB_NAME = self.db_path
        __import__("search_engine").DB_NAME = self.db_path
        await database.init_db()

        self.admin_id = 500
        await database.add_sub_admin(self.admin_id, "admin")
        await database.register_user(self.admin_id, "admin", "Admin User")

        await database.add_folder(0, "Block A", "general")
        self.root_id = (await database.get_folders(0))[0][0]

        await database.add_folder(self.root_id, "Theory", "mcq")
        self.sub_id = (await database.get_folders(self.root_id))[0][0]

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        __import__("search_engine").DB_NAME = self._old_search
        for name in os.listdir(self.tmp_dir):
            try:
                os.remove(os.path.join(self.tmp_dir, name))
            except OSError:
                pass
        os.rmdir(self.tmp_dir)

    def _buttons(self, query):
        return [
            (button.text, button.callback_data)
            for row in query.last_markup.inline_keyboard
            for button in row
        ]

    # ---------------- MCQ grading logic ----------------

    async def test_mcq_grading_logic_direct(self):
        import mcq_engine

        question = {
            "id": 1,
            "options": ["A", "B", "C"],
            "correct_index": 1,
            "explanation": "e",
        }

        self.assertTrue(mcq_engine.is_correct(question, 1))
        self.assertFalse(mcq_engine.is_correct(question, 2))
        self.assertFalse(mcq_engine.is_correct(question, "not-a-number"))
        self.assertFalse(mcq_engine.is_correct(question, -1))

    async def test_mcq_malformed_question_never_raises(self):
        import mcq_engine

        # Out-of-range correct index.
        self.assertFalse(
            mcq_engine.is_correct({"options": ["A"], "correct_index": 5}, 0)
        )
        # Missing options.
        self.assertFalse(mcq_engine.is_correct({"correct_index": 0}, 0))
        # Non-dict.
        self.assertFalse(mcq_engine.is_correct(None, 0))

    async def test_mcq_session_score_and_summary(self):
        import mcq_engine

        questions = [
            {"id": 1, "options": ["A", "B"], "correct_index": 0, "explanation": ""},
            {"id": 2, "options": ["A", "B"], "correct_index": 1, "explanation": ""},
        ]

        session = mcq_engine.fresh_session(questions)
        self.assertEqual(session["total"], 2)

        first = mcq_engine.record_answer(session, 0)  # correct
        self.assertTrue(first["correct"])
        self.assertTrue(mcq_engine.advance(session))

        second = mcq_engine.record_answer(session, 1)  # correct
        self.assertTrue(second["correct"])
        self.assertFalse(mcq_engine.advance(session))

        summary = mcq_engine.score_summary(session)
        self.assertEqual(summary["score"], 2)
        self.assertEqual(summary["percentage"], 100.0)

    async def test_mcq_empty_bank_shows_no_start_button(self):
        query = _FakeQuery(999, "mcq_menu")
        await main.callback_router(_FakeUpdate(query), _FakeContext())

        callbacks = [cb for _, cb in self._buttons(query)]
        self.assertNotIn("mcq_start", callbacks)
        self.assertIn("home", callbacks)

    async def test_mcq_full_flow_grades_and_scores(self):
        await database.add_mcq_question(
            self.sub_id, "Q one?", ["alpha", "beta"], 0, "because alpha"
        )
        await database.add_mcq_question(
            self.sub_id, "Q two?", ["gamma", "delta"], 1, "because delta"
        )

        context = _FakeContext()

        menu = _FakeQuery(999, "mcq_menu")
        await main.callback_router(_FakeUpdate(menu), context)
        self.assertIn("mcq_start", [cb for _, cb in self._buttons(menu)])

        start = _FakeQuery(999, "mcq_start")
        await main.callback_router(_FakeUpdate(start), context)
        session = context.user_data.get("mcq_session")
        self.assertEqual(session["total"], 2)

        question_id = session["questions"][0]["id"]

        answer = _FakeQuery(999, f"mcq_answer:{question_id}:0")
        await main.callback_router(_FakeUpdate(answer), context)
        self.assertEqual(context.user_data["mcq_session"]["score"], 1)

        nxt = _FakeQuery(999, "mcq_next")
        await main.callback_router(_FakeUpdate(nxt), context)

        second_id = context.user_data["mcq_session"]["questions"][1]["id"]
        answer2 = _FakeQuery(999, f"mcq_answer:{second_id}:0")
        await main.callback_router(_FakeUpdate(answer2), context)

        finish = _FakeQuery(999, "mcq_finish")
        await main.callback_router(_FakeUpdate(finish), context)

        self.assertIn("1", finish.last_text)
        self.assertNotIn("mcq_session", context.user_data)

    async def test_mcq_stale_answer_does_not_change_score(self):
        await database.add_mcq_question(
            self.sub_id, "Only Q?", ["a", "b"], 0, ""
        )

        context = _FakeContext()
        start = _FakeQuery(999, "mcq_start")
        await main.callback_router(_FakeUpdate(start), context)

        # Wrong question key: must be ignored without scoring.
        stale = _FakeQuery(999, "mcq_answer:99999:0")
        await main.callback_router(_FakeUpdate(stale), context)

        self.assertEqual(context.user_data["mcq_session"]["score"], 0)

    async def test_mcq_exit_clears_session(self):
        await database.add_mcq_question(self.sub_id, "Q?", ["a", "b"], 0, "")

        context = _FakeContext()
        await main.callback_router(
            _FakeUpdate(_FakeQuery(999, "mcq_start")), context
        )
        await main.callback_router(
            _FakeUpdate(_FakeQuery(999, "mcq_exit")), context
        )

        self.assertNotIn("mcq_session", context.user_data)

    async def test_mcq_malformed_answer_callback_survives(self):
        for data in ("mcq_answer:", "mcq_answer:abc", "mcq_answer:1:x"):
            query = _FakeQuery(999, data)
            await main.callback_router(_FakeUpdate(query), _FakeContext())
            self.assertTrue(query.answered, data)

    # ---------------- MCQ admin registration + security ----------------

    async def test_admin_mcq_requires_ai_permission(self):
        await database.add_folder(0, "Temp", "general")

        # A non-admin must never reach the MCQ admin surface.
        query = _FakeQuery(999, "admin_mcq")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        self.assertIn("غير مصرح", query.last_text)

    async def test_subadmin_without_ai_permission_blocked(self):
        await database.add_sub_admin(777, "sub")
        await database.add_sub_admin_record(
            777,
            permissions={
                "can_folders": True,
                "can_content": False,
                "can_contributions": False,
                "can_ai": False,
            },
        )

        query = _FakeQuery(777, "admin_mcq")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        self.assertIn("لا تملك الصلاحية", query.last_text)

    async def test_admin_mcq_registration_full_flow(self):
        context = _FakeContext()

        start = _FakeQuery(self.admin_id, "admin_mcq_add")
        await main.callback_router(_FakeUpdate(start), context)
        self.assertEqual(context.user_data.get("admin_mcq_step"), "folder")

        pick = _FakeQuery(self.admin_id, f"admin_mcq_folder:{self.sub_id}")
        await main.callback_router(_FakeUpdate(pick), context)
        self.assertEqual(context.user_data.get("admin_mcq_step"), "text")

        text_msg = _TextMessage("Which enzyme is deficient?")
        handled = await main.handle_mcq_text_input(
            _MediaUpdate(self.admin_id, text_msg), context
        )
        self.assertTrue(handled)
        self.assertEqual(context.user_data.get("admin_mcq_step"), "options")

        options_msg = _TextMessage("Option One\nOption Two\nOption Three")
        handled = await main.handle_mcq_text_input(
            _MediaUpdate(self.admin_id, options_msg), context
        )
        self.assertTrue(handled)
        self.assertEqual(context.user_data.get("admin_mcq_step"), "correct")

        correct = _FakeQuery(self.admin_id, "admin_mcq_correct:1")
        await main.callback_router(_FakeUpdate(correct), context)
        self.assertEqual(context.user_data.get("admin_mcq_step"), "explanation")

        skip = _FakeQuery(self.admin_id, "admin_mcq_skip_explanation")
        await main.callback_router(_FakeUpdate(skip), context)

        questions = await database.get_mcq_questions(self.sub_id)
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["correct_index"], 1)
        self.assertEqual(len(questions[0]["options"]), 3)

        # State fully cleared.
        for key in ("admin_mcq_step", "admin_mcq_text", "admin_mcq_correct"):
            self.assertNotIn(key, context.user_data)

    async def test_mcq_option_validation_rejects_single_option(self):
        context = _FakeContext()
        context.user_data["admin_mcq_step"] = "options"

        msg = _TextMessage("Only one option")
        handled = await main.handle_mcq_text_input(
            _MediaUpdate(self.admin_id, msg), context
        )

        self.assertTrue(handled)
        self.assertEqual(context.user_data.get("admin_mcq_step"), "options")
        self.assertTrue(msg.replies)

    async def test_mcq_media_during_text_step_does_not_corrupt(self):
        context = _FakeContext()
        context.user_data["admin_mcq_step"] = "text"

        msg = _MediaMessage(document=_FakeDoc("fid", "file.pdf"))
        handled = await main.handle_mcq_text_input(
            _MediaUpdate(self.admin_id, msg), context
        )

        self.assertTrue(handled)
        self.assertEqual(context.user_data.get("admin_mcq_step"), "text")
        self.assertNotIn("admin_mcq_text", context.user_data)

    async def test_cancel_clears_mcq_state(self):
        context = _FakeContext()
        context.user_data["admin_mcq_step"] = "text"

        msg = _TextMessage("/cancel")
        await main.cancel_command(_MediaUpdate(self.admin_id, msg), context)

        self.assertNotIn("admin_mcq_step", context.user_data)

    # ---------------- Leaderboard ----------------

    async def test_leaderboard_open_uses_approved_only(self):
        for uid, name in ((101, "Student One"), (102, "Student Two"), (103, "Student Three")):
            await database.register_user(uid, f"u{uid}", name)

        approved_a = await database.add_contribution(
            101, "Student One", self.root_id, "T", "f1", "document"
        )
        approved_b = await database.add_contribution(
            101, "Student One", self.root_id, "T", "f2", "document"
        )
        approved_c = await database.add_contribution(
            102, "Student Two", self.root_id, "T", "f3", "document"
        )
        rejected = await database.add_contribution(
            103, "Student Three", self.root_id, "T", "f4", "document"
        )
        pending = await database.add_contribution(
            103, "Student Three", self.root_id, "T", "f5", "document"
        )

        await database.approve_contribution(approved_a)
        await database.approve_contribution(approved_b)
        await database.approve_contribution(approved_c)
        await database.reject_contribution(rejected)
        # 'pending' intentionally left pending.

        top = await database.get_top_contributors(limit=10)

        self.assertEqual(len(top), 2)
        self.assertEqual(top[0]["user_id"], 101)
        self.assertEqual(top[0]["approved_count"], 2)
        self.assertEqual(top[1]["user_id"], 102)
        self.assertEqual(top[1]["approved_count"], 1)

        # The rejected/pending-only contributor must not appear.
        self.assertNotIn(103, [entry["user_id"] for entry in top])

    async def test_leaderboard_render_shows_contributors(self):
        await database.register_user(101, "stud1", "Student One")
        cid = await database.add_contribution(
            101, "Student One", self.root_id, "T", "f1", "document"
        )
        await database.approve_contribution(cid)

        query = _FakeQuery(999, "leaderboard")
        await main.callback_router(_FakeUpdate(query), _FakeContext())

        self.assertIn("Student One", query.last_text)
        self.assertIn("لوحة المتصدرين", query.last_text)

    async def test_leaderboard_empty_state(self):
        query = _FakeQuery(999, "leaderboard")
        await main.callback_router(_FakeUpdate(query), _FakeContext())

        self.assertIn("لا يوجد متصدرون", query.last_text)

    async def test_leaderboard_limit_respected(self):
        for uid in range(200, 215):
            await database.register_user(uid, f"u{uid}", f"Student {uid}")
            cid = await database.add_contribution(
                uid, f"Student {uid}", self.root_id, "T", f"f{uid}", "document"
            )
            await database.approve_contribution(cid)

        top = await database.get_top_contributors(limit=10)
        self.assertEqual(len(top), 10)

    # ---------------- Search filters ----------------

    async def test_search_type_filter_restricts_results(self):
        await database.add_content(
            self.root_id, "Heart Lecture #ExamTrap", "f1", "document"
        )
        await database.add_content(self.root_id, "Heart Diagram", "f2", "photo")
        await database.add_content(self.root_id, "Heart Sounds", "f3", "audio")

        docs = await main.search_content("Heart", types=["document"])
        self.assertEqual([r["title"] for r in docs], ["Heart Lecture #ExamTrap"])

        photos = await main.search_content("Heart", types=["photo"])
        self.assertEqual([r["title"] for r in photos], ["Heart Diagram"])

        media = await main.search_content("Heart", types=["audio", "video"])
        self.assertEqual([r["title"] for r in media], ["Heart Sounds"])

    async def test_search_tag_filter(self):
        await database.add_content(
            self.root_id, "Tagged Resource #ClinicalRelevance", "f1", "document"
        )
        await database.add_content(self.root_id, "Plain Resource", "f2", "document")

        tagged = await main.search_content("Resource", tag="#ClinicalRelevance")
        self.assertEqual(len(tagged), 1)
        self.assertIn("#ClinicalRelevance", tagged[0]["title"])

    async def test_search_mcq_type_filter(self):
        await database.add_mcq_question(
            self.sub_id, "Heart anatomy question", ["a", "b"], 0, ""
        )

        mcqs = await main.search_content("Heart", types=["mcq"])
        self.assertEqual(len(mcqs), 1)
        self.assertEqual(mcqs[0]["result_type"], "MCQ")

    async def test_search_filter_toggle_reruns_last_search(self):
        await database.add_content(self.root_id, "Heart Lecture", "f1", "document")
        await database.add_content(self.root_id, "Heart Diagram", "f2", "photo")

        context = _FakeContext()

        # Results screen populates last_search.
        msg = _TextMessage("Heart")
        context.user_data["search_mode"] = True
        await main.run_search(_MediaUpdate(999, msg), "Heart", context)
        self.assertEqual(context.user_data.get("last_search"), "Heart")

        # Toggling the document filter re-renders into a callback message.
        toggle = _FakeQuery(999, "search_type:document")
        await main.callback_router(_FakeUpdate(toggle), context)

        self.assertEqual(context.user_data.get("search_types"), ["document"])
        self.assertIn("Heart Lecture", toggle.last_text)
        self.assertNotIn("Heart Diagram", toggle.last_text)

    async def test_search_filters_clear(self):
        context = _FakeContext()
        context.user_data["search_types"] = ["document"]
        context.user_data["search_tag"] = "#ExamTrap"

        query = _FakeQuery(999, "search_filters_clear")
        await main.callback_router(_FakeUpdate(query), context)

        self.assertNotIn("search_types", context.user_data)
        self.assertNotIn("search_tag", context.user_data)

    async def test_search_tag_toggle(self):
        await database.add_content(
            self.root_id, "Resource #HighYield", "f1", "document"
        )

        context = _FakeContext()
        msg = _TextMessage("Resource")
        await main.run_search(_MediaUpdate(999, msg), "Resource", context)

        on = _FakeQuery(999, "search_tag:#HighYield")
        await main.callback_router(_FakeUpdate(on), context)
        self.assertEqual(context.user_data.get("search_tag"), "#HighYield")

        off = _FakeQuery(999, "search_tag:#HighYield")
        await main.callback_router(_FakeUpdate(off), context)
        self.assertNotIn("search_tag", context.user_data)

    async def test_malformed_filter_callbacks_never_crash(self):
        for data in ("search_type:", "search_type:zzz", "search_tag:",
                     "search_type:document:extra"):
            query = _FakeQuery(999, data)
            await main.callback_router(_FakeUpdate(query), _FakeContext())
            self.assertTrue(query.answered, data)

    # ---------------- Notifications ----------------

    async def test_publication_alert_only_from_stored_metadata(self):
        folder = await database.get_folder(self.root_id)
        text = main._build_publication_alert("Real Title", folder[2], "test")

        self.assertIn("Real Title", text)
        self.assertIn(folder[2], text)

    async def test_notification_recipients_exclude_optouts(self):
        await database.register_user(601, "a", "A")
        await database.register_user(602, "b", "B")

        recipients = await database.get_notification_recipients()
        self.assertIn(601, recipients)
        self.assertIn(602, recipients)

        await database.set_notifications_enabled(602, False)
        recipients = await database.get_notification_recipients()

        self.assertIn(601, recipients)
        self.assertNotIn(602, recipients)

    async def test_notify_new_resource_sends_to_subscribers(self):
        await database.register_user(601, "a", "A")
        await database.register_user(602, "b", "B")

        context = _FakeContext()
        sent = await main.notify_new_resource(
            context, "Heart Lecture", self.root_id
        )

        chat_ids = [call["chat_id"] for call in context.bot.sent]
        self.assertIn(601, chat_ids)
        self.assertIn(602, chat_ids)
        self.assertEqual(sent, len(chat_ids))
        self.assertGreaterEqual(sent, 2)

    async def test_notify_new_resource_survives_send_failures(self):
        class _FlakyBot:
            def __init__(self):
                self.sent = []

            async def send_message(self, **kwargs):
                if kwargs["chat_id"] == 601:
                    raise RuntimeError("Forbidden: bot was blocked by the user")
                self.sent.append(kwargs)

        await database.register_user(601, "a", "A")
        await database.register_user(602, "b", "B")

        context = _FakeContext()
        context.bot = _FlakyBot()

        sent = await main.notify_new_resource(context, "Title", self.root_id)

        # The blocked recipient is skipped; the rest still receive the alert.
        self.assertNotIn(601, [call["chat_id"] for call in context.bot.sent])
        self.assertIn(602, [call["chat_id"] for call in context.bot.sent])
        self.assertEqual(sent, len(context.bot.sent))

    async def test_notify_new_resource_never_raises_without_recipients(self):
        # Opt every registered user out of publication alerts.
        for user_id in await database.get_all_user_ids():
            await database.set_notifications_enabled(user_id, False)

        context = _FakeContext()
        sent = await main.notify_new_resource(context, "Title", self.root_id)
        self.assertEqual(sent, 0)
        self.assertEqual(context.bot.sent, [])

    async def test_account_notification_toggle_round_trip(self):
        await database.register_user(999, "stu", "Student Nine")

        context = _FakeContext()
        account = _FakeQuery(999, "account")
        await main.callback_router(_FakeUpdate(account), context)
        self.assertIn("مفعّلة", account.last_text)

        toggle = _FakeQuery(999, "toggle_notifications")
        await main.callback_router(_FakeUpdate(toggle), context)
        self.assertIn("معطّلة", toggle.last_text)
        self.assertFalse(await database.notifications_enabled(999))

        again = _FakeQuery(999, "toggle_notifications")
        await main.callback_router(_FakeUpdate(again), context)
        self.assertTrue(await database.notifications_enabled(999))

    async def test_approval_schedules_publication_notification(self):
        await database.register_user(601, "a", "A")
        cid = await database.add_contribution(
            601, "A", self.root_id, "Approved Resource", "f1", "document"
        )

        context = _FakeContext()
        query = _FakeQuery(self.admin_id, f"approve:{cid}")
        await main.callback_router(_FakeUpdate(query), context)

        # Scheduled via the application task queue, not awaited inline.
        self.assertEqual(len(context.application.tasks), 1)

    async def test_rejection_does_not_schedule_notification(self):
        cid = await database.add_contribution(
            601, "A", self.root_id, "Rejected Resource", "f1", "document"
        )

        context = _FakeContext()
        query = _FakeQuery(self.admin_id, f"reject:{cid}")
        await main.callback_router(_FakeUpdate(query), context)

        self.assertEqual(len(context.application.tasks), 0)

    async def test_duplicate_approval_does_not_republish(self):
        cid = await database.add_contribution(
            601, "A", self.root_id, "Once Only", "f1", "document"
        )

        context = _FakeContext()
        first = _FakeQuery(self.admin_id, f"approve:{cid}")
        await main.callback_router(_FakeUpdate(first), context)

        second = _FakeQuery(self.admin_id, f"approve:{cid}")
        await main.callback_router(_FakeUpdate(second), context)

        # Exactly one publication occurred.
        self.assertEqual(len(context.application.tasks), 1)
        files = await database.get_files(self.root_id)
        self.assertEqual(len(files), 1)


# ============================================================
# Malformed callback fuzzing — the router must never raise
# ============================================================


class CallbackFuzzTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-fuzz-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        database.DB_NAME = self.db_path
        await database.init_db()

        self.admin_id = 500
        await database.add_sub_admin(self.admin_id, "admin")
        await database.register_user(self.admin_id, "admin", "Admin User")

        await database.add_folder(0, "Root", "general")
        self.root_id = (await database.get_folders(0))[0][0]

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        for name in os.listdir(self.tmp_dir):
            try:
                os.remove(os.path.join(self.tmp_dir, name))
            except OSError:
                pass
        os.rmdir(self.tmp_dir)

    MALFORMED = [
        "",
        "::::",
        "admin_folder:",
        "admin_folder:abc",
        "admin_folder:999999",
        "admin_folder:-1",
        "admin_folder:1.5",
        "admin_folder_rename:",
        "admin_folder_rename:abc",
        "admin_folder_rename:999999",
        "admin_folder_toggle:999999",
        "admin_folder_toggle:abc",
        "admin_folder_settype:999999:general",
        "admin_folder_settype:abc:general",
        "admin_folder_settype:1:not_a_type",
        "admin_folder_settype:1",
        "admin_folder_move:999999",
        "admin_folder_move:abc",
        "admin_folder_delete:999999",
        "admin_folder_delete:abc",
        "folder:999999",
        "folder:abc",
        "library:999999",
        "library:abc",
        "open_file:999999",
        "open_file:abc",
        "contrib_folder:999999",
        "contrib_folder:abc",
        "review:999999",
        "review:abc",
        "approve:999999",
        "approve:abc",
        "reject:999999",
        "reject:abc",
        "admin_file_rename:999999",
        "admin_file_delete:999999",
        "admin_file_move:999999",
        "mcq_answer:999999:999",
        "mcq_answer:abc:abc",
        "admin_mcq_delete:999999",
        "admin_mcq_delete:abc",
        "search_type:not_a_type",
        "search_tag:not_a_tag",
        "unknown_prefix_that_does_not_exist",
    ]

    async def test_malformed_callbacks_never_raise(self):
        context = _FakeContext()

        for data in self.MALFORMED:
            for user_id in (self.admin_id, 999):
                query = _FakeQuery(user_id, data)
                try:
                    await main.callback_router(_FakeUpdate(query), context)
                except Exception as exc:  # pragma: no cover - failure path
                    self.fail(
                        f"callback_router raised for data={data!r} "
                        f"user={user_id}: {exc!r}"
                    )

    async def test_non_admin_cannot_mutate_folders(self):
        # A normal user replaying an admin callback must be refused and
        # must not change the tree.
        before = await database.get_folders(0)

        for data in (
            f"admin_folder_delete:{self.root_id}",
            f"admin_folder_rename:{self.root_id}",
            f"admin_folder_move:{self.root_id}",
            f"admin_folder_settype:{self.root_id}:audio",
            f"admin_folder_toggle:{self.root_id}",
        ):
            query = _FakeQuery(999, data)
            await main.callback_router(_FakeUpdate(query), context := _FakeContext())
            self.assertIn("غير مصرح", query.last_text or "")

        after = await database.get_folders(0)
        self.assertEqual([f[0] for f in before], [f[0] for f in after])
        self.assertTrue(await database.get_folder(self.root_id))

    async def test_folder_delete_refuses_non_empty_folder(self):
        await database.add_folder(self.root_id, "Child", "general")
        await database.add_content(self.root_id, "Doc", "fid", "document")

        context = _FakeContext()
        query = _FakeQuery(self.admin_id, f"admin_folder_delete:{self.root_id}")
        await main.callback_router(_FakeUpdate(query), context)

        # The folder survives because it still holds children/resources.
        self.assertTrue(await database.get_folder(self.root_id))

    async def test_move_into_own_descendant_is_refused(self):
        await database.add_folder(self.root_id, "Child", "general")
        child_id = (await database.get_folders(self.root_id))[0][0]

        # Attempt: move Root into its own Child → would create a cycle.
        moved, _message = await database.move_folder(self.root_id, child_id)

        self.assertFalse(moved)
        self.assertEqual(await database.get_parent_id(self.root_id), 0)
        self.assertEqual(await database.get_parent_id(child_id), self.root_id)

    async def test_invalid_ids_do_not_touch_database(self):
        # Deleting a non-existent folder must be a no-op.
        self.assertFalse(await database.delete_folder(999999))
        self.assertFalse(await database.delete_folder(-1))

        # Unknown resource IDs must not raise.
        self.assertFalse(await database.update_file_title(999999, "x"))

        moved, _message = await database.move_content(999999, self.root_id)
        self.assertFalse(moved)

    async def test_move_folder_into_itself_is_refused(self):
        moved, _message = await database.move_folder(self.root_id, self.root_id)
        self.assertFalse(moved)
        self.assertEqual(await database.get_parent_id(self.root_id), 0)


# ============================================================
# Resource lifecycle — register / open / rename / move / delete
# ============================================================


class ResourceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-res-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        database.DB_NAME = self.db_path
        await database.init_db()

        self.admin_id = 500
        await database.add_sub_admin(self.admin_id, "admin")
        await database.register_user(self.admin_id, "admin", "Admin User")

        await database.add_folder(0, "Root", "general")
        self.root_id = (await database.get_folders(0))[0][0]

        await database.add_folder(self.root_id, "Child", "general")
        self.child_id = (await database.get_folders(self.root_id))[0][0]

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        for name in os.listdir(self.tmp_dir):
            try:
                os.remove(os.path.join(self.tmp_dir, name))
            except OSError:
                pass
        os.rmdir(self.tmp_dir)

    async def test_register_and_retrieve_all_media_types(self):
        samples = {
            "document": "file-id-doc",
            "photo": "file-id-photo",
            "audio": "file-id-audio",
            "video": "file-id-video",
        }

        for file_type, file_id in samples.items():
            await database.add_content(
                self.child_id, f"Title {file_type}", file_id, file_type
            )

        files = await database.get_files(self.child_id)
        self.assertEqual(len(files), len(samples))

        by_type = {}
        for item in files:
            by_type[item[3]] = item

        for file_type, file_id in samples.items():
            self.assertIn(file_type, by_type)
            record = await database.get_file_record(by_type[file_type][0])
            self.assertEqual(record[3], file_id)
            self.assertEqual(record[4], file_type)

    async def test_open_file_sends_correct_telegram_media(self):
        expected = {
            "document": "send_document",
            "photo": "send_photo",
            "audio": "send_audio",
            "video": "send_video",
        }

        for file_type, method in expected.items():
            content_id = await database.add_content(
                self.root_id, f"T {file_type}", f"fid-{file_type}", file_type
            )

            context = _FakeContext()
            query = _FakeQuery(self.admin_id, f"file:{content_id}")
            await main.open_file(query, context, content_id)

            self.assertTrue(
                any(method in call for call in context.bot.sent_methods),
                f"{file_type} should be sent via {method}; got "
                f"{context.bot.sent_methods}",
            )

    async def test_open_missing_resource_reports_gracefully(self):
        context = _FakeContext()
        query = _FakeQuery(self.admin_id, "file:999999")

        await main.open_file(query, context, 999999)

        # No media is sent, and no exception escapes.
        self.assertEqual(context.bot.sent_methods, [])
        self.assertIn("غير موجود", query.last_text or "")

    async def test_resource_rename_move_delete_persist(self):
        content_id = await database.add_content(
            self.root_id, "Old Title", "fid", "document"
        )

        # Rename
        self.assertTrue(await database.update_file_title(content_id, "New Title"))
        self.assertEqual((await database.get_file_record(content_id))[2], "New Title")

        # Move
        moved, _message = await database.move_content(content_id, self.child_id)
        self.assertTrue(moved)
        self.assertEqual((await database.get_file_record(content_id))[1], self.child_id)

        # Delete
        self.assertTrue(await database.delete_file(content_id))
        self.assertIsNone(await database.get_file_record(content_id))

    async def test_resources_stay_in_their_own_folder(self):
        a = await database.add_content(self.root_id, "A", "fa", "document")
        b = await database.add_content(self.child_id, "B", "fb", "document")

        root_files = await database.get_files(self.root_id)
        child_files = await database.get_files(self.child_id)

        self.assertEqual([f[0] for f in root_files], [a])
        self.assertEqual([f[0] for f in child_files], [b])

    async def test_move_content_to_invalid_folder_is_refused(self):
        content_id = await database.add_content(
            self.root_id, "T", "fid", "document"
        )

        moved, _message = await database.move_content(content_id, 999999)
        self.assertFalse(moved)

        # The resource must not have been detached from its folder.
        self.assertEqual((await database.get_file_record(content_id))[1], self.root_id)


# ============================================================
# Contribution targeting — media must land in the chosen folder
# ============================================================


class ContributionTargetingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-ctar-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        database.DB_NAME = self.db_path
        await database.init_db()

        # parent (accepts) -> child (accepts) -> leaf, plus a sibling
        await database.add_folder(0, "Parent", "general", 1)
        self.parent_id = (await database.get_folders(0))[0][0]

        await database.add_folder(self.parent_id, "Child", "general", 1)
        self.child_id = (await database.get_folders(self.parent_id))[0][0]

        await database.add_folder(0, "Other", "general", 1)
        self.other_id = (await database.get_folders(0))[1][0]

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        for name in os.listdir(self.tmp_dir):
            try:
                os.remove(os.path.join(self.tmp_dir, name))
            except OSError:
                pass
        os.rmdir(self.tmp_dir)

    def _buttons(self, query):
        return [
            (button.text, button.callback_data)
            for row in query.last_markup.inline_keyboard
            for button in row
        ]

    async def test_drill_down_does_not_leave_stale_send_target(self):
        context = _FakeContext()

        # 1. Open contributions and drill into the parent.
        query = _FakeQuery(999, "contribute")
        await main.callback_router(_FakeUpdate(query), context)

        drill = _FakeQuery(999, f"contrib_folder:{self.parent_id}")
        await main.callback_router(_FakeUpdate(drill), context)

        # Drilling shows the child level, which must not arm a send target.
        self.assertNotIn(
            "contribution_folder",
            context.user_data,
            "Browsing a container must not arm a send target for it",
        )

    async def test_media_after_drill_lands_in_explicit_target_only(self):
        context = _FakeContext()

        query = _FakeQuery(999, "contribute")
        await main.callback_router(_FakeUpdate(query), context)

        drill = _FakeQuery(999, f"contrib_folder:{self.parent_id}")
        await main.callback_router(_FakeUpdate(drill), context)

        # Browsing only: a media upload here must NOT be recorded.
        media = _MediaUpdate(
            999, _MediaMessage(document=_FakeDoc("fid-browse", "browse.pdf"))
        )
        await main.media_router(media, context)

        contributions = await database.get_pending_contributions()
        self.assertEqual(
            contributions,
            [],
            "Media sent while merely browsing must not create a contribution",
        )

        # Explicitly choose the child as the target, then upload.
        target = _FakeQuery(999, f"contrib_submit_here:{self.child_id}")
        await main.callback_router(_FakeUpdate(target), context)
        self.assertEqual(context.user_data.get("contribution_folder"), self.child_id)

        media = _MediaUpdate(
            999, _MediaMessage(document=_FakeDoc("fid-send", "send.pdf"))
        )
        handled = await main.media_router(media, context)
        self.assertTrue(handled)
        contributions = await database.get_pending_contributions()
        self.assertEqual(len(contributions), 1)
        self.assertEqual(contributions[0][3], self.child_id)

    async def test_navigating_away_clears_armed_target(self):
        """An armed send target must not survive subsequent tree navigation."""
        context = _FakeContext()

        query = _FakeQuery(999, "contribute")
        await main.callback_router(_FakeUpdate(query), context)

        # Arm the child folder as the explicit send target.
        target = _FakeQuery(999, f"contrib_submit_here:{self.child_id}")
        await main.callback_router(_FakeUpdate(target), context)
        self.assertEqual(context.user_data.get("contribution_folder"), self.child_id)

        # Now navigate back up to the parent level (browsing, not sending).
        back = _FakeQuery(999, f"contrib_folder:{self.parent_id}")
        await main.callback_router(_FakeUpdate(back), context)

        # Sending media now must not silently publish to the old target.
        media = _MediaUpdate(
            999, _MediaMessage(document=_FakeDoc("fid-stale", "stale.pdf"))
        )
        await main.media_router(media, context)

        contributions = await database.get_pending_contributions()
        self.assertEqual(
            contributions,
            [],
            "Stale armed target must not capture media after navigating away",
        )


class UserNavigationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Library ❯ folder ❯ resource browsing and actual media delivery."""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-user-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        database.DB_NAME = self.db_path
        await database.init_db()

        await database.add_folder(0, "Year 1", "general")
        self.year_id = (await database.get_folders(0))[0][0]

        await database.add_folder(self.year_id, "Anatomy", "general")
        self.anat_id = (await database.get_folders(self.year_id))[0][0]

        await database.add_content(
            self.anat_id, "Lecture 1", "fid-lecture-1", "document"
        )
        self.content_id = (await database.get_files(self.anat_id))[0][0]

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        for name in os.listdir(self.tmp_dir):
            try:
                os.remove(os.path.join(self.tmp_dir, name))
            except OSError:
                pass
        os.rmdir(self.tmp_dir)

    def _callbacks(self, query):
        return [
            button.callback_data
            for row in query.last_markup.inline_keyboard
            for button in row
        ]

    async def test_full_drill_down_and_back(self):
        ctx = _FakeContext()

        root = _FakeQuery(999, "library:0")
        await main.callback_router(_FakeUpdate(root), ctx)
        self.assertIn(f"folder:{self.year_id}", self._callbacks(root))

        year = _FakeQuery(999, f"folder:{self.year_id}")
        await main.callback_router(_FakeUpdate(year), ctx)
        self.assertIn(f"folder:{self.anat_id}", self._callbacks(year))
        # Back button returns to library root.
        self.assertIn("library:0", self._callbacks(year))

        anat = _FakeQuery(999, f"folder:{self.anat_id}")
        await main.callback_router(_FakeUpdate(anat), ctx)
        self.assertIn(f"file:{self.content_id}", self._callbacks(anat))
        # Back returns to the parent (Year 1).
        self.assertIn(f"library:{self.year_id}", self._callbacks(anat))

    async def test_empty_folder_renders_gracefully(self):
        ctx = _FakeContext()
        await database.add_folder(self.year_id, "Empty", "general")
        empty_id = [
            f[0] for f in await database.get_folders(self.year_id) if f[1] == "Empty"
        ][0]

        query = _FakeQuery(999, f"folder:{empty_id}")
        await main.callback_router(_FakeUpdate(query), ctx)

        self.assertIn("لا توجد", query.last_text or "")

    async def test_opening_resource_delivers_actual_media(self):
        ctx = _FakeContext()
        query = _FakeQuery(999, f"file:{self.content_id}")

        await main.callback_router(_FakeUpdate(query), ctx)

        # The registered Telegram document is actually sent.
        self.assertEqual(len(ctx.bot.documents), 1)
        self.assertEqual(ctx.bot.documents[0]["document"], "fid-lecture-1")

        # And the user is offered a way back to the folder.
        back_callbacks = [
            button.callback_data
            for call in ctx.bot.sent
            for row in (call.get("reply_markup").inline_keyboard if call.get("reply_markup") else [])
            for button in row
        ]
        self.assertIn(f"folder:{self.anat_id}", back_callbacks)

    async def test_invalid_folder_and_file_do_not_crash(self):
        ctx = _FakeContext()

        for data in ("folder:999999", "file:999999", "library:-1"):
            query = _FakeQuery(999, data)
            try:
                await main.callback_router(_FakeUpdate(query), ctx)
            except Exception as exc:  # pragma: no cover - failure path
                self.fail(f"{data} raised {exc!r}")


if __name__ == "__main__":
    unittest.main()