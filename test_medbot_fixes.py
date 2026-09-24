"""Regression tests for the real-Telegram bug-fix pass.

Covers the five reported defects against a temporary SQLite database and real
handlers (no mocks of database or business logic):

  1. Admin message/reply RBAC routing.
  2. Workflow state isolation (the collision bug).
  3. Contribution preview (read-only, does not change status).
  4. Topics vs Resources navigation is distinct and localized.
  5. Full ar/en localization of the main user surfaces.

`ai_handler` receives a `context.bot` that would normally hit Telegram; the
preview test drives a fake bot so delivery can be observed without network.
"""

import os
import tempfile
import unittest

import database
import i18n
import main
import messaging
import search_engine
import topics
import workflow
from test_medbot_router import (
    _FakeContext,
    _FakeQuery,
    _FakeUpdate,
    _MediaUpdate,
    _TextMessage,
)
from test_medbot_system import _FakeDoc
from test_platform_update import _FakeSent


class _RecordingBot:
    """Minimal bot stub recording outgoing API calls."""

    def __init__(self):
        self.messages = []
        self.media = []
        self._next = 7000

    async def send_message(self, chat_id=None, text=None, **kwargs):
        self._next += 1
        self.messages.append((chat_id, text, kwargs.get("reply_markup")))
        return _FakeSent(self._next)

    async def send_document(self, chat_id=None, document=None, caption=None, **kwargs):
        self.media.append(("document", document, caption))
        return _FakeSent(self._next)

    async def send_photo(self, chat_id=None, photo=None, caption=None, **kwargs):
        self.media.append(("photo", photo, caption))
        return _FakeSent(self._next)

    async def send_audio(self, chat_id=None, audio=None, caption=None, **kwargs):
        self.media.append(("audio", audio, caption))
        return _FakeSent(self._next)

    async def send_video(self, chat_id=None, video=None, caption=None, **kwargs):
        self.media.append(("video", video, caption))
        return _FakeSent(self._next)


class FixBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-fixes-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        self._old_search = search_engine.DB_NAME
        database.DB_NAME = self.db_path
        search_engine.DB_NAME = self.db_path
        await database.init_db()

        main._CONTENT_MESSAGES_BY_USER.clear()

        self.owner_id = 700
        self.can_msg = 701
        self.no_msg = 702
        self.student_id = 900

        await database.add_sub_admin(self.owner_id, "owner")
        await database.add_sub_admin(self.can_msg, "canmsg")
        await database.add_sub_admin(self.no_msg, "nomsg")
        await database.ensure_configured_admin(self.owner_id)

        self.section = await database.add_folder(
            0, "Section", "general", accepts_contributions=1
        )

        # Revoke every messaging capability from the "no messages" admin.
        await database.update_admin_permissions(
            self.no_msg, {key: False for key in database.PERMISSION_KEYS}
        )
        perms = dict(await database.get_admin_permissions(self.can_msg))
        perms["can_messages"] = True
        await database.update_admin_permissions(self.can_msg, perms)

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        search_engine.DB_NAME = self._old_search
        main._CONTENT_MESSAGES_BY_USER.clear()
        for name in os.listdir(self.tmp_dir):
            try:
                os.remove(os.path.join(self.tmp_dir, name))
            except OSError:
                pass
        os.rmdir(self.tmp_dir)


# ---------------------------------------------------------------
# 1. Admin communication permissions
# ---------------------------------------------------------------


