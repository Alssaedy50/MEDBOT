"""Tests for RBAC (owner + sub-admin) and the isolated audit log.

Runs against a temporary SQLite database only: never touches the real DB.
Covers migration idempotency, backward compatibility for pre-RBAC admins,
the permission truth table, owner bootstrap safety, and audit isolation.
"""

import os
import tempfile
import unittest

import admin_management
import audit
import database
import main
import messaging
from test_medbot_router import (
    _FakeContext,
    _FakeQuery,
    _FakeUpdate,
    _TextMessage,
    _MediaUpdate,
)


class RBACBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-rbac-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        self._old_admin = os.environ.get("ADMIN_ID")
        database.DB_NAME = self.db_path
        await database.init_db()

        self.owner_id = 500
        self.sub_id = 501
        self.student_id = 900

        await database.add_sub_admin(self.owner_id, "owner")
        await database.add_sub_admin(self.sub_id, "sub")

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        if self._old_admin is None:
            os.environ.pop("ADMIN_ID", None)
        else:
            os.environ["ADMIN_ID"] = self._old_admin
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


class LegacyUpgradeTests(unittest.IsolatedAsyncioTestCase):
    """Upgrade a pre-RBAC database in place (no data loss, no reset)."""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-legacy-")
        self.db_path = os.path.join(self.tmp_dir, "legacy.sqlite3")
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

    async def _seed_legacy_db(self):
        """Build the old schema exactly as pre-RBAC MEDBOT left it."""
        db = await database.get_db()
        await db.execute(
            "CREATE TABLE admins ("
            "telegram_id INTEGER PRIMARY KEY, username TEXT, "
            "added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        await db.execute(
            "CREATE TABLE folders (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "parent_id INTEGER, name TEXT NOT NULL, "
            "node_type TEXT DEFAULT 'general')"
        )
        await db.execute(
            "INSERT INTO admins (telegram_id, username) VALUES (321, 'legacy')"
        )
        await db.execute(
            "INSERT INTO folders (parent_id, name, node_type) "
            "VALUES (NULL, 'Existing', 'general')"
        )
        await db.commit()
        await db.close()

    async def test_legacy_admin_and_folders_survive_upgrade(self):
        await self._seed_legacy_db()
        await database.init_db()

        # Pre-existing admin keeps admin status and full access.
        self.assertTrue(await database.is_user_admin(321))
        self.assertFalse(await database.is_owner(321))
        for key in database.PERMISSION_KEYS:
            self.assertTrue(await database.user_has_permission(321, key), key)

        # Pre-existing content is untouched.
        folders = await database.get_folders(0)
        self.assertEqual([f[1] for f in folders], ["Existing"])

    async def test_repeated_init_db_after_legacy_upgrade(self):
        await self._seed_legacy_db()
        for _ in range(3):
            await database.init_db()

        self.assertTrue(await database.is_user_admin(321))
        self.assertEqual(len(await database.get_folders(0)), 1)
        self.assertEqual(await database.get_audit_count(), 0)


# ---------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------


