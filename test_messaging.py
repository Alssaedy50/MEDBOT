"""Tests for the isolated Contact Admin messaging system.

Runs against a temporary SQLite database. Asserts the messaging system is
not coupled to content/contributions and that admin gating is enforced per
call regardless of client-supplied callback data.
"""

import os
import tempfile
import unittest

import database
import main
import messaging
from test_medbot_router import (
    _FakeBot,
    _FakeContext,
    _FakeQuery,
    _FakeUpdate,
    _TextMessage,
    _MediaUpdate,
)


class MessagingBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-msg-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        database.DB_NAME = self.db_path
        await database.init_db()

        await database.add_sub_admin(500, "admin")
        await database.add_sub_admin(501, "admin2")
        self.admin_id = 500
        self.student_id = 900

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
        await messaging.messaging_callback_handler(_FakeUpdate(query), ctx)
        return query, ctx


# ---------------------------------------------------------------
# Migration
# ---------------------------------------------------------------


class MessagingMigrationTests(MessagingBase):
    async def test_messages_table_exists(self):
        db = await database.get_db()
        try:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
            ) as cur:
                self.assertIsNotNone(await cur.fetchone())
        finally:
            await db.close()

    async def test_migration_idempotent(self):
        await database.init_db()
        await database.init_db()

        db = await database.get_db()
        try:
            async with db.execute("PRAGMA index_list(messages)") as cur:
                names = {row[1] for row in await cur.fetchall()}
        finally:
            await db.close()

        self.assertIn("idx_messages_status", names)
        self.assertIn("idx_messages_user", names)

    async def test_existing_tables_untouched(self):
        # Messaging must not disturb existing systems' tables.
        await database.add_folder(0, "Root", "general")
        fid = (await database.get_folders(0))[0][0]
        await database.add_content(fid, "Doc", "fid-1", "document")

        await database.init_db()

        self.assertEqual(len(await database.get_folders(0)), 1)
        self.assertEqual(len(await database.get_files(fid)), 1)


# ---------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------


class MessagingDBTests(MessagingBase):
    async def test_create_and_get_message(self):
        mid = await database.create_message(
            self.student_id, "Student", "message", "hello admin"
        )
        record = await database.get_message(mid)
        self.assertIsNotNone(record)
        self.assertEqual(record[3], "message")
        self.assertEqual(record[4], "hello admin")
        self.assertEqual(record[5], "NEW")
        self.assertIsNone(record[6])

    async def test_invalid_category_rejected(self):
        with self.assertRaises(database.MessageValidationError):
            await database.create_message(
                self.student_id, "S", "not_a_category", "body"
            )

    async def test_empty_body_rejected(self):
        with self.assertRaises(database.MessageValidationError):
            await database.create_message(self.student_id, "S", "message", "   ")

    async def test_overlong_body_rejected(self):
        with self.assertRaises(database.MessageValidationError):
            await database.create_message(
                self.student_id,
                "S",
                "message",
                "x" * (database.MAX_MESSAGE_BODY_LENGTH + 1),
            )

    async def test_reply_sets_replied_and_records_admin(self):
        mid = await database.create_message(
            self.student_id, "S", "message", "q"
        )
        result = await database.reply_to_message(mid, self.admin_id, "answer")
        self.assertIsNotNone(result)

        record = await database.get_message(mid)
        self.assertEqual(record[5], "REPLIED")
        self.assertEqual(record[6], "answer")
        self.assertEqual(record[7], self.admin_id)

    async def test_reply_to_missing_refused(self):
        self.assertIsNone(
            await database.reply_to_message(999999, self.admin_id, "x")
        )

    async def test_reply_to_closed_refused(self):
        mid = await database.create_message(self.student_id, "S", "message", "q")
        await database.set_message_status(mid, "CLOSED")
        self.assertIsNone(
            await database.reply_to_message(mid, self.admin_id, "x")
        )

    async def test_empty_reply_refused(self):
        mid = await database.create_message(self.student_id, "S", "message", "q")
        self.assertIsNone(
            await database.reply_to_message(mid, self.admin_id, "   ")
        )
        # Status unchanged.
        self.assertEqual((await database.get_message(mid))[5], "NEW")

    async def test_status_transitions(self):
        mid = await database.create_message(self.student_id, "S", "message", "q")
        for status in ("IN_REVIEW", "REPLIED", "CLOSED"):
            self.assertTrue(await database.set_message_status(mid, status))
            self.assertEqual((await database.get_message(mid))[5], status)

    async def test_invalid_status_refused(self):
        mid = await database.create_message(self.student_id, "S", "message", "q")
        self.assertFalse(await database.set_message_status(mid, "BOGUS"))
        self.assertEqual((await database.get_message(mid))[5], "NEW")

    async def test_status_missing_message_refused(self):
        self.assertFalse(await database.set_message_status(999999, "CLOSED"))

    async def test_user_messages_isolated_per_user(self):
        await database.create_message(self.student_id, "S", "message", "mine")
        await database.create_message(777, "Other", "message", "theirs")

        mine = await database.get_user_messages(self.student_id)
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0][2], "mine")

    async def test_open_count(self):
        await database.create_message(self.student_id, "S", "message", "a")
        mid = await database.create_message(self.student_id, "S", "message", "b")
        await database.set_message_status(mid, "CLOSED")

        self.assertEqual(await database.get_open_messages_count(), 1)

    async def test_no_coupling_to_content_or_contributions(self):
        """Messaging must be usable with zero content/contribution rows."""
        mid = await database.create_message(
            self.student_id, "S", "message", "independent"
        )
        self.assertIsNotNone(await database.get_message(mid))

        folders = await database.get_folders(0)
        self.assertEqual(folders, [])
        self.assertEqual(await database.get_pending_contributions_count(), 0)