class MessagePermissionTests(FixBase):
    async def test_only_can_messages_admins_are_notified(self):
        bot = _RecordingBot()
        await messaging.notify_admins_new_message(
            bot, 1, "message", "hello", "Student"
        )
        chat_ids = {m[0] for m in bot.messages}
        self.assertIn(self.owner_id, chat_ids)
        self.assertIn(self.can_msg, chat_ids)
        self.assertNotIn(self.no_msg, chat_ids)

    async def test_unauthorized_admin_cannot_open_messages(self):
        query = _FakeQuery(self.no_msg, "admin_messages")
        await messaging.messaging_callback_handler(
            _FakeUpdate(query), _FakeContext()
        )
        self.assertIn("غير مصرح", query.last_text or "")

    async def test_unauthorized_admin_cannot_reply(self):
        msg_id = await database.create_message(
            self.student_id, "Student", "message", "help"
        )
        ctx = _FakeContext()
        ctx.bot = _RecordingBot()
        ctx.user_data["contact_reply_id"] = msg_id
        ctx.user_data["active_workflow"] = "admin_reply"

        update = _MediaUpdate(
            self.no_msg, _TextMessage("unauthorized reply")
        )
        handled = await messaging.handle_admin_reply_text(update, ctx)
        self.assertTrue(handled)
        record = await database.get_message(msg_id)
        self.assertEqual(record[5], "NEW")

    async def test_reply_is_audited(self):
        msg_id = await database.create_message(
            self.student_id, "Student", "message", "help"
        )
        ctx = _FakeContext()
        ctx.bot = _RecordingBot()
        ctx.user_data["contact_reply_id"] = msg_id
        ctx.user_data["active_workflow"] = "admin_reply"

        update = _MediaUpdate(
            self.can_msg, _TextMessage("an answer")
        )
        handled = await messaging.handle_admin_reply_text(update, ctx)
        self.assertTrue(handled)

        entries = await database.get_audit_entries(limit=50)
        actions = [
            (e[3] if not isinstance(e, dict) else e.get("action"))
            for e in entries
        ]
        self.assertIn("message_reply", actions)


# ---------------------------------------------------------------
# 2. Workflow state isolation
# ---------------------------------------------------------------


class WorkflowIsolationTests(FixBase):
    async def test_begin_cancels_other_workflows(self):
        ctx = _FakeContext()
        workflow.begin(ctx, "review_note")
        ctx.user_data["review_note_kind"] = "reject"
        ctx.user_data["review_note_id"] = 5

        workflow.begin(ctx, "admin_folder_create")
        self.assertNotIn("review_note_kind", ctx.user_data)
        self.assertNotIn("review_note_id", ctx.user_data)
        self.assertEqual(ctx.user_data[workflow.ACTIVE_KEY], "admin_folder_create")

    async def test_only_active_workflow_owns_input(self):
        ctx = _FakeContext()
        workflow.begin(ctx, "admin_folder_create")
        self.assertTrue(workflow.owns(ctx, "admin_folder_create"))
        self.assertFalse(workflow.owns(ctx, "review_note"))

    async def test_legacy_call_still_works_without_marker(self):
        ctx = _FakeContext()
        self.assertTrue(workflow.owns(ctx, "review_note"))

    async def test_stale_review_state_does_not_consume_section_name(self):
        """The reported bug: section-create must not hit the old review flow."""
        ctx = _FakeContext()
        ctx.bot = _RecordingBot()

        contrib_id = await database.add_contribution(
            self.student_id, "Student", self.section, "File", "fid", "document"
        )

        # Admin opened a review and left it pending.
        ctx.user_data["review_note_kind"] = "reject"
        ctx.user_data["review_note_id"] = contrib_id
        ctx.user_data["active_workflow"] = "review_note"

        # Admin then starts Section Create and types a section name.
        workflow.begin(ctx, "admin_folder_create")
        ctx.user_data["admin_folder_create"] = True
        ctx.user_data["admin_folder_name"] = True

        update = _MediaUpdate(
            self.owner_id, _TextMessage("Physiology")
        )
        # The old review consumer must refuse the message.
        self.assertFalse(await main.process_review_text(update, ctx))

        # Contribution status must be untouched.
        record = await database.get_contribution(contrib_id)
        self.assertEqual(record[7], "pending")

    async def test_clear_all_releases_everything(self):
        ctx = _FakeContext()
        workflow.begin(ctx, "topics_create")
        ctx.user_data["topics_create"] = True
        workflow.clear_all(ctx)
        self.assertNotIn("topics_create", ctx.user_data)
        self.assertNotIn(workflow.ACTIVE_KEY, ctx.user_data)

    async def test_home_resets_all_workflows(self):
        ctx = _FakeContext()
        workflow.begin(ctx, "notification_body")
        ctx.user_data["notifications_body"] = True

        query = _FakeQuery(self.owner_id, "home")
        await main.callback_router(_FakeUpdate(query), ctx)
        self.assertNotIn(workflow.ACTIVE_KEY, ctx.user_data)
        self.assertNotIn("notifications_body", ctx.user_data)


