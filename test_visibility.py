"""Tests for navigation visibility (إظهار/إخفاء الأقسام) and role scope.

Runs against a temporary SQLite database only: never touches the real DB.
Covers the persisted hidden-feature set, the home-keyboard effect, the callback
gate (including the admin bypass), and the role/permission presets.
"""

import os
import tempfile
import unittest

import database
import main
import messaging
import topics
import visibility
from test_medbot_router import (
    _FakeContext,
    _FakeQuery,
    _FakeUpdate,
)


class VisibilityBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-visibility-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        database.DB_NAME = self.db_path
        await database.init_db()

        self.owner_id = 500
        self.sub_id = 501
        self.student_id = 900

        await database.add_sub_admin(self.owner_id, "owner")
        await database.add_sub_admin(self.sub_id, "sub")
        await database.ensure_configured_admin(self.owner_id)

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        for name in os.listdir(self.tmp_dir):
            try:
                os.remove(os.path.join(self.tmp_dir, name))
            except OSError:
                pass
        os.rmdir(self.tmp_dir)


class HiddenFeatureStoreTests(VisibilityBase):
    async def test_default_is_all_visible(self):
        self.assertEqual(await database.get_hidden_features(), set())
        for feature in database.FEATURES:
            self.assertFalse(await database.is_feature_hidden(feature))

    async def test_set_and_read_back(self):
        await database.set_hidden_features({"assistant", "topics"})
        self.assertEqual(
            await database.get_hidden_features(), {"assistant", "topics"}
        )
        self.assertTrue(await database.is_feature_hidden("assistant"))
        self.assertFalse(await database.is_feature_hidden("account"))

    async def test_unknown_keys_are_ignored(self):
        await database.set_hidden_features({"assistant", "not_a_feature"})
        self.assertEqual(await database.get_hidden_features(), {"assistant"})

    async def test_unknown_feature_is_never_hidden(self):
        self.assertFalse(await database.is_feature_hidden("bogus"))