# ---------------------------------------------------------------
# Student flow
# ---------------------------------------------------------------


class StudentFlowTests(MessagingBase):
    async def test_contact_screen_and_category_selection(self):
        query, ctx = await self._route(self.student_id, "contact")
        self.assertIsNotNone(query.last_text)

        query, ctx = await self._route(self.student_id, "msg_cat:suggestion", ctx)
        self.assertEqual(ctx.user_data.get("contact_category"), "suggestion")

    async def test_invalid_category_selection_rejected(self):
        ctx = _FakeContext()
        query, ctx = await self._route(self.student_id, "msg_cat:bogus", ctx)
        self.assertIsNone(ctx.user_data.get("contact_category"))

    async def test_typed_body_creates_message_and_notifies_admins(self):
        ctx = _FakeContext()
        await self._route(self.student_id, "msg_cat:message", ctx)

        msg = _TextMessage("I need help")
        handled = await messaging.handle_contact_text(
            _MediaUpdate(self.student_id, msg), ctx
        )
        self.assertTrue(handled)
        self.assertIsNone(ctx.user_data.get("contact_category"))

        items = await database.get_user_messages(self.student_id)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][3], "NEW")

        notified = {c["chat_id"] for c in ctx.bot.sent}
        self.assertIn(self.admin_id, notified)
        self.assertIn(501, notified)

    async def test_cancel_aborts_message(self):
        ctx = _FakeContext()
        await self._route(self.student_id, "msg_cat:message", ctx)

        msg = _TextMessage("/cancel")
        handled = await messaging.handle_contact_text(
            _MediaUpdate(self.student_id, msg), ctx
        )
        self.assertTrue(handled)
        self.assertEqual(await database.get_user_messages(self.student_id), [])

    async def test_overlong_body_reports_error_without_storing(self):
        ctx = _FakeContext()
        await self._route(self.student_id, "msg_cat:message", ctx)

        msg = _TextMessage("x" * (database.MAX_MESSAGE_BODY_LENGTH + 1))
        handled = await messaging.handle_contact_text(
            _MediaUpdate(self.student_id, msg), ctx
        )
        self.assertTrue(handled)
        self.assertEqual(await database.get_user_messages(self.student_id), [])

    async def test_handle_contact_text_noop_without_state(self):
        ctx = _FakeContext()
        msg = _TextMessage("random")
        handled = await messaging.handle_contact_text(
            _MediaUpdate(self.student_id, msg), ctx
        )
        self.assertFalse(handled)

    async def test_my_messages_shows_status_and_reply(self):
        mid = await database.create_message(
            self.student_id, "S", "message", "question"
        )
        await database.reply_to_message(mid, self.admin_id, "the answer")

        query, _ = await self._route(self.student_id, "msg_mine")
        self.assertIn(str(mid), query.last_text)
        self.assertIn("the answer", query.last_text)

    async def test_my_messages_empty_state(self):
        query, _ = await self._route(self.student_id, "msg_mine")
        self.assertIsNotNone(query.last_text)

    async def test_student_cannot_reach_admin_message_list(self):
        query, ctx = await self._route(self.student_id, "admin_messages")
        self.assertNotIn("رسائل الطلاب", query.last_text or "")
        # No admin list buttons leaked.
        labels = []
        if query.last_markup:
            for row in query.last_markup.inline_keyboard:
                for b in row:
                    labels.append(b.callback_data)
        self.assertNotIn("admin_messages", labels)