class MigrationTests(RBACBase):
    async def test_admins_has_role_and_permissions_columns(self):
        db = await database.get_db()
        async with db.execute("PRAGMA table_info(admins)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        await db.close()
        self.assertIn("role", cols)
        self.assertIn("permissions", cols)

    async def test_audit_log_table_and_indexes_exist(self):
        db = await database.get_db()
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_log'"
        ) as cur:
            self.assertIsNotNone(await cur.fetchone())
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_audit%'"
        ) as cur:
            names = {row[0] for row in await cur.fetchall()}
        await db.close()
        self.assertEqual(
            names, {"idx_audit_actor", "idx_audit_action", "idx_audit_created"}
        )

    async def test_init_db_is_idempotent(self):
        await database.add_folder(0, "Keep", "general")
        before = await database.get_folders(0)

        for _ in range(3):
            await database.init_db()

        after = await database.get_folders(0)
        self.assertEqual(len(before), len(after))

        db = await database.get_db()
        async with db.execute("PRAGMA integrity_check") as cur:
            self.assertEqual((await cur.fetchone())[0], "ok")
        async with db.execute("PRAGMA foreign_keys") as cur:
            await cur.fetchone()
        await db.close()

    async def test_existing_admin_rows_are_preserved(self):
        db = await database.get_db()
        async with db.execute(
            "SELECT telegram_id, role FROM admins WHERE telegram_id = ?",
            (self.sub_id,),
        ) as cur:
            row = await cur.fetchone()
        await db.close()
        self.assertEqual(row[0], self.sub_id)
        # Pre-existing admins default to 'admin', not owner.
        self.assertNotEqual(row[1], "owner")

    async def test_migration_v7_backfills_null_role(self):
        # A legacy row written before RBAC (role NULL) becomes an active admin.
        db = await database.get_db()
        await db.execute(
            "INSERT INTO admins (telegram_id, username, role) "
            "VALUES (?, ?, NULL)",
            (self.student_id + 1, "legacy-null"),
        )
        await db.commit()
        await db.close()

        await database.init_db()

        record = await database.get_admin_record(self.student_id + 1)
        self.assertEqual(record["role"], "admin")
        self.assertTrue(await database.is_user_admin(self.student_id + 1))


# ---------------------------------------------------------------
# Permission resolution / backward compatibility
# ---------------------------------------------------------------


class PermissionTests(RBACBase):
    async def test_preexisting_admin_keeps_full_access(self):
        # Empty permissions column must resolve to all-granted.
        record = await database.get_admin_record(self.sub_id)
        self.assertEqual(record["permissions_raw"], "")
        self.assertTrue(all(record["permissions"].values()))

        for key in database.PERMISSION_KEYS:
            self.assertTrue(
                await database.user_has_permission(self.sub_id, key), key
            )

    async def test_is_user_admin_unchanged_for_owner_and_admin(self):
        self.assertTrue(await database.is_user_admin(self.sub_id))
        await database.ensure_configured_admin(self.owner_id)
        self.assertTrue(await database.is_user_admin(self.owner_id))

    async def test_permissions_string_roundtrip(self):
        original = {key: True for key in database.PERMISSION_KEYS}
        original["can_ai"] = False
        encoded = database.permissions_to_string(original)
        self.assertNotIn("can_ai", encoded.split(","))
        decoded = database.permissions_from_string(encoded)
        self.assertFalse(decoded["can_ai"])
        self.assertTrue(decoded["can_folders"])

    async def test_revoked_everything_is_not_legacy_full_access(self):
        # All-False must round-trip as all-False, not fall back to "all granted".
        revoked = {key: False for key in database.PERMISSION_KEYS}
        encoded = database.permissions_to_string(revoked)
        self.assertTrue(encoded.strip())
        self.assertFalse(any(database.permissions_from_string(encoded).values()))

    async def test_empty_string_means_all_granted(self):
        self.assertTrue(
            all(database.permissions_from_string("").values())
        )
        self.assertTrue(
            all(database.permissions_from_string(None).values())
        )

    async def test_user_has_permission_truth_table(self):
        await database.ensure_configured_admin(self.owner_id)

        # Owner: always true, even for an unknown-but-valid key.
        for key in database.PERMISSION_KEYS:
            self.assertTrue(await database.user_has_permission(self.owner_id, key))

        # Non-admin: never.
        self.assertFalse(
            await database.user_has_permission(self.student_id, "can_folders")
        )

        # Unknown permission key: never, even for the owner.
        self.assertFalse(
            await database.user_has_permission(self.owner_id, "can_does_not_exist")
        )

    async def test_admin_with_revoked_capability_denied_only_that_one(self):
        perms = {key: True for key in database.PERMISSION_KEYS}
        perms["can_ai"] = False
        await database.update_admin_permissions(self.sub_id, perms)

        self.assertFalse(await database.user_has_permission(self.sub_id, "can_ai"))
        self.assertTrue(
            await database.user_has_permission(self.sub_id, "can_folders")
        )

    async def test_unknown_role_is_rejected(self):
        self.assertFalse(await database.set_admin_role(self.sub_id, "superuser"))
        record = await database.get_admin_record(self.sub_id)
        self.assertIn(record["role"], database.ROLES)

    async def test_update_admin_permissions_ignores_unknown_keys(self):
        await database.update_admin_permissions(
            self.sub_id, {"can_ai": True, "bogus": True}
        )
        record = await database.get_admin_record(self.sub_id)
        self.assertEqual(
            set(record["permissions"].keys()), set(database.PERMISSION_KEYS)
        )