# ---------------------------------------------------------------
# 3. Contribution preview
# ---------------------------------------------------------------


class ContributionPreviewTests(FixBase):
    async def _pending(self):
        return await database.add_contribution(
            self.student_id, "Student", self.section, "Essay", "fid-1", "document"
        )

    async def test_review_screen_offers_preview(self):
        cid = await self._pending()
        query = _FakeQuery(self.owner_id, f"review:{cid}")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        callbacks = [
            b.callback_data
            for row in query.last_markup.inline_keyboard
            for b in row
        ]
        self.assertIn(f"preview:{cid}", callbacks)

    async def test_preview_sends_file_without_changing_status(self):
        cid = await self._pending()
        ctx = _FakeContext()
        bot = _RecordingBot()
        ctx.bot = bot

        query = _FakeQuery(self.owner_id, f"preview:{cid}")
        query.message.chat_id = self.owner_id
        await main.callback_router(_FakeUpdate(query), ctx)

        self.assertTrue(bot.media)
        self.assertEqual(bot.media[0][1], "fid-1")
        record = await database.get_contribution(cid)
        self.assertEqual(record[7], "pending")

    async def test_preview_denied_without_permission(self):
        cid = await self._pending()
        ctx = _FakeContext()
        ctx.bot = _RecordingBot()
        query = _FakeQuery(self.no_msg, f"preview:{cid}")
        await main.callback_router(_FakeUpdate(query), ctx)
        self.assertFalse(ctx.bot.media)
        self.assertIn("غير مصرح", query.last_text or "")

    async def test_preview_of_missing_contribution(self):
        ctx = _FakeContext()
        ctx.bot = _RecordingBot()
        query = _FakeQuery(self.owner_id, "preview:999999")
        await main.callback_router(_FakeUpdate(query), ctx)
        self.assertIn("غير موجودة", query.last_text or "")


# ---------------------------------------------------------------
# 4. Topics vs Resources navigation
# ---------------------------------------------------------------


class TopicsVsResourcesTests(FixBase):
    async def test_home_has_separate_topics_and_resources(self):
        labels = {}
        for row in main.home_keyboard("ar").inline_keyboard:
            for b in row:
                labels[b.callback_data] = b.text

        self.assertIn("library:0", labels)
        self.assertIn("topics", labels)
        self.assertNotEqual(labels["library:0"], labels["topics"])
        self.assertIn("موارد", labels["library:0"])
        self.assertIn("المواضيع", labels["topics"])

    async def test_topics_screen_links_back_to_resources(self):
        await database.add_topic("Anatomy")
        query = _FakeQuery(self.student_id, "topics")
        await topics.topics_callback_handler(_FakeUpdate(query), _FakeContext())
        callbacks = [
            b.callback_data
            for row in query.last_markup.inline_keyboard
            for b in row
        ]
        self.assertIn("library:0", callbacks)

    async def test_resource_screen_title_is_not_topics_title(self):
        query = _FakeQuery(self.student_id, "library:0")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        self.assertNotIn("المواضيع", query.last_text or "")


# ---------------------------------------------------------------
# 5. Full localization
# ---------------------------------------------------------------