# ---------------------------------------------------------------
# Admin flow + authorization
# ---------------------------------------------------------------


class AdminFlowTests(MessagingBase):
    async def test_admin_can_list_messages(self):
        await database.create_message(self.student_id, "S", "message", "q")
        query, _ = await self._route(self.admin_id, "admin_messages")
        self.assertIn("رسائل الطلاب", query.last_text)

    async def test_admin_can_open_any_message(self):
        mid = await database.create_message(self.student_id, "S", "message", "q")
        query, _ = await self._route(self.admin_id, f"msg_open:{mid}")
        self.assertIn(str(mid), query.last_text)

    async def test_non_admin_cannot_open_message(self):
        mid = await database.create_message(self.student_id, "S", "message", "secret")
        query, _ = await self._route(999, f"msg_open:{mid}")
        self.assertNotIn("secret", query.last_text or "")
        self.assertIn("غير مصرح", query.last_text or "")

    async def test_non_admin_cannot_arm_reply(self):
        mid = await database.create_message(self.student_id, "S", "message", "q")
        ctx = _FakeContext()
        query, ctx = await self._route(999, f"msg_reply:{mid}", ctx)
        self.assertIsNone(ctx.user_data.get("contact_reply_id"))
        self.assertIn("غير مصرح", query.last_text or "")

    async def test_non_admin_cannot_change_status(self):
        mid = await database.create_message(self.student_id, "S", "message", "q")
        await self._route(999, f"msg_status:{mid}:CLOSED")
        self.assertEqual((await database.get_message(mid))[5], "NEW")

    async def test_admin_reply_flow_notifies_student(self):
        mid = await database.create_message(self.student_id, "S", "message", "q")
        ctx = _FakeContext()

        await self._route(self.admin_id, f"msg_reply:{mid}", ctx)
        self.assertEqual(ctx.user_data.get("contact_reply_id"), mid)

        msg = _TextMessage("هذا هو الرد")
        handled = await messaging.handle_admin_reply_text(
            _MediaUpdate(self.admin_id, msg), ctx
        )
        self.assertTrue(handled)

        record = await database.get_message(mid)
        self.assertEqual(record[5], "REPLIED")
        self.assertEqual(record[6], "هذا هو الرد")
        self.assertIn(self.student_id, {c["chat_id"] for c in ctx.bot.sent})

    async def test_non_admin_typed_reply_ignored(self):
        mid = await database.create_message(self.student_id, "S", "message", "q")
        ctx = _FakeContext()
        ctx.user_data["contact_reply_id"] = mid

        msg = _TextMessage("hijack")
        handled = await messaging.handle_admin_reply_text(
            _MediaUpdate(999, msg), ctx
        )
        self.assertTrue(handled)
        self.assertEqual((await database.get_message(mid))[5], "NEW")

    async def test_status_change_via_callback(self):
        mid = await database.create_message(self.student_id, "S", "message", "q")
        query, _ = await self._route(self.admin_id, f"msg_status:{mid}:IN_REVIEW")
        self.assertEqual((await database.get_message(mid))[5], "IN_REVIEW")

    async def test_full_lifecycle(self):
        mid = await database.create_message(self.student_id, "S", "message", "q")
        await self._route(self.admin_id, f"msg_status:{mid}:IN_REVIEW")
        self.assertEqual((await database.get_message(mid))[5], "IN_REVIEW")

        # Replying arms a text step; the reply lands when text is sent.
        ctx = _FakeContext()
        await self._route(self.admin_id, f"msg_reply:{mid}", ctx)
        msg = _TextMessage("الرد النهائي")
        await messaging.handle_admin_reply_text(_MediaUpdate(self.admin_id, msg), ctx)
        self.assertEqual((await database.get_message(mid))[5], "REPLIED")

        await self._route(self.admin_id, f"msg_status:{mid}:CLOSED")
        self.assertEqual((await database.get_message(mid))[5], "CLOSED")

    async def test_reply_to_closed_message_via_ui_refused(self):
        mid = await database.create_message(self.student_id, "S", "message", "q")
        await database.set_message_status(mid, "CLOSED")
        ctx = _FakeContext()
        query, ctx = await self._route(self.admin_id, f"msg_reply:{mid}", ctx)
        self.assertIsNone(ctx.user_data.get("contact_reply_id"))

    async def test_notification_failure_non_fatal(self):
        class _BrokenBot:
            async def send_message(self, **kwargs):
                raise RuntimeError("blocked")

        delivered = await messaging.notify_admins_new_message(
            _BrokenBot(), 1, "message", "body", "S"
        )
        self.assertEqual(delivered, 0)