class HomeKeyboardVisibilityTests(VisibilityBase):
    @staticmethod
    def _callbacks(markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    async def test_hidden_feature_removed_from_public_keyboard(self):
        await database.set_hidden_features({"assistant", "contact"})
        hidden = await database.get_hidden_features()
        callbacks = self._callbacks(main.home_keyboard("ar", hidden))
        self.assertNotIn("assistant", callbacks)
        self.assertNotIn("contact", callbacks)
        self.assertIn("account", callbacks)

    async def test_all_hidden_still_yields_a_usable_keyboard(self):
        hidden = set(database.FEATURES) - {"admin_panel"}
        markup = main.home_keyboard("ar", hidden)
        self.assertEqual(self._callbacks(markup), ["home"])

    async def test_owner_keeps_admin_entry_even_when_hidden(self):
        await database.set_hidden_features({"admin_panel"})
        query = _FakeQuery(self.owner_id, "home")
        callbacks = self._callbacks(await main.home_for(_FakeUpdate(query)))
        self.assertIn("admin", callbacks)

    async def test_sub_admin_admin_entry_is_soft_hidden(self):
        await database.set_hidden_features({"admin_panel"})
        query = _FakeQuery(self.sub_id, "home")
        callbacks = self._callbacks(await main.home_for(_FakeUpdate(query)))
        self.assertNotIn("admin", callbacks)


class CallbackGateTests(VisibilityBase):
    async def _route(self, user_id, data):
        query = _FakeQuery(user_id, data)
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        return query

    async def test_hidden_feature_blocks_student_callback(self):
        await database.set_hidden_features({"account"})
        query = await self._route(self.student_id, "account")
        self.assertIn("غير متاح مؤقتاً", query.last_text)

    async def test_hidden_feature_blocks_resources_callback(self):
        await database.set_hidden_features({"resources"})
        query = await self._route(self.student_id, "library:0")
        self.assertIn("غير متاح مؤقتاً", query.last_text)

    async def test_admin_bypasses_hidden_gate(self):
        await database.set_hidden_features({"account"})
        query = await self._route(self.owner_id, "account")
        self.assertNotIn("غير متاح مؤقتاً", query.last_text)

    async def test_visible_feature_is_unaffected(self):
        await database.set_hidden_features({"topics"})
        query = await self._route(self.student_id, "account")
        self.assertNotIn("غير متاح مؤقتاً", query.last_text)

    async def test_hidden_contact_blocked_in_own_handler(self):
        await database.set_hidden_features({"contact"})
        query = _FakeQuery(self.student_id, "contact")
        await messaging.messaging_callback_handler(
            _FakeUpdate(query), _FakeContext()
        )
        self.assertIn("غير متاح مؤقتاً", query.last_text)

    async def test_hidden_topics_blocked_in_own_handler(self):
        await database.set_hidden_features({"topics"})
        query = _FakeQuery(self.student_id, "topics")
        await topics.topics_callback_handler(
            _FakeUpdate(query), _FakeContext()
        )
        self.assertIn("غير متاح مؤقتاً", query.last_text)

    async def test_admin_bypasses_topics_gate(self):
        await database.set_hidden_features({"topics"})
        query = _FakeQuery(self.owner_id, "topics")
        await topics.topics_callback_handler(
            _FakeUpdate(query), _FakeContext()
        )
        self.assertNotIn("غير متاح مؤقتاً", query.last_text)

    async def test_role_ui_hidden_feature_map_covers_home_entries(self):
        # Every home entry must map to a real gate key, otherwise a hidden
        # button would still be reachable.
        for row in main._home_rows():
            for feature, _label, callback in row:
                self.assertEqual(
                    main._feature_for_callback(callback), feature, callback
                )

    async def test_news_subscription_callbacks_map_to_news(self):
        # The subscription tree uses news_pick_child/news_pick_root to descend
        # and news_subs_section to toggle; a hidden news feature must block all
        # of them, not only the first screen.
        for callback in (
            "news",
            "news_subs",
            "news_sub:section",
            "news_unsub:section",
            "news_subs_section:5",
            "news_pick_child:5",
            "news_pick_root:0",
            "news_open:3",
            "news_more:1",
        ):
            self.assertEqual(main._feature_for_callback(callback), "news", callback)


class VisibilityAdminSurfaceTests(VisibilityBase):
    async def _manage(self, user_id, data):
        query = _FakeQuery(user_id, data)
        await visibility.visibility_callback_handler(
            _FakeUpdate(query), _FakeContext()
        )
        return query

    async def test_owner_can_open_visibility_screen(self):
        query = await self._manage(self.owner_id, "vis_list")
        self.assertIn("إظهار وإخفاء الأقسام", query.last_text)

    async def test_student_cannot_open_visibility_screen(self):
        query = await self._manage(self.student_id, "vis_list")
        self.assertIn("غير مصرح", query.last_text)

    async def test_admin_without_permission_denied(self):
        perms = {key: False for key in database.PERMISSION_KEYS}
        await database.update_admin_permissions(self.sub_id, perms)
        query = await self._manage(self.sub_id, "vis_list")
        self.assertIn("غير مصرح", query.last_text)

    async def test_toggle_hides_and_shows_and_audits(self):
        await self._manage(self.owner_id, "vis_toggle:assistant")
        self.assertTrue(await database.is_feature_hidden("assistant"))
        entries = await database.get_audit_entries(action="feature_visibility")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][5], "assistant")

        await self._manage(self.owner_id, "vis_toggle:assistant")
        self.assertFalse(await database.is_feature_hidden("assistant"))
        self.assertEqual(
            len(await database.get_audit_entries(action="feature_visibility")), 2
        )

    async def test_show_all_restores_everything(self):
        await database.set_hidden_features({"assistant", "topics"})
        query = await self._manage(self.owner_id, "vis_showall")
        self.assertEqual(await database.get_hidden_features(), set())
        self.assertIn("إظهار وإخفاء الأقسام", query.last_text)

    async def test_unknown_toggle_fails_safely(self):
        query = await self._manage(self.owner_id, "vis_toggle:bogus")
        self.assertIn("غير معروف", query.last_text)

    async def test_new_permission_key_exists(self):
        self.assertIn("can_visibility", database.PERMISSION_KEYS)
        self.assertIn("can_visibility", database.PERMISSION_LABELS)

    async def test_owner_panel_lists_visibility_surface_once(self):
        query = _FakeQuery(self.owner_id, "admin")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        callbacks = [
            b.callback_data
            for row in query.last_markup.inline_keyboard
            for b in row
        ]
        self.assertEqual(callbacks.count("vis_list"), 1)