# ---------------------------------------------------------------
# Owner bootstrap safety
# ---------------------------------------------------------------


class OwnerBootstrapTests(RBACBase):
    async def test_zero_or_negative_never_grants(self):
        self.assertFalse(await database.ensure_configured_admin(0))
        self.assertFalse(await database.ensure_configured_admin(-5))
        self.assertFalse(await database.is_owner(0))
        self.assertFalse(await database.is_owner(-1))
        self.assertFalse(await database.user_has_permission(0, "can_folders"))

    async def test_configured_id_becomes_owner(self):
        ok = await database.ensure_configured_admin(self.owner_id)
        self.assertTrue(ok)
        self.assertTrue(await database.is_owner(self.owner_id))
        record = await database.get_admin_record(self.owner_id)
        self.assertEqual(record["role"], "owner")
        self.assertTrue(all(record["permissions"].values()))

    async def test_bootstrap_is_idempotent(self):
        await database.ensure_configured_admin(self.owner_id)
        await database.ensure_configured_admin(self.owner_id)
        owners = [
            r for r in
            [await database.get_admin_record(self.owner_id)]
            if r["role"] == "owner"
        ]
        self.assertEqual(len(owners), 1)

    async def test_changing_admin_id_demotes_previous_owner(self):
        await database.ensure_configured_admin(self.owner_id)
        self.assertTrue(await database.is_owner(self.owner_id))

        # A new configured owner must leave exactly one owner behind.
        await database.ensure_configured_admin(self.sub_id)
        self.assertTrue(await database.is_owner(self.sub_id))
        self.assertFalse(await database.is_owner(self.owner_id))


# ---------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------


class AuditTests(RBACBase):
    async def test_log_action_records_actor_action_target_and_time(self):
        await database.ensure_configured_admin(self.owner_id)

        ok = await audit.log_action(
            self.owner_id,
            "folder_create",
            target_type="folder",
            target_id=7,
            details="name=Test",
        )
        self.assertTrue(ok)

        rows = await database.get_audit_entries(limit=10)
        self.assertEqual(len(rows), 1)
        _id, actor_id, actor_role, action, ttype, tid, details, created = rows[0]
        self.assertEqual(actor_id, self.owner_id)
        self.assertEqual(actor_role, "owner")
        self.assertEqual(action, "folder_create")
        self.assertEqual(ttype, "folder")
        self.assertEqual(tid, "7")
        self.assertEqual(details, "name=Test")
        self.assertIsNotNone(created)

    async def test_log_action_never_raises_on_bad_input(self):
        # None actor, None action, odd types: must not raise into the caller.
        await audit.log_action(None, "folder_create")
        await audit.log_action("not-an-int", "folder_create")
        await audit.log_action(self.sub_id, None)
        await audit.log_action(self.sub_id, "folder_create", target_id=object())

    async def test_get_audit_entries_filters(self):
        await audit.log_action(self.sub_id, "folder_create", "folder", 1)
        await audit.log_action(self.sub_id, "content_delete", "content", 2)

        only_folder = await database.get_audit_entries(action="folder_create")
        self.assertEqual(len(only_folder), 1)
        self.assertEqual(only_folder[0][3], "folder_create")

        by_actor = await database.get_audit_entries(actor_id=self.sub_id)
        self.assertEqual(len(by_actor), 2)

    async def test_get_audit_entries_survives_bad_limit(self):
        await audit.log_action(self.sub_id, "folder_create")
        for bad in ("abc", None, -1, 10 ** 9):
            rows = await database.get_audit_entries(limit=bad)
            self.assertIsInstance(rows, list)

    async def test_audit_is_isolated_from_messages_and_contributions(self):
        await database.create_message(self.student_id, "S", "message", "hi")
        await audit.log_action(self.sub_id, "message_reply", "message", 1)

        # Audit write must not alter the messaging table contents.
        db = await database.get_db()
        async with db.execute("SELECT COUNT(*) FROM messages") as cur:
            self.assertEqual((await cur.fetchone())[0], 1)
        await db.close()

        rows = await database.get_audit_entries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], "message_reply")

    async def test_all_declared_actions_have_labels(self):
        for key in audit.AUDIT_ACTIONS:
            self.assertIn(key, audit.ACTION_LABELS)


