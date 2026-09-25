"""Callback-router tests for MEDBOT.

Simulates Telegram objects in-process so the router, authorization and
malformed-callback handling are exercised without any network access.
"""

import os
import tempfile
import unittest

import database
import main
import messaging
import audit
import admin_management
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

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)


class _FakeUpdate:
    def __init__(self, query):
        self.callback_query = query
        self.effective_user = query.from_user
        self.effective_message = None
        self.message = None


class _FakeContext:
    def __init__(self):
        self.user_data = {}
        self.bot = _FakeBot()


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
            # Contact Admin messaging (handled by the messaging module).
            "msg_open:abc",
            "msg_reply:abc",
            "msg_status:abc",
            "msg_status:",
            "msg_status:5",
            "msg_status:5:BOGUS",
            "msg_status:5:CLOSED:extra",
            "msg_cat:",
            "msg_cat:bogus",
            "msg_open:",
            "msg_reply:",
            # RBAC admin management + audit viewer (isolated modules).
            "amg_view:abc",
            "amg_preview:abc",
            "amg_perm:1",
            "amg_perm:abc:can_ai",
            "amg_role:abc:admin",
            "amg_remove:abc",
            "audit_act:",
            "audit_act:bogus_action",
            "unknown_namespace:1",
        ]

        for data in bad:
            # Messaging / audit / admin-management callbacks are dispatched to
            # their own modules in production (registered before the catch-all
            # router), so route them through their real handler; everything
            # else goes through the router.
            ns = data.split(":", 1)[0]
            if ns in ("msg_open", "msg_reply", "msg_status", "msg_cat"):
                query = _FakeQuery(999, data)
                await messaging.messaging_callback_handler(
                    _FakeUpdate(query), _FakeContext()
                )
            elif ns in ("audit_log", "audit_act"):
                query = _FakeQuery(999, data)
                await audit.audit_callback_handler(
                    _FakeUpdate(query), _FakeContext()
                )
            elif ns in ("amg_list", "amg_add", "amg_view", "amg_perm",
                        "amg_role", "amg_remove"):
                query = _FakeQuery(999, data)
                await admin_management.admin_management_callback_handler(
                    _FakeUpdate(query), _FakeContext()
                )
            else:
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

    async def test_unauthorized_new_surfaces_blocked(self):
        # The isolated modules must reject a non-admin on every entry point.
        for handler, data in (
            (admin_management.admin_management_callback_handler, "amg_list"),
            (admin_management.admin_management_callback_handler, "amg_add"),
            (audit.audit_callback_handler, "audit_log"),
            (audit.audit_callback_handler, "audit_act:folder_create"),
        ):
            query = _FakeQuery(999, data)
            await handler(_FakeUpdate(query), _FakeContext())
            self.assertIn("غير مصرح", query.last_text or "", data)

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
        self.last_markup = None

    async def reply_text(self, text, parse_mode=None, reply_markup=None):
        self.replies.append(text)
        self.last_markup = reply_markup


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


if __name__ == "__main__":
    unittest.main()