class RoleScopeTests(VisibilityBase):
    async def test_role_presets_cover_every_permission_key(self):
        for role, preset in database.ROLE_PERMISSION_PRESETS.items():
            self.assertEqual(set(preset), set(database.PERMISSION_KEYS), role)

    async def test_reviewer_scope_is_limited(self):
        await database.apply_role_preset(self.sub_id, "reviewer")
        record = await database.get_admin_record(self.sub_id)
        self.assertEqual(record["role"], "reviewer")
        self.assertTrue(record["permissions"]["can_contributions"])
        self.assertTrue(record["permissions"]["can_messages"])
        self.assertFalse(record["permissions"]["can_folders"])
        self.assertFalse(record["permissions"]["can_settings"])

    async def test_admin_scope_excludes_admin_management(self):
        await database.apply_role_preset(self.sub_id, "admin")
        record = await database.get_admin_record(self.sub_id)
        self.assertFalse(record["permissions"]["can_admins"])
        self.assertTrue(record["permissions"]["can_folders"])

    async def test_owner_role_cannot_be_applied_as_preset(self):
        self.assertFalse(await database.apply_role_preset(self.sub_id, "owner"))
        self.assertFalse(await database.is_owner(self.sub_id))

    async def test_transfer_demotes_previous_owner_to_admin_scope(self):
        await database.transfer_ownership(self.owner_id, self.sub_id)
        record = await database.get_admin_record(self.owner_id)
        self.assertEqual(record["role"], "admin")
        self.assertFalse(record["permissions"]["can_admins"])

    async def test_transfer_grants_new_owner_full_scope(self):
        await database.transfer_ownership(self.owner_id, self.sub_id)
        for key in database.PERMISSION_KEYS:
            self.assertTrue(
                await database.user_has_permission(self.sub_id, key), key
            )

    async def test_every_role_has_a_description(self):
        for role in ("owner", "admin", "reviewer", "none"):
            self.assertTrue(database.ROLE_DESCRIPTIONS.get(role), role)


class LegacyMultipleOwnerHealTests(unittest.IsolatedAsyncioTestCase):
    """A DB left with two owners is healed to exactly one on startup."""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-ownerheal-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        database.DB_NAME = self.db_path
        await database.init_db()

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        for name in os.listdir(self.tmp_dir):
            try:
                os.remove(os.path.join(self.tmp_dir, name))
            except OSError:
                pass
        os.rmdir(self.tmp_dir)

    async def test_two_owners_are_reduced_to_one(self):
        db = await database.get_db()
        await db.execute(
            "INSERT INTO admins (telegram_id, username, role) "
            "VALUES (700, 'a', 'owner')"
        )
        await db.execute(
            "INSERT INTO admins (telegram_id, username, role) "
            "VALUES (701, 'b', 'owner')"
        )
        await db.commit()
        await db.close()

        # The persisted owner (if any) wins; otherwise the earliest row does.
        await database.init_db()

        owners = [
            r["telegram_id"]
            for r in await database.get_admins_full_records()
            if r["role"] == "owner"
        ]
        self.assertEqual(len(owners), 1)
        self.assertEqual(owners[0], 700)