# ---------------------------------------------------------------
# Audit viewer authorization
# ---------------------------------------------------------------


class AuditViewerTests(RBACBase):
    async def _view(self, user_id, data="audit_log"):
        query = _FakeQuery(user_id, data)
        await audit.audit_callback_handler(_FakeUpdate(query), _FakeContext())
        return query

    async def test_non_admin_cannot_open_viewer(self):
        query = await self._view(self.student_id)
        self.assertIn("غير مصرح", query.last_text)

    async def test_owner_can_open_viewer(self):
        await database.ensure_configured_admin(self.owner_id)
        await audit.log_action(self.owner_id, "folder_create", "folder", 1)
        query = await self._view(self.owner_id)
        self.assertNotIn("غير مصرح", query.last_text)
        self.assertIn("سجل التدقيق", query.last_text)

    async def test_admin_without_can_admins_denied(self):
        perms = {key: True for key in database.PERMISSION_KEYS}
        perms["can_admins"] = False
        await database.update_admin_permissions(self.sub_id, perms)
        query = await self._view(self.sub_id)
        self.assertIn("غير مصرح", query.last_text)

    async def test_admin_with_can_admins_allowed(self):
        perms = {key: True for key in database.PERMISSION_KEYS}
        await database.update_admin_permissions(self.sub_id, perms)
        query = await self._view(self.sub_id)
        self.assertNotIn("غير مصرح", query.last_text)

    async def test_malformed_audit_callbacks_fail_safely(self):
        await database.ensure_configured_admin(self.owner_id)
        for data in ("audit_act:", "audit_act:bogus_action", "audit_log"):
            query = await self._view(self.owner_id, data)
            self.assertTrue(query.answered, data)


# ---------------------------------------------------------------
# Admin management UI
# ---------------------------------------------------------------


