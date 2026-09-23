"""Regression tests for the advanced platform update.

Everything runs against a temporary SQLite database (never the real one) and
exercises real code paths — no mocks of the database or of business logic.

Covered:
  1. Ownership transfer + single-owner invariant + restart behaviour.
  2. Granular permissions and admin-panel visibility.
  3. Section/branch move over the whole hierarchy (Root, sibling, cycle-safe).
  4. Multiple resources per section remaining visible.
  5. Platform settings persistence, defaults and permission gating.
  6. Localization (ar/en) persistence and interface strings.
  7. Search topics (create/edit/deactivate/link) and the user entry point.
  8. Notification broadcasts + permission + audit.
  9. Message persistence (content survives a later navigation tap).
"""

import os
import tempfile
import unittest

import admin_management
import database
import i18n
import main
import notifications
import platform_settings
import search_engine
import topics
from test_medbot_router import (
    _FakeContext,
    _FakeQuery,
    _FakeUpdate,
    _MediaMessage,
    _MediaUpdate,
    _TextMessage,
)
from test_medbot_system import _FakeDoc


class _FakeSent:
    def __init__(self, message_id):
        self.message_id = message_id


class _ReplyTrackedMessage:
    """Message stub whose replies carry unique ids (persistence tests)."""

    def __init__(self, text="", chat_id=111):
        self.text = text
        self.chat_id = chat_id
        self.message_id = 500
        self.replies = []
        self._next = 2000

    async def reply_text(self, text, parse_mode=None, reply_markup=None):
        self._next += 1
        self.replies.append(text)
        return _FakeSent(self._next)


class PlatformUpdateBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-update-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        self._old_search = search_engine.DB_NAME
        database.DB_NAME = self.db_path
        search_engine.DB_NAME = self.db_path
        await database.init_db()

        main._CONTENT_MESSAGES_BY_USER.clear()

        self.owner_id = 500
        self.sub_id = 501
        self.student_id = 900

        await database.add_sub_admin(self.owner_id, "owner")
        await database.add_sub_admin(self.sub_id, "sub")
        await database.ensure_configured_admin(self.owner_id)

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
# 1. Ownership transfer
# ---------------------------------------------------------------


class OwnershipTransferTests(PlatformUpdateBase):
    async def test_transfer_moves_owner_and_demotes_previous(self):
        ok, _ = await database.transfer_ownership(self.owner_id, self.sub_id)
        self.assertTrue(ok)
        self.assertTrue(await database.is_owner(self.sub_id))
        self.assertFalse(await database.is_owner(self.owner_id))
        self.assertEqual(
            (await database.get_admin_record(self.owner_id))["role"], "admin"
        )

    async def test_transfer_leaves_exactly_one_owner(self):
        await database.transfer_ownership(self.owner_id, self.sub_id)
        owners = [
            r for r in [
                await database.get_admin_record(self.owner_id),
                await database.get_admin_record(self.sub_id),
            ] if r and r["role"] == "owner"
        ]
        self.assertEqual(len(owners), 1)

    async def test_only_owner_can_transfer(self):
        ok, _ = await database.transfer_ownership(self.sub_id, self.owner_id)
        self.assertFalse(ok)
        self.assertTrue(await database.is_owner(self.owner_id))

    async def test_transfer_to_self_rejected(self):
        ok, _ = await database.transfer_ownership(self.owner_id, self.owner_id)
        self.assertFalse(ok)
        self.assertTrue(await database.is_owner(self.owner_id))

    async def test_transfer_to_revoked_admin_rejected(self):
        await database.remove_sub_admin(self.sub_id)
        ok, _ = await database.transfer_ownership(self.owner_id, self.sub_id)
        self.assertFalse(ok)
        self.assertTrue(await database.is_owner(self.owner_id))

    async def test_transfer_is_audited(self):
        query = _FakeQuery(self.owner_id, f"amg_transfer_confirm:{self.sub_id}")
        await admin_management.admin_management_callback_handler(
            _FakeUpdate(query), _FakeContext()
        )
        rows = await database.get_audit_entries(action="ownership_transfer")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], self.owner_id)
        self.assertTrue(await database.is_owner(self.sub_id))

    async def test_new_owner_survives_restart_bootstrap(self):
        # Explicit transfer, then the same ADMIN_ID is re-bootstrapped at
        # startup: the transferred owner must be preserved.
        await database.transfer_ownership(self.owner_id, self.sub_id)
        await database.ensure_configured_admin(self.owner_id)
        self.assertTrue(await database.is_owner(self.sub_id))
        self.assertFalse(await database.is_owner(self.owner_id))

    async def test_changed_configured_admin_reasserts_owner(self):
        await database.ensure_configured_admin(self.owner_id)
        await database.ensure_configured_admin(self.sub_id)
        self.assertTrue(await database.is_owner(self.sub_id))

    async def test_non_owner_cannot_open_transfer_screen(self):
        query = _FakeQuery(self.sub_id, f"amg_transfer:{self.owner_id}")
        await admin_management.admin_management_callback_handler(
            _FakeUpdate(query), _FakeContext()
        )
        self.assertIn("نقل الملكية", query.last_text or "")