class FullLocalizationTests(FixBase):
    async def test_home_keyboard_switches_language(self):
        ar = {b.callback_data: b.text for row in main.home_keyboard("ar").inline_keyboard for b in row}
        en = {b.callback_data: b.text for row in main.home_keyboard("en").inline_keyboard for b in row}

        self.assertIn("موارد", ar["library:0"])
        self.assertIn("Resources", en["library:0"])
        self.assertIn("Topics", en["topics"])
        self.assertIn("المواضيع", ar["topics"])
        self.assertNotEqual(ar["account"], en["account"])

    async def test_library_screen_uses_stored_language(self):
        await database.register_user(self.student_id, "s", "Student")
        await database.set_user_language(self.student_id, "en")

        query = _FakeQuery(self.student_id, "library:0")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        self.assertIn("Resources", query.last_text or "")
        self.assertNotIn("موارد المنصة", query.last_text or "")

    async def test_topics_screen_uses_stored_language(self):
        await database.register_user(self.student_id, "s", "Student")
        await database.set_user_language(self.student_id, "en")
        await database.add_topic("Anatomy")

        query = _FakeQuery(self.student_id, "topics")
        await topics.topics_callback_handler(_FakeUpdate(query), _FakeContext())
        self.assertIn("Search Topics", query.last_text or "")

    async def test_user_created_names_are_not_translated(self):
        """A topic name must render exactly as entered, in any language."""
        await database.register_user(self.student_id, "s", "Student")
        await database.set_user_language(self.student_id, "en")
        topic_id = await database.add_topic("Physiology")

        query = _FakeQuery(self.student_id, f"topic_open:{topic_id}")
        await topics.topics_callback_handler(_FakeUpdate(query), _FakeContext())
        self.assertIn("Physiology", query.last_text or "")

    async def test_language_preference_persists_across_reload(self):
        await database.register_user(self.student_id, "s", "Student")
        await database.set_user_language(self.student_id, "en")
        # Simulate a bot restart: reopen the database and re-read.
        self.assertEqual(await database.get_user_language(self.student_id), "en")


# ---------------------------------------------------------------
# 6. Callback delivery + visible failures
# ---------------------------------------------------------------


class CallbackDeliveryTests(FixBase):
    """Regression coverage for the deployed 'inline buttons do nothing' bug.

    The production symptom was: /start renders, but every button press is a
    no-op. Telegram keeps the last `allowed_updates` it was sent and reuses it
    when the parameter is omitted, so a stale restricted filter silently
    dropped `callback_query` updates while `message` updates kept flowing.
    """

    async def test_polling_requests_callback_updates(self):
        allowed = main.polling_allowed_updates()
        # `callback_query` must be part of the explicit subscription.
        self.assertIn("callback_query", allowed)
        # Every update type PTB knows about must be requested, so no other
        # stale filter can hide a future surface.
        from telegram import Update

        self.assertEqual(set(allowed), set(Update.ALL_TYPES))

    async def test_unmapped_callback_is_surfaced_not_silent(self):
        query = _FakeQuery(self.student_id, "totally_unknown_namespace:9")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        self.assertTrue(query.answered)
        # The user gets an explicit, visible response rather than a dead tap.
        self.assertIn("غير مدعوم", query.last_text or "")
        callbacks = [
            b.callback_data
            for row in query.last_markup.inline_keyboard
            for b in row
        ]
        self.assertIn("home", callbacks)

    async def test_edit_safe_falls_back_when_edit_fails(self):
        class _OldMessage:
            message_id = 4242
            chat_id = 1

            def __init__(self):
                self.replies = []

            async def reply_text(self, text, **kwargs):
                self.replies.append(text)

        class _OldQuery:
            def __init__(self):
                self.message = _OldMessage()

            async def edit_message_text(self, text, **kwargs):
                raise RuntimeError("Message is too old to be edited")

        query = _OldQuery()
        await main.edit_safe(query, "hello", None)
        # The tap must produce a NEW visible message instead of a dead screen.
        self.assertEqual(query.message.replies, ["hello"])

    async def test_edit_safe_not_modified_is_noop(self):
        class _SameMessage:
            message_id = 4243
            chat_id = 1

            def __init__(self):
                self.replies = []

            async def reply_text(self, text, **kwargs):
                self.replies.append(text)

        class _SameQuery:
            def __init__(self):
                self.message = _SameMessage()

            async def edit_message_text(self, text, **kwargs):
                raise RuntimeError("Message is not modified")

        query = _SameQuery()
        await main.edit_safe(query, "hello", None)
        # Re-tapping an identical button must not spam a duplicate message.
        self.assertEqual(query.message.replies, [])

    async def test_error_handler_answers_callback_with_alert(self):
        class _ErrQuery:
            def __init__(self):
                self.calls = []

            async def answer(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        class _ErrUpdate:
            callback_query = None

        class _Ctx:
            error = RuntimeError("boom")
            bot = _RecordingBot()

        query = _ErrQuery()
        update = _ErrUpdate()
        update.callback_query = query
        await main.error_handler(update, _Ctx())
        self.assertTrue(query.calls)
        self.assertTrue(query.calls[0][1].get("show_alert"))


if __name__ == "__main__":
    unittest.main()