class AdminManagementTests(RBACBase):
    async def _manage(self, user_id, data, context=None):
        query = _FakeQuery(user_id, data)
        ctx = context or _FakeContext()
        await admin_management.admin_management_callback_handler(
            _FakeUpdate(query), ctx
        )
        return query, ctx

    async def test_non_admin_cannot_manage(self):
        query, _ = await self._manage(self.student_id, "amg_list")
        self.assertIn("غير مصرح", query.last_text)

    async def test_owner_can_list_admins(self):
        await database.ensure_configured_admin(self.owner_id)
        query, _ = await self._manage(self.owner_id, "amg_list")
        self.assertNotIn("غير مصرح", query.last_text)
        self.assertIn("إدارة المشرفين", query.last_text)

    async def test_owner_can_toggle_permission_and_it_is_audited(self):
        await database.ensure_configured_admin(self.owner_id)

        before = await database.get_admin_record(self.sub_id)
        key = "can_ai"
        self.assertTrue(before["permissions"][key])

        await self._manage(self.owner_id, f"amg_perm:{self.sub_id}:{key}")

        after = await database.get_admin_record(self.sub_id)
        self.assertFalse(after["permissions"][key])

        entries = await database.get_audit_entries(action="admin_permissions")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][4], "admin")  # target_type
        self.assertEqual(entries[0][5], str(self.sub_id))

    async def test_owner_can_change_role_and_it_is_audited(self):
        await database.ensure_configured_admin(self.owner_id)
        await self._manage(self.owner_id, f"amg_role:{self.sub_id}:reviewer")

        record = await database.get_admin_record(self.sub_id)
        self.assertEqual(record["role"], "reviewer")
        entries = await database.get_audit_entries(action="admin_role")
        self.assertEqual(len(entries), 1)

    async def test_changing_role_applies_its_permission_scope(self):
        await database.ensure_configured_admin(self.owner_id)

        await self._manage(self.owner_id, f"amg_role:{self.sub_id}:reviewer")
        record = await database.get_admin_record(self.sub_id)
        self.assertTrue(record["permissions"]["can_contributions"])
        self.assertTrue(record["permissions"]["can_messages"])
        self.assertFalse(record["permissions"]["can_folders"])
        self.assertFalse(record["permissions"]["can_admins"])

        await self._manage(self.owner_id, f"amg_role:{self.sub_id}:admin")
        record = await database.get_admin_record(self.sub_id)
        self.assertTrue(record["permissions"]["can_folders"])
        self.assertTrue(record["permissions"]["can_content"])
        # `can_admins` is owner-only, so an admin never receives it.
        self.assertFalse(record["permissions"]["can_admins"])

    async def test_role_reference_screen_lists_all_roles(self):
        await database.ensure_configured_admin(self.owner_id)
        query, _ = await self._manage(self.owner_id, "amg_roles")
        text = query.last_text or ""
        for role in ("owner", "admin", "reviewer"):
            self.assertIn(database.ROLE_LABELS[role], text)

    async def test_owner_cannot_be_removed_via_ui(self):
        await database.ensure_configured_admin(self.owner_id)
        await self._manage(self.owner_id, f"amg_remove:{self.owner_id}")
        self.assertTrue(await database.is_owner(self.owner_id))
        self.assertTrue(await database.is_user_admin(self.owner_id))

    async def test_removing_sub_admin_is_audited(self):
        await database.ensure_configured_admin(self.owner_id)
        await self._manage(self.owner_id, f"amg_remove:{self.sub_id}")

        self.assertFalse(await database.is_user_admin(self.sub_id))
        entries = await database.get_audit_entries(action="admin_remove")
        self.assertEqual(len(entries), 1)

    async def test_add_admin_text_flow_grants_no_permissions(self):
        await database.ensure_configured_admin(self.owner_id)
        _, ctx = await self._manage(self.owner_id, "amg_add")

        msg = _TextMessage("777")
        update = _MediaUpdate(self.owner_id, msg)
        handled = await admin_management.handle_add_admin_text(update, ctx)
        self.assertTrue(handled)

        self.assertTrue(await database.is_user_admin(777))
        self.assertFalse(await database.user_has_permission(777, "can_folders"))
        self.assertFalse(await database.is_owner(777))

        entries = await database.get_audit_entries(action="admin_add")
        self.assertEqual(len(entries), 1)

    async def test_non_owner_cannot_mint_an_owner(self):
        await database.ensure_configured_admin(self.owner_id)
        perms = {key: True for key in database.PERMISSION_KEYS}
        await database.update_admin_permissions(self.sub_id, perms)

        await self._manage(self.sub_id, f"amg_role:{self.sub_id}:owner")
        record = await database.get_admin_record(self.sub_id)
        self.assertNotEqual(record["role"], "owner")

    async def test_malformed_admin_mgmt_callbacks_fail_safely(self):
        await database.ensure_configured_admin(self.owner_id)
        for data in (
            "amg_view:abc",
            "amg_perm:1",
            "amg_perm:abc:can_ai",
            "amg_role:abc:admin",
            "amg_remove:abc",
        ):
            query, _ = await self._manage(self.owner_id, data)
            self.assertTrue(query.answered, data)


# ---------------------------------------------------------------
# End-to-end: gated admin operations emit exactly one audit row
# ---------------------------------------------------------------