# ---------------------------------------------------------------
# 2. Granular permissions + admin panel gating
# ---------------------------------------------------------------


class GranularPermissionTests(PlatformUpdateBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await database.update_admin_permissions(
            self.sub_id, {k: True for k in database.PERMISSION_KEYS}
        )

    async def test_new_permission_keys_exist(self):
        for key in ("can_notifications", "can_settings", "can_topics"):
            self.assertIn(key, database.PERMISSION_KEYS)
            self.assertIn(key, database.PERMISSION_LABELS)

    async def test_grant_and_revoke_new_permission(self):
        perms = dict(await database.get_admin_permissions(self.sub_id))
        perms["can_notifications"] = False
        await database.update_admin_permissions(self.sub_id, perms)
        self.assertFalse(
            await database.user_has_permission(self.sub_id, "can_notifications")
        )
        perms["can_notifications"] = True
        await database.update_admin_permissions(self.sub_id, perms)
        self.assertTrue(
            await database.user_has_permission(self.sub_id, "can_notifications")
        )

    async def _panel_callbacks(self, user_id):
        query = _FakeQuery(user_id, "admin")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        return [
            b.callback_data
            for row in query.last_markup.inline_keyboard
            for b in row
        ]

    async def test_owner_panel_lists_new_surfaces_once(self):
        callbacks = await self._panel_callbacks(self.owner_id)
        self.assertEqual(callbacks.count("admin_notifications"), 1)
        self.assertEqual(callbacks.count("admin_settings"), 1)
        self.assertEqual(callbacks.count("admin_topics"), 1)

    async def test_admin_panel_hides_revoked_surfaces(self):
        perms = dict(await database.get_admin_permissions(self.sub_id))
        perms["can_notifications"] = False
        perms["can_settings"] = False
        perms["can_topics"] = False
        await database.update_admin_permissions(self.sub_id, perms)

        callbacks = await self._panel_callbacks(self.sub_id)
        self.assertNotIn("admin_notifications", callbacks)
        self.assertNotIn("admin_settings", callbacks)
        self.assertNotIn("admin_topics", callbacks)


# ---------------------------------------------------------------
# 3. Section / branch move over the hierarchy
# ---------------------------------------------------------------


class SectionMoveTests(PlatformUpdateBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.a = await database.add_folder(0, "A", "general")
        self.b = await database.add_folder(0, "B", "general")
        self.a_child = await database.add_folder(self.a, "A-Child", "general")
        self.a_grand = await database.add_folder(self.a_child, "A-Grand", "general")

    async def test_move_section_into_another_section(self):
        ok, _ = await database.move_folder(self.b, self.a)
        self.assertTrue(ok)
        self.assertEqual(await database.get_parent_id(self.b), self.a)

    async def test_move_into_descendant_rejected(self):
        ok, _ = await database.move_folder(self.a, self.a_grand)
        self.assertFalse(ok)
        self.assertEqual(await database.get_parent_id(self.a), 0)

    async def test_self_move_rejected(self):
        ok, _ = await database.move_folder(self.a, self.a)
        self.assertFalse(ok)

    async def test_move_to_root(self):
        ok, _ = await database.move_folder(self.a_child, 0)
        self.assertTrue(ok)
        self.assertEqual(await database.get_parent_id(self.a_child), 0)

    async def test_move_preserves_children_and_resources(self):
        await database.add_content(self.b, "Res", "fid-b", "document")
        b_child = await database.add_folder(self.b, "B-Child", "general")

        ok, _ = await database.move_folder(self.b, self.a)
        self.assertTrue(ok)

        self.assertEqual(len(await database.get_folders(self.b)), 1)
        self.assertEqual(await database.get_parent_id(b_child), self.b)
        self.assertEqual(len(await database.get_files(self.b)), 1)

    async def test_move_menu_offers_root_and_sections(self):
        ctx = _FakeContext()
        query = _FakeQuery(self.owner_id, f"admin_folder_move:{self.b}")
        await main.callback_router(_FakeUpdate(query), ctx)

        callbacks = [
            b.callback_data
            for row in query.last_markup.inline_keyboard
            for b in row
        ]
        self.assertIn(f"admin_folder_move_to:{self.b}:0", callbacks)
        self.assertIn(f"admin_folder_move_to:{self.b}:{self.a}", callbacks)

    async def test_move_menu_excludes_self_and_descendants(self):
        ctx = _FakeContext()
        query = _FakeQuery(self.owner_id, f"admin_folder_move:{self.a}")
        await main.callback_router(_FakeUpdate(query), ctx)

        callbacks = [
            b.callback_data
            for row in query.last_markup.inline_keyboard
            for b in row
        ]
        self.assertNotIn(f"admin_folder_move_to:{self.a}:{self.a}", callbacks)
        self.assertNotIn(
            f"admin_folder_move_to:{self.a}:{self.a_child}", callbacks
        )
        self.assertNotIn(
            f"admin_folder_move_to:{self.a}:{self.a_grand}", callbacks
        )

    async def test_move_browse_descends_into_a_branch(self):
        ctx = _FakeContext()
        query = _FakeQuery(
            self.owner_id, f"admin_folder_move_browse:{self.b}:{self.a}"
        )
        await main.callback_router(_FakeUpdate(query), ctx)

        callbacks = [
            b.callback_data
            for row in query.last_markup.inline_keyboard
            for b in row
        ]
        self.assertIn(f"admin_folder_move_to:{self.b}:{self.a_child}", callbacks)

    async def test_move_to_root_via_router(self):
        ctx = _FakeContext()
        query = _FakeQuery(
            self.owner_id, f"admin_folder_move_to:{self.a_child}:0"
        )
        await main.callback_router(_FakeUpdate(query), ctx)
        self.assertEqual(await database.get_parent_id(self.a_child), 0)

    async def test_add_cycle_convenience_wrapper(self):
        # The picker uses this wrapper; a node must count as its own
        # descendant so a self/cycle destination is impossible.
        self.assertTrue(await database.is_descendant_of(self.a, self.a))
        self.assertTrue(await database.is_descendant_of(self.a, self.a_grand))
        self.assertFalse(await database.is_descendant_of(self.a, self.b))


# ---------------------------------------------------------------
# 4. Multiple resources per section
# ---------------------------------------------------------------


class MultipleResourceTests(PlatformUpdateBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.section = await database.add_folder(0, "Section", "general")

    async def _upload(self, name):
        ctx = _FakeContext()
        start = _FakeQuery(self.owner_id, f"admin_upload:{self.section}")
        await main.callback_router(_FakeUpdate(start), ctx)

        msg = _MediaMessage(document=_FakeDoc(f"fid-{name}", name))
        await main.admin_upload_media_handler(_MediaUpdate(self.owner_id, msg), ctx)

        confirm = _FakeQuery(self.owner_id, "admin_upload_confirm")
        await main.callback_router(_FakeUpdate(confirm), ctx)

    async def test_multiple_uploads_are_all_saved(self):
        for n in ("a.pdf", "b.pdf", "c.pdf"):
            await self._upload(n)
        files = await database.get_files(self.section)
        self.assertEqual(len(files), 3)
        self.assertEqual(sorted(f[1] for f in files), ["a.pdf", "b.pdf", "c.pdf"])

    async def test_all_resources_appear_to_users(self):
        for n in ("a.pdf", "b.pdf", "c.pdf"):
            await self._upload(n)

        query = _FakeQuery(self.student_id, f"folder:{self.section}")
        await main.callback_router(_FakeUpdate(query), _FakeContext())

        callbacks = [
            b.callback_data
            for row in query.last_markup.inline_keyboard
            for b in row
        ]
        self.assertEqual(len([c for c in callbacks if c.startswith("file:")]), 3)

    async def test_existing_resources_are_not_overwritten(self):
        await self._upload("first.pdf")
        before = {f[0] for f in await database.get_files(self.section)}
        await self._upload("second.pdf")
        after = await database.get_files(self.section)
        self.assertEqual(len(after), 2)
        self.assertTrue(before.issubset({f[0] for f in after}))

    async def test_all_resources_reachable_through_the_ui(self):
        for n in ("a.pdf", "b.pdf", "c.pdf"):
            await self._upload(n)

        query = _FakeQuery(self.student_id, f"folder:{self.section}")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        file_buttons = [
            b.callback_data
            for row in query.last_markup.inline_keyboard
            for b in row
            if b.callback_data.startswith("file:")
        ]
        opened = set()
        for callback in file_buttons:
            record = await database.get_file_record(int(callback.split(":")[1]))
            self.assertIsNotNone(record)
            opened.add(record[0])
        self.assertEqual(len(opened), 3)


# ---------------------------------------------------------------
# 5. Platform settings
# ---------------------------------------------------------------


class PlatformSettingsTests(PlatformUpdateBase):
    async def test_defaults_when_unset(self):
        for key in database.PLATFORM_SETTING_KEYS:
            value = await database.get_platform_setting(key)
            self.assertTrue(value)
            self.assertEqual(value, database.PLATFORM_SETTING_DEFAULTS[key])

    async def test_update_persists(self):
        ok = await database.set_platform_setting("platform_name", "MEDBOT-Test")
        self.assertTrue(ok)
        self.assertEqual(
            await database.get_platform_setting("platform_name"), "MEDBOT-Test"
        )

    async def test_unknown_key_rejected(self):
        self.assertFalse(await database.set_platform_setting("nope", "x"))

    async def test_owner_can_edit_through_the_ui(self):
        ctx = _FakeContext()
        query = _FakeQuery(self.owner_id, "admin_settings")
        await platform_settings.platform_settings_callback_handler(
            _FakeUpdate(query), ctx
        )
        self.assertIn("إعدادات المنصة", query.last_text or "")

        edit = _FakeQuery(self.owner_id, "set_edit:platform_name")
        await platform_settings.platform_settings_callback_handler(
            _FakeUpdate(edit), ctx
        )
        self.assertEqual(ctx.user_data.get("settings_edit_key"), "platform_name")

        handled = await platform_settings.handle_settings_text(
            _MediaUpdate(self.owner_id, _TextMessage("منصة طبية تجريبية")), ctx
        )
        self.assertTrue(handled)
        self.assertEqual(
            await database.get_platform_setting("platform_name"),
            "منصة طبية تجريبية",
        )

    async def test_setting_persists_across_reconnect(self):
        await database.set_platform_setting("welcome_message", "أهلاً")
        self.assertEqual(
            await database.get_platform_setting("welcome_message"), "أهلاً"
        )

    async def test_setting_change_is_audited(self):
        ctx = _FakeContext()
        ctx.user_data["settings_edit_key"] = "help_text"
        await platform_settings.handle_settings_text(
            _MediaUpdate(self.owner_id, _TextMessage("نص مساعدة محدث")), ctx
        )
        rows = await database.get_audit_entries(action="platform_setting")
        self.assertEqual(len(rows), 1)

    async def test_unauthorized_admin_cannot_edit(self):
        perms = dict(await database.get_admin_permissions(self.sub_id))
        perms["can_settings"] = False
        await database.update_admin_permissions(self.sub_id, perms)

        query = _FakeQuery(self.sub_id, "admin_settings")
        await platform_settings.platform_settings_callback_handler(
            _FakeUpdate(query), _FakeContext()
        )
        self.assertIn("غير مصرح", query.last_text or "")

    async def test_platform_name_appears_on_home(self):
        await database.set_platform_setting("platform_name", "عيادة المنصة")
        query = _FakeQuery(self.student_id, "home")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        self.assertIn("عيادة المنصة", query.last_text or "")


# ---------------------------------------------------------------
# 6. Localization
# ---------------------------------------------------------------


class LocalizationTests(PlatformUpdateBase):
    async def test_default_language_is_arabic(self):
        await database.register_user(self.student_id, "s", "Student")
        self.assertEqual(await database.get_user_language(self.student_id), "ar")

    async def test_switch_and_persist(self):
        await database.register_user(self.student_id, "s", "Student")
        self.assertTrue(await database.set_user_language(self.student_id, "en"))
        self.assertEqual(await database.get_user_language(self.student_id), "en")
        self.assertTrue(await database.set_user_language(self.student_id, "ar"))
        self.assertEqual(await database.get_user_language(self.student_id), "ar")

    async def test_unknown_language_rejected(self):
        await database.register_user(self.student_id, "s", "Student")
        self.assertFalse(await database.set_user_language(self.student_id, "fr"))

    async def test_language_picker_roundtrip(self):
        await database.register_user(self.student_id, "s", "Student")
        ctx = _FakeContext()
        query = _FakeQuery(self.student_id, "language")
        await main.callback_router(_FakeUpdate(query), ctx)
        callbacks = [
            b.callback_data
            for row in query.last_markup.inline_keyboard
            for b in row
        ]
        self.assertIn("lang_set:en", callbacks)

        set_query = _FakeQuery(self.student_id, "lang_set:en")
        await main.callback_router(_FakeUpdate(set_query), ctx)
        self.assertEqual(await database.get_user_language(self.student_id), "en")

    async def test_major_interface_strings_localized(self):
        self.assertNotEqual(i18n.t("home", "ar"), i18n.t("home", "en"))
        self.assertIn("الرئيسية", i18n.t("home", "ar"))
        self.assertIn("Home", i18n.t("home", "en"))
        self.assertIn("المنصة", i18n.t("menu_contact", "ar"))
        self.assertIn("الصلاحيات", i18n.t("permissions_title", "ar"))
        self.assertIn("نقل الملكية", i18n.t("ownership_transfer", "ar"))
        self.assertIn("سجل التدقيق", i18n.t("admin_audit", "ar"))

    async def test_missing_key_never_raises(self):
        self.assertEqual(i18n.t("does.not.exist", "ar"), "does.not.exist")


# ---------------------------------------------------------------
# 7. Search Topics
# ---------------------------------------------------------------


class SearchTopicTests(PlatformUpdateBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.physio = await database.add_folder(0, "Physiology", "general")
        await database.add_content(self.physio, "Lecture 1", "fid-1", "document")

    async def test_create_edit_deactivate_delete(self):
        topic_id = await database.add_topic("علم وظائف الأعضاء", icon="🫀")
        self.assertIsNotNone(topic_id)

        self.assertTrue(await database.update_topic(topic_id, name="الفسيولوجي"))
        self.assertEqual((await database.get_topic(topic_id))["name"], "الفسيولوجي")

        await database.update_topic(topic_id, active=False)
        active = await database.get_topics(active_only=True)
        self.assertNotIn(topic_id, [t["id"] for t in active])

        self.assertTrue(await database.delete_topic(topic_id))
        self.assertIsNone(await database.get_topic(topic_id))

    async def test_link_and_resource_count(self):
        topic_id = await database.add_topic("Physiology")
        self.assertTrue(await database.link_topic_folder(topic_id, self.physio))
        self.assertEqual(await database.topic_resource_count(topic_id), 1)
        self.assertFalse(await database.link_topic_folder(topic_id, 999999))

    async def test_topic_appears_to_users_and_opens_content(self):
        topic_id = await database.add_topic("Physiology")
        await database.link_topic_folder(topic_id, self.physio)

        query = _FakeQuery(self.student_id, "topics")
        await topics.topics_callback_handler(_FakeUpdate(query), _FakeContext())
        callbacks = [
            b.callback_data
            for row in query.last_markup.inline_keyboard
            for b in row
        ]
        self.assertIn(f"topic_open:{topic_id}", callbacks)

        open_query = _FakeQuery(self.student_id, f"topic_open:{topic_id}")
        await topics.topics_callback_handler(_FakeUpdate(open_query), _FakeContext())
        open_callbacks = [
            b.callback_data
            for row in open_query.last_markup.inline_keyboard
            for b in row
        ]
        self.assertIn(f"folder:{self.physio}", open_callbacks)

    async def test_inactive_topic_hidden_from_users(self):
        topic_id = await database.add_topic("Hidden")
        await database.update_topic(topic_id, active=False)

        query = _FakeQuery(self.student_id, "topics")
        await topics.topics_callback_handler(_FakeUpdate(query), _FakeContext())
        callbacks = [
            b.callback_data
            for row in query.last_markup.inline_keyboard
            for b in row
        ]
        self.assertNotIn(f"topic_open:{topic_id}", callbacks)

    async def test_topic_creation_is_gated_and_audited(self):
        perms = dict(await database.get_admin_permissions(self.sub_id))
        perms["can_topics"] = False
        await database.update_admin_permissions(self.sub_id, perms)

        query = _FakeQuery(self.sub_id, "admin_topics")
        await topics.topics_callback_handler(_FakeUpdate(query), _FakeContext())
        self.assertIn("غير مصرح", query.last_text or "")

        ctx = _FakeContext()
        ctx.user_data["topics_create"] = True
        await topics.handle_topics_text(
            _MediaUpdate(self.owner_id, _TextMessage("علم التشريح")), ctx
        )
        rows = await database.get_audit_entries(action="topic_create")
        self.assertEqual(len(rows), 1)

    async def test_deleting_topic_keeps_folders_and_resources(self):
        topic_id = await database.add_topic("Physiology")
        await database.link_topic_folder(topic_id, self.physio)
        await database.delete_topic(topic_id)
        self.assertIsNotNone(await database.get_folder(self.physio))
        self.assertEqual(len(await database.get_files(self.physio)), 1)


# ---------------------------------------------------------------
# 8. Notifications
# ---------------------------------------------------------------


class NotificationTests(PlatformUpdateBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await database.register_user(self.student_id, "s", "Student")
        await database.register_user(901, "s2", "Student2")

    async def test_broadcast_delivers_and_records(self):
        bot = _FakeContext().bot
        recipients, delivered = await notifications.broadcast_notification(
            bot, "اختبار إشعار", sender_id=self.owner_id
        )
        self.assertEqual(recipients, 2)
        self.assertEqual(delivered, 2)
        self.assertEqual(await database.get_notifications_count(), 1)

    async def test_broadcast_survives_blocked_recipients(self):
        class _BrokenBot:
            async def send_message(self, **kwargs):
                raise RuntimeError("blocked")

        recipients, delivered = await notifications.broadcast_notification(
            _BrokenBot(), "x", sender_id=self.owner_id
        )
        self.assertEqual(recipients, 2)
        self.assertEqual(delivered, 0)
        self.assertEqual(await database.get_notifications_count(), 1)

    async def test_owner_sees_notifications_surface(self):
        query = _FakeQuery(self.owner_id, "admin_notifications")
        await notifications.notifications_callback_handler(
            _FakeUpdate(query), _FakeContext()
        )
        self.assertIn("الإشعارات", query.last_text or "")

    async def test_unauthorized_cannot_open_notifications(self):
        perms = dict(await database.get_admin_permissions(self.sub_id))
        perms["can_notifications"] = False
        await database.update_admin_permissions(self.sub_id, perms)

        query = _FakeQuery(self.sub_id, "admin_notifications")
        await notifications.notifications_callback_handler(
            _FakeUpdate(query), _FakeContext()
        )
        self.assertIn("غير مصرح", query.last_text or "")

        query2 = _FakeQuery(self.student_id, "admin_notifications")
        await notifications.notifications_callback_handler(
            _FakeUpdate(query2), _FakeContext()
        )
        self.assertIn("غير مصرح", query2.last_text or "")

    async def test_sending_notification_is_audited(self):
        ctx = _FakeContext()
        ctx.user_data["notifications_body"] = True
        handled = await notifications.handle_notification_text(
            _MediaUpdate(self.owner_id, _TextMessage("إشعار مهم")), ctx
        )
        self.assertTrue(handled)
        rows = await database.get_audit_entries(action="notification_send")
        self.assertEqual(len(rows), 1)


# ---------------------------------------------------------------
# 9. Message persistence
# ---------------------------------------------------------------


class MessagePersistenceTests(PlatformUpdateBase):
    async def test_send_safe_message_registers_content(self):
        update = _MediaUpdate(self.student_id, _ReplyTrackedMessage())
        await main.send_safe_message(update, "إجابة مهمة", None)
        self.assertTrue(main._is_content_message(self.student_id, 2001))

    async def test_navigation_cannot_overwrite_content_message(self):
        sent_texts = []

        class _ContentMessage:
            chat_id = 111
            message_id = 777

            async def reply_text(self, text, parse_mode=None, reply_markup=None):
                sent_texts.append(text)
                return _FakeSent(778)

        main._register_content_message(self.student_id, 777)
        query = _FakeQuery(self.student_id, "home")
        query.message = _ContentMessage()

        await main.edit_safe(query, "شاشة تنقل جديدة", None)
        # A NEW message was sent; the delivered content was not edited in place.
        self.assertEqual(sent_texts, ["شاشة تنقل جديدة"])
        self.assertIsNone(query.last_text)

    async def test_plain_navigation_still_edits_in_place(self):
        query = _FakeQuery(self.student_id, "home")
        await main.edit_safe(query, "شاشة تنقل", None)
        self.assertEqual(query.last_text, "شاشة تنقل")

    async def test_search_results_registered_as_content(self):
        folder = await database.add_folder(0, "Anatomy", "general")
        await database.add_content(folder, "Lecture", "fid-l", "document")

        update = _MediaUpdate(self.student_id, _ReplyTrackedMessage(text="Anatomy"))
        await main.run_search(update, "Anatomy")
        self.assertTrue(main._is_content_message(self.student_id, 2001))


if __name__ == "__main__":
    unittest.main()