# ---------------------------------------------------------------
# Malformed callbacks
# ---------------------------------------------------------------


class MalformedCallbackTests(MessagingBase):
    async def test_malformed_callbacks_never_crash(self):
        for data in (
            "msg_open:abc",
            "msg_reply:abc",
            "msg_status:abc",
            "msg_status:",
            "msg_status:5",
            "msg_status:5:BOGUS",
            "msg_status:5:CLOSED:extra",
            "msg_cat:",
            "msg_open:",
            "msg_reply:",
        ):
            query, _ = await self._route(self.admin_id, data)
            self.assertTrue(query.answered, f"answer() not called for {data}")

    async def test_malformed_never_mutates(self):
        mid = await database.create_message(self.student_id, "S", "message", "q")
        await self._route(self.admin_id, f"msg_status:{mid}:BOGUS")
        self.assertEqual((await database.get_message(mid))[5], "NEW")


# ---------------------------------------------------------------
# Integration with main router
# ---------------------------------------------------------------


class MainIntegrationTests(MessagingBase):
    async def test_home_keyboard_has_contact_button(self):
        labels = []
        for row in main.home_keyboard().inline_keyboard:
            for b in row:
                labels.append(b.callback_data)
        self.assertIn("contact", labels)

    async def test_registered_before_catch_all_router(self):
        """`contact` must resolve to messaging, not fall through silently."""
        query, _ = await self._route(self.student_id, "contact")
        self.assertIsNotNone(query.last_text)
        # The contact label is platform-configurable and defaults to the
        # broader "تواصل مع المنصة" concept.
        self.assertIn("تواصل مع المنصة", query.last_text)

    async def test_ai_handler_routes_contact_state(self):
        """ai_handler must consume the body via messaging, not the AI path."""
        ctx = _FakeContext()
        await self._route(self.student_id, "msg_cat:report", ctx)

        msg = _TextMessage("مشكلة في مورد")
        update = _MediaUpdate(self.student_id, msg)
        await main.ai_handler(update, ctx)

        # The body was stored as a message (not answered by the AI), the
        # capture state was cleared, and the only reply is the receipt
        # confirmation — no AI answer was generated.
        items = await database.get_user_messages(self.student_id)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][3], "NEW")
        self.assertIsNone(ctx.user_data.get("contact_category"))
        self.assertEqual(len(msg.replies), 1)
        self.assertIn("تم استلام رسالتك", msg.replies[0])

    async def test_cancel_command_clears_contact_state(self):
        ctx = _FakeContext()
        ctx.user_data["contact_category"] = "message"

        msg = _TextMessage("/cancel")
        await main.cancel_command(_MediaUpdate(self.student_id, msg), ctx)
        self.assertIsNone(ctx.user_data.get("contact_category"))


if __name__ == "__main__":
    unittest.main()