class AdminOperationAuditTests(RBACBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await database.ensure_configured_admin(self.owner_id)

        await database.add_folder(0, "Root", "general")
        self.root_id = (await database.get_folders(0))[0][0]
        await database.add_content(self.root_id, "Doc", "fid-1", "document")
        self.content_id = (await database.get_files(self.root_id))[0][0]

    async def test_folder_delete_writes_one_audit_row(self):
        # Deletion is only allowed for a completely empty folder.
        await database.add_folder(0, "Empty", "general")
        empty_id = (await database.get_folders(0))[-1][0]

        await self._route(self.owner_id, f"admin_folder_delete:{empty_id}")
        rows = await database.get_audit_entries(action="folder_delete")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], self.owner_id)
        self.assertEqual(rows[0][5], str(empty_id))

    async def test_content_delete_writes_one_audit_row(self):
        await self._route(self.owner_id, f"admin_file_delete:{self.content_id}")
        rows = await database.get_audit_entries(action="content_delete")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], self.owner_id)

    async def test_sub_admin_without_can_folders_is_denied_and_not_audited(self):
        perms = {key: True for key in database.PERMISSION_KEYS}
        perms["can_folders"] = False
        await database.update_admin_permissions(self.sub_id, perms)

        query, _ = await self._route(self.sub_id, "admin_folders")
        self.assertIn("غير مصرح", query.last_text)

        # The denied call must not delete anything nor log an operation.
        self.assertEqual(await database.get_audit_entries(action="folder_delete"), [])

    async def test_sub_admin_with_can_folders_is_allowed(self):
        perms = {key: True for key in database.PERMISSION_KEYS}
        await database.update_admin_permissions(self.sub_id, perms)

        query, _ = await self._route(self.sub_id, "admin_folders")
        self.assertNotIn("غير مصرح", query.last_text)


# ---------------------------------------------------------------
# Owner protection (the reported defect: owner demoted to sub-admin)
# ---------------------------------------------------------------


class OwnerProtectionTests(RBACBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await database.ensure_configured_admin(self.owner_id)

    async def _manage(self, user_id, data):
        """Route an admin-management callback through its real handler."""
        query = _FakeQuery(user_id, data)
        await admin_management.admin_management_callback_handler(
            _FakeUpdate(query), _FakeContext()
        )
        return query

    async def test_owner_cannot_be_demoted_by_role_change(self):
        # Owner tries to set their own role to a plain admin.
        query = await self._manage(self.owner_id, f"amg_role:{self.owner_id}:admin")
        self.assertTrue(await database.is_owner(self.owner_id))
        self.assertIn("لا يمكن تغيير دور المالك", query.last_text)

        # The demotion must not be written either.
        self.assertFalse(await database.set_admin_role(self.owner_id, "admin"))
        self.assertTrue(await database.is_owner(self.owner_id))

    async def test_owner_can_be_demoted_only_when_another_owner_exists(self):
        # A second owner can only be created by an explicit ownership transfer;
        # the transfer demotes the previous owner to a plain admin.
        query = await self._manage(self.owner_id, f"amg_transfer_confirm:{self.sub_id}")
        self.assertTrue(await database.is_owner(self.sub_id))
        self.assertFalse(await database.is_owner(self.owner_id))
        self.assertEqual(
            (await database.get_admin_record(self.owner_id))["role"], "admin"
        )
        self.assertIn("تم نقل الملكية", query.last_text)

    async def test_role_ui_cannot_mint_a_second_owner(self):
        # Promoting via the role screen is refused outright, so two owners can
        # never coexist.
        query = await self._manage(self.owner_id, f"amg_role:{self.sub_id}:owner")
        self.assertFalse(await database.is_owner(self.sub_id))
        self.assertTrue(await database.is_owner(self.owner_id))
        self.assertIn("نقل الملكية", query.last_text)

    async def test_transfer_only_when_actor_is_owner(self):
        # A non-owner admin (even with full permissions) cannot transfer.
        perms = {key: True for key in database.PERMISSION_KEYS}
        await database.update_admin_permissions(self.sub_id, perms)
        query = await self._manage(self.sub_id, f"amg_transfer_confirm:{self.owner_id}")
        self.assertTrue(await database.is_owner(self.owner_id))
        self.assertFalse(await database.is_owner(self.sub_id))
        self.assertIn("نقل الملكية متاح للمالك", query.last_text)

    async def test_owner_cannot_remove_themselves(self):
        query = await self._manage(self.owner_id, f"amg_remove:{self.owner_id}")
        self.assertTrue(await database.is_user_admin(self.owner_id))
        self.assertTrue(await database.is_owner(self.owner_id))
        self.assertIn("لا يمكن إزالة المالك", query.last_text)
        self.assertFalse(await database.remove_sub_admin(self.owner_id))

    async def test_owner_still_has_permission_toggles(self):
        # Owner keeps the permission surface (it is separate from the role).
        await self._manage(self.owner_id, f"amg_perm:{self.owner_id}:can_ai")
        # Owner permissions always resolve to full, regardless of the toggle.
        self.assertTrue(
            await database.user_has_permission(self.owner_id, "can_ai")
        )


class RevokedAdminTests(RBACBase):
    """Remove keeps the row but revokes access, and can be re-added."""

    async def test_removed_admin_is_not_admin_but_row_kept(self):
        await database.ensure_configured_admin(self.owner_id)
        await database.remove_sub_admin(self.sub_id)

        self.assertFalse(await database.is_user_admin(self.sub_id))
        self.assertFalse(await database.is_owner(self.sub_id))
        for key in database.PERMISSION_KEYS:
            self.assertFalse(await database.user_has_permission(self.sub_id, key))

        record = await database.get_admin_record(self.sub_id)
        self.assertEqual(record["role"], "none")
        self.assertTrue(await database.admin_access_denied(self.sub_id))

    async def test_revoked_admin_can_be_added_again(self):
        await database.remove_sub_admin(self.sub_id)
        self.assertTrue(await database.add_sub_admin(self.sub_id, "sub"))
        # A fresh add restores the legacy full-access role.
        self.assertTrue(await database.is_user_admin(self.sub_id))

    async def test_revoked_admin_is_excluded_from_notification_recipients(self):
        # `get_all_admins` feeds admin notifications; a revoked row must not
        # receive them even though it is still present in the table.
        await database.remove_sub_admin(self.sub_id)
        ids = [row[0] for row in await database.get_all_admins()]
        self.assertNotIn(self.sub_id, ids)


class HomeKeyboardTests(RBACBase):
    """Design rule: regular users see only their options; admins see the panel."""

    @staticmethod
    def _callbacks(markup):
        return [
            b.callback_data for row in markup.inline_keyboard for b in row
        ]

    async def test_regular_user_has_no_admin_button(self):
        callbacks = self._callbacks(main.home_keyboard())
        self.assertNotIn("admin", callbacks)
        self.assertIn("contact", callbacks)

    async def test_home_for_regular_user_hides_admin(self):
        query = _FakeQuery(self.student_id, "home")
        update = _FakeUpdate(query)
        markup = await main.home_for(update)
        self.assertNotIn("admin", self._callbacks(markup))

    async def test_home_for_admin_shows_admin_panel_once(self):
        await database.add_sub_admin(self.sub_id, "sub")
        query = _FakeQuery(self.sub_id, "home")
        update = _FakeUpdate(query)
        callbacks = self._callbacks(await main.home_for(update))
        self.assertEqual(callbacks.count("admin"), 1)

    async def test_admin_panel_rows_have_no_duplicates(self):
        await database.ensure_configured_admin(self.owner_id)
        query, _ = await self._route(self.owner_id, "admin")
        callbacks = self._callbacks(query.last_markup)
        self.assertEqual(len(callbacks), len(set(callbacks)), callbacks)
        self.assertIn("amg_list", callbacks)
        self.assertIn("audit_log", callbacks)

    async def test_admin_panel_hides_surfaces_without_permission(self):
        await database.ensure_configured_admin(self.owner_id)
        perms = await database.get_admin_permissions(self.sub_id)
        perms = dict(perms)
        perms["can_folders"] = False
        perms["can_ai"] = False
        perms["can_admins"] = False
        await database.update_admin_permissions(self.sub_id, perms)

        query, _ = await self._route(self.sub_id, "admin")
        callbacks = self._callbacks(query.last_markup)
        self.assertNotIn("admin_folders", callbacks)
        self.assertNotIn("admin_ai", callbacks)
        self.assertNotIn("amg_list", callbacks)
        self.assertNotIn("audit_log", callbacks)
        # Retained surfaces still show, exactly once each.
        self.assertEqual(callbacks.count("admin_pending"), 1)
        self.assertEqual(callbacks.count("admin_runtime"), 1)


class AIRegistryViewerTests(RBACBase):
    """The AI Registry screen must stay bounded and permission-gated."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await database.ensure_configured_admin(self.owner_id)

    async def _seed(self, count, provider="P"):
        for i in range(count):
            await database.ai_registry_add(
                provider=f"{provider}{i % 3}",
                model=f"model-{i}-" + ("x" * 40),
                endpoint=f"https://example.com/{i}",
                availability="DISCOVERED",
                auth_status="valid",
            )

    async def test_registry_message_stays_under_telegram_limit(self):
        await self._seed(120)
        query, _ = await self._route(self.owner_id, "admin_ai")
        text = query.last_text or ""
        self.assertTrue(text)
        self.assertLessEqual(len(text), 4096)
        self.assertIn("120 models", text)

    async def test_registry_lists_every_provider_header(self):
        await self._seed(9, provider="Prov")
        query, _ = await self._route(self.owner_id, "admin_ai")
        text = query.last_text or ""
        for idx in range(3):
            self.assertIn(f"Prov{idx}", text)

    async def test_registry_empty_state(self):
        query, _ = await self._route(self.owner_id, "admin_ai")
        self.assertIn("لا توجد نماذج", query.last_text)

    async def test_registry_denied_to_regular_user(self):
        query, _ = await self._route(self.student_id, "admin_ai")
        self.assertIn("غير مصرح", query.last_text)

    async def test_registry_denied_without_can_ai(self):
        perms = dict(await database.get_admin_permissions(self.sub_id))
        perms["can_ai"] = False
        await database.update_admin_permissions(self.sub_id, perms)

        query, _ = await self._route(self.sub_id, "admin_ai")
        self.assertIn("غير مصرح", query.last_text)


class AIRegistryTimestampTests(RBACBase):
    """`_refresh_discovered_models` must read `last_test` (column 11), not the
    neighbouring `error_category` (column 13), when ordering probes."""

    async def test_migration_added_columns_shift_last_test_away_from_13(self):
        await database.ai_registry_add(
            provider="groq",
            model="llama",
            endpoint="https://api.groq.com",
            availability="DISCOVERED",
            auth_status="valid",
        )
        db = await database.get_db()
        async with db.execute("PRAGMA table_info(ai_registry)") as cur:
            cols = [row[1] for row in await cur.fetchall()]
        await db.close()

        self.assertEqual(cols[11], "last_test")
        self.assertEqual(cols[13], "error_category")

    async def test_probe_order_uses_last_test_not_error_category(self):
        import ai

        # Two discovered models for one provider; the never-tested one (old id)
        # must be probed before the recently-tested one.
        await database.ai_registry_add(
            provider="groq", model="recent", endpoint="https://a",
            availability="DISCOVERED", auth_status="valid",
        )
        await database.ai_registry_add(
            provider="groq", model="fresh", endpoint="https://b",
            availability="DISCOVERED", auth_status="valid",
        )
        rows = await database.ai_registry_get_all()
        by_model = {row[2]: row for row in rows}
        recent_id = by_model["recent"][0]

        # Mark "recent" as tested just now; stamp error_category too, which is
        # exactly the column the old buggy code was reading.
        db = await database.get_db()
        await db.execute(
            "UPDATE ai_registry SET last_test = ?, error_category = NULL "
            "WHERE id = ?",
            ("2099-01-01 00:00:00", recent_id),
        )
        await db.commit()
        await db.close()

        probed = []

        async def fake_probe(item):
            probed.append(item["model"])

        original = ai._probe_model
        ai._probe_model = fake_probe
        try:
            candidates = [
                {"provider": "groq", "model": "recent", "endpoint": "https://a",
                 "availability": "DISCOVERED", "id": recent_id},
                {"provider": "groq", "model": "fresh", "endpoint": "https://b",
                 "availability": "DISCOVERED",
                 "id": by_model["fresh"][0]},
            ]
            await ai._refresh_discovered_models(candidates)
        finally:
            ai._probe_model = original

        self.assertIn("fresh", probed)
        self.assertLess(probed.index("fresh"), probed.index("recent"))


if __name__ == "__main__":
    unittest.main()
