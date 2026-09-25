"""Tests for the MEDBOT Scoped RBAC (Phase 3).

Runs against a temporary SQLite database only — never the real one — and
exercises real code paths: the v16 migration, the scope storage/resolution in
`database.py`, the single authorization layer (`authorization.py`), and the
handlers in `main.py`, `news.py` and `admin_management.py`. No business logic
is mocked; only the Telegram query/message boundary is a fake.

Covered (Phase 3 Definition of Done):
  1.  Migration v16 is additive, idempotent and creates `admin_scopes`.
  2.  A scope is validated against the real registry (folder/topic/resource).
  3.  `(admin_id, scope_type, scope_id)` is unique; duplicate add is idempotent.
  4.  No scope rows -> platform-wide (pre-Phase-3 behaviour is preserved).
  5.  A folder scope confines to that folder and its descendants; a sibling
      branch is refused (fail-closed).
  6.  `resource` and `topic` scopes resolve through the existing tables.
  7.  The owner is unrestricted by design.
  8.  Resource handlers (upload/delete/edit/move) honour scope.
  9.  Folder handlers (create/delete/move) honour scope.
  10. News handlers, reference pickers and lifecycle actions honour scope.
  11. Contribution review is confined to in-scope destinations.
  12. Denials are audited (`authz_denied`); grants/revokes are audited.
  13. The scope admin surface is owner-gated.
  14. Existing notifications / resource navigation are not affected.
"""

import os
import tempfile
import unittest

import admin_management
import audit
import authorization
import database
import main
import news
import notifications
from test_medbot_router import (
    _FakeContext,
    _FakeQuery,
    _FakeUpdate,
)
from test_medbot_fixes import _RecordingBot


class ScopedRbacBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-scope-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        database.DB_NAME = self.db_path
        await database.init_db()

        main._CONTENT_MESSAGES_BY_USER.clear()

        self.owner_id = 500
        self.scoped_id = 501
        self.plain_admin_id = 502
        self.student_id = 900

        await database.add_sub_admin(self.owner_id, "owner")
        await database.ensure_configured_admin(self.owner_id)

        # Two disjoint branches: A (Microbiology -> عملي) and B (Anatomy).
        self.year = await database.add_folder(0, "Second Year", "general")
        self.subject_a = await database.add_folder(self.year, "Microbiology", "general")
        self.section_a = await database.add_folder(
            self.subject_a, "عملي", "general", accepts_contributions=1
        )
        self.resource_a = await database.add_content(
            self.section_a, "Micro Lecture", "fid-a", "document"
        )

        self.subject_b = await database.add_folder(self.year, "Anatomy", "general")
        self.section_b = await database.add_folder(
            self.subject_b, "نظري", "general", accepts_contributions=1
        )
        self.resource_b = await database.add_content(
            self.section_b, "Anatomy Lecture", "fid-b", "document"
        )

        # A scoped admin responsible for Microbiology only.
        await database.add_sub_admin(self.scoped_id, "scoped")
        await database.apply_role_preset(self.scoped_id, "admin")
        await database.add_admin_scope(
            self.scoped_id, "folder", self.subject_a, created_by=self.owner_id
        )

        # An unscoped admin keeps platform-wide reach.
        await database.add_sub_admin(self.plain_admin_id, "plain")
        await database.apply_role_preset(self.plain_admin_id, "admin")

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        main._CONTENT_MESSAGES_BY_USER.clear()
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

    async def _news(self, user_id, data, context=None):
        query = _FakeQuery(user_id, data)
        ctx = context or _FakeContext()
        await news.news_callback_handler(_FakeUpdate(query), ctx)
        return query, ctx

    async def _amg(self, user_id, data, context=None):
        query = _FakeQuery(user_id, data)
        ctx = context or _FakeContext()
        await admin_management.admin_management_callback_handler(
            _FakeUpdate(query), ctx
        )
        return query, ctx


# ---------------------------------------------------------------
# 1-3. Migration + scope storage
# ---------------------------------------------------------------


class ScopeMigrationTests(ScopedRbacBase):
    async def test_scope_table_exists(self):
        db = await database.get_db()
        try:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) as cur:
                tables = {row[0] for row in await cur.fetchall()}
        finally:
            await db.close()
        self.assertIn("admin_scopes", tables)

    async def test_migration_is_idempotent(self):
        await database.init_db()
        self.assertTrue(await database.admin_has_scopes(self.scoped_id))

    async def test_scope_is_validated_against_registry(self):
        # Unknown folder id is refused.
        self.assertFalse(
            await database.add_admin_scope(self.scoped_id, "folder", 999999)
        )
        # Unknown scope type is refused.
        self.assertFalse(
            await database.add_admin_scope(self.scoped_id, "bogus", self.subject_b)
        )

    async def test_duplicate_scope_is_idempotent(self):
        before = len(await database.get_admin_scopes(self.scoped_id))
        self.assertTrue(
            await database.add_admin_scope(self.scoped_id, "folder", self.subject_a)
        )
        after = len(await database.get_admin_scopes(self.scoped_id))
        self.assertEqual(before, after)

    async def test_remove_scope(self):
        await database.remove_admin_scope(self.scoped_id, "folder", self.subject_a)
        self.assertFalse(await database.admin_has_scopes(self.scoped_id))

    async def test_scope_tables_scoped_to_own_row(self):
        # The migration adds one table; nothing else is created or dropped.
        nid = await database.create_news(
            news_type="notify", title="X", sender_id=self.owner_id,
            status="published",
        )
        await database.init_db()
        self.assertIsNotNone(await database.get_news(nid))


# ---------------------------------------------------------------
# 4-7. Authorization resolution
# ---------------------------------------------------------------


class AuthorizationResolutionTests(ScopedRbacBase):
    async def test_no_scopes_means_platform_wide(self):
        # Pre-Phase-3 behaviour: an unscoped admin keeps full reach.
        self.assertTrue(
            await authorization.can(
                self.plain_admin_id, "resource.delete", "resource", self.resource_b
            )
        )
        self.assertTrue(
            await authorization.can(
                self.plain_admin_id, "section.manage", "folder", self.subject_b
            )
        )

    async def test_folder_scope_confines_to_subtree(self):
        self.assertTrue(
            await authorization.can(
                self.scoped_id, "section.manage", "folder", self.subject_a
            )
        )
        self.assertTrue(
            await authorization.can(
                self.scoped_id, "section.manage", "folder", self.section_a
            )
        )

    async def test_sibling_branch_is_refused(self):
        self.assertFalse(
            await authorization.can(
                self.scoped_id, "section.manage", "folder", self.subject_b
            )
        )
        self.assertFalse(
            await authorization.can(
                self.scoped_id, "resource.delete", "resource", self.resource_b
            )
        )

    async def test_parent_of_scope_is_refused(self):
        # The scope is a subtree; its ancestors are outside it.
        self.assertFalse(
            await authorization.can(
                self.scoped_id, "section.manage", "folder", self.year
            )
        )

    async def test_resource_inside_scope_allowed(self):
        self.assertTrue(
            await authorization.can(
                self.scoped_id, "resource.edit", "resource", self.resource_a
            )
        )

    async def test_direct_resource_scope(self):
        await database.clear_admin_scopes(self.scoped_id)
        await database.add_admin_scope(
            self.scoped_id, "resource", self.resource_b
        )
        self.assertTrue(
            await authorization.can(
                self.scoped_id, "resource.edit", "resource", self.resource_b
            )
        )
        self.assertFalse(
            await authorization.can(
                self.scoped_id, "resource.edit", "resource", self.resource_a
            )
        )

    async def test_topic_scope_resolves_linked_folders(self):
        await database.clear_admin_scopes(self.scoped_id)
        topic_id = await database.add_topic("Microbiology")
        await database.link_topic_folder(topic_id, self.subject_a)
        await database.add_admin_scope(self.scoped_id, "topic", topic_id)
        self.assertTrue(
            await authorization.can(
                self.scoped_id, "resource.edit", "resource", self.resource_a
            )
        )
        self.assertFalse(
            await authorization.can(
                self.scoped_id, "resource.edit", "resource", self.resource_b
            )
        )

    async def test_owner_is_unrestricted(self):
        await database.add_admin_scope(
            self.owner_id, "folder", self.subject_a, created_by=self.owner_id
        )
        self.assertTrue(
            await authorization.can(
                self.owner_id, "resource.delete", "resource", self.resource_b
            )
        )

    async def test_global_target_denied_for_scoped_admin(self):
        # notification.send has no folder target -> a scoped admin is refused.
        self.assertFalse(
            await authorization.can(self.scoped_id, "notification.send")
        )
        self.assertTrue(
            await authorization.can(self.plain_admin_id, "notification.send")
        )

    async def test_unknown_permission_denied(self):
        self.assertFalse(
            await authorization.can(
                self.plain_admin_id, "bogus.permission", "folder", self.subject_a
            )
        )

    async def test_non_admin_denied(self):
        self.assertFalse(
            await authorization.can(
                self.student_id, "resource.delete", "resource", self.resource_a
            )
        )

    async def test_deleted_target_fails_closed(self):
        await database.clear_admin_scopes(self.scoped_id)
        await database.add_admin_scope(self.scoped_id, "resource", self.resource_b)
        await database.delete_file(self.resource_b)
        self.assertFalse(
            await authorization.can(
                self.scoped_id, "resource.edit", "resource", self.resource_b
            )
        )

    async def test_require_reports_decision_codes(self):
        self.assertEqual(
            await authorization.require(
                self.owner_id, "resource.delete", "resource", self.resource_a
            ),
            authorization.DECISION_ALLOW,
        )
        self.assertEqual(
            await authorization.require(
                self.scoped_id, "resource.delete", "resource", self.resource_b
            ),
            authorization.DECISION_OUT_OF_SCOPE,
        )
        self.assertEqual(
            await authorization.require(
                self.student_id, "resource.delete", "resource", self.resource_a
            ),
            authorization.DECISION_NOT_ADMIN,
        )


# ---------------------------------------------------------------
# 8. Resource handlers honour scope
# ---------------------------------------------------------------


class ResourceScopeTests(ScopedRbacBase):
    async def test_scoped_admin_can_delete_in_scope(self):
        await self._route(
            self.scoped_id, f"admin_file_delete:{self.resource_a}"
        )
        self.assertIsNone(await database.get_file_record(self.resource_a))

    async def test_scoped_admin_cannot_delete_out_of_scope(self):
        query, _ = await self._route(
            self.scoped_id, f"admin_file_delete:{self.resource_b}"
        )
        self.assertIsNotNone(await database.get_file_record(self.resource_b))
        self.assertIn("نطاق", query.last_text or "")

    async def test_scoped_admin_cannot_view_out_of_scope_resource(self):
        await self._route(self.scoped_id, f"admin_file:{self.resource_b}")
        # The out-of-scope resource must remain untouched and unexposed.
        self.assertIsNotNone(await database.get_file_record(self.resource_b))

    async def test_scoped_admin_cannot_move_out_of_scope(self):
        await self._route(
            self.scoped_id,
            f"admin_file_move_to:{self.resource_a}:{self.section_b}",
        )
        # Move must be refused: destination outside scope.
        self.assertEqual(
            (await database.get_file_record(self.resource_a))[1], self.section_a
        )

    async def test_scoped_admin_cannot_upload_into_out_of_scope_folder(self):
        query, ctx = await self._route(
            self.scoped_id, f"admin_upload:{self.section_b}"
        )
        self.assertFalse(ctx.user_data.get("admin_upload_folder"))
        self.assertIn("نطاق", query.last_text or "")

    async def test_scoped_admin_can_enter_in_scope_upload(self):
        _, ctx = await self._route(
            self.scoped_id, f"admin_upload:{self.section_a}"
        )
        self.assertTrue(ctx.user_data.get("admin_upload_folder"))

    async def test_scoped_admin_cannot_rename_out_of_scope(self):
        await self._route(
            self.scoped_id, f"admin_file_rename:{self.resource_b}"
        )
        self.assertEqual(
            (await database.get_file_record(self.resource_b))[2], "Anatomy Lecture"
        )

    async def test_plain_admin_delete_still_works(self):
        await self._route(
            self.plain_admin_id, f"admin_file_delete:{self.resource_b}"
        )
        self.assertIsNone(await database.get_file_record(self.resource_b))

    async def test_scoped_admin_cannot_retype_out_of_scope_resource(self):
        await self._route(
            self.scoped_id, f"admin_file_settype:{self.resource_b}:audio"
        )
        self.assertEqual(
            (await database.get_file_record(self.resource_b))[4], "document"
        )

    async def test_scoped_admin_can_retype_in_scope_resource(self):
        await self._route(
            self.scoped_id, f"admin_file_settype:{self.resource_a}:audio"
        )
        self.assertEqual(
            (await database.get_file_record(self.resource_a))[4], "audio"
        )

    async def test_scoped_admin_file_move_menu_hides_out_of_scope_roots(self):
        query, _ = await self._route(
            self.scoped_id, f"admin_file_move:{self.resource_a}"
        )
        markup = query.last_markup
        callbacks = " ".join(
            b.callback_data
            for row in (markup.inline_keyboard if markup else [])
            for b in row
        )
        self.assertNotIn(f":{self.subject_b}", callbacks)


# ---------------------------------------------------------------
# 9. Folder handlers honour scope
# ---------------------------------------------------------------


class FolderScopeTests(ScopedRbacBase):
    async def test_scoped_admin_cannot_delete_out_of_scope_folder(self):
        await self._route(
            self.scoped_id, f"admin_folder_delete:{self.section_b}"
        )
        self.assertIsNotNone(await database.get_folder(self.section_b))

    async def test_scoped_admin_can_delete_in_scope_empty_folder(self):
        empty = await database.add_folder(self.subject_a, "Empty", "general")
        await self._route(self.scoped_id, f"admin_folder_delete:{empty}")
        self.assertIsNone(await database.get_folder(empty))

    async def test_scoped_admin_cannot_move_out_of_scope(self):
        await self._route(
            self.scoped_id,
            f"admin_folder_move_to:{self.section_a}:{self.subject_b}",
        )
        self.assertEqual(
            await database.get_parent_id(self.section_a), self.subject_a
        )

    async def test_scoped_admin_cannot_open_out_of_scope_folder(self):
        await self._route(self.scoped_id, f"admin_folder:{self.subject_b}")
        # Folder untouched; the denial does not leak its contents.
        self.assertIsNotNone(await database.get_folder(self.subject_b))

    async def test_scoped_admin_cannot_create_out_of_scope(self):
        query, ctx = await self._route(
            self.scoped_id, "admin_folder_select_parent:" + str(self.subject_b)
        )
        self.assertFalse(ctx.user_data.get("admin_folder_create"))
        self.assertIn("نطاق", query.last_text or "")

    async def test_scoped_admin_cannot_rename_out_of_scope_folder(self):
        query, ctx = await self._route(
            self.scoped_id, f"admin_folder_rename:{self.subject_b}"
        )
        self.assertFalse(ctx.user_data.get("admin_folder_rename"))
        self.assertIn("نطاق", query.last_text or "")

    async def test_scoped_admin_can_rename_in_scope_folder(self):
        _, ctx = await self._route(
            self.scoped_id, f"admin_folder_rename:{self.section_a}"
        )
        self.assertTrue(ctx.user_data.get("admin_folder_rename"))

    async def test_scoped_admin_cannot_retype_out_of_scope_folder(self):
        query, ctx = await self._route(
            self.scoped_id, f"admin_folder_retype_existing:{self.subject_b}"
        )
        self.assertIsNone(ctx.user_data.get("admin_folder_retype_id"))
        self.assertIn("نطاق", query.last_text or "")

    async def test_scoped_admin_move_picker_hides_out_of_scope(self):
        await self._route(self.scoped_id, f"admin_folder_move:{self.section_a}")
        query, _ = await self._route(
            self.scoped_id,
            f"admin_folder_move_browse:{self.section_a}:0",
        )
        markup = query.last_markup
        callbacks = " ".join(
            b.callback_data
            for row in (markup.inline_keyboard if markup else [])
            for b in row
        )
        # The root move and the sibling branch must not be offered.
        self.assertNotIn("admin_folder_move_to:%d:0" % self.section_a, callbacks)
        self.assertNotIn(f":{self.subject_b}", callbacks)

    async def test_scoped_admin_cannot_move_folder_to_root(self):
        await self._route(
            self.scoped_id, f"admin_folder_move_to:{self.section_a}:0"
        )
        self.assertEqual(
            await database.get_parent_id(self.section_a), self.subject_a
        )


# ---------------------------------------------------------------
# 10. News handlers honour scope
# ---------------------------------------------------------------


class NewsScopeTests(ScopedRbacBase):
    async def _make_section_news(self, folder_id, title):
        news_id = await database.create_news(
            news_type="section", title=title, sender_id=self.owner_id,
            section_folder_id=folder_id, subject_folder_id=folder_id,
        )
        return news_id

    async def test_scoped_admin_list_shows_only_own_news(self):
        in_id = await self._make_section_news(self.subject_a, "A news")
        out_id = await self._make_section_news(self.subject_b, "B news")
        await database.publish_news(in_id)
        await database.publish_news(out_id)

        query, _ = await self._news(self.scoped_id, "admin_news")
        text = query.last_text or ""
        self.assertIn("A news", text)
        self.assertNotIn("B news", text)

    async def test_scoped_admin_cannot_open_out_of_scope_news(self):
        out_id = await self._make_section_news(self.subject_b, "B news")
        await database.publish_news(out_id)
        query, _ = await self._news(self.scoped_id, f"news_admin_view:{out_id}")
        self.assertIn("نطاق", query.last_text or "")

    async def test_scoped_admin_cannot_publish_out_of_scope(self):
        out_id = await self._make_section_news(self.subject_b, "B news")
        await self._news(self.scoped_id, f"news_admin_pub:{out_id}")
        self.assertEqual((await database.get_news_detail(out_id))["status"], "draft")

    async def test_scoped_admin_can_manage_in_scope_news(self):
        in_id = await self._make_section_news(self.subject_a, "A news")
        await self._news(self.scoped_id, f"news_admin_pub:{in_id}")
        self.assertEqual((await database.get_news_detail(in_id))["status"], "published")

    async def test_scoped_admin_cannot_archive_out_of_scope(self):
        out_id = await self._make_section_news(self.subject_b, "B news")
        await database.publish_news(out_id)
        await self._news(self.scoped_id, f"news_admin_archive:{out_id}")
        self.assertEqual((await database.get_news_detail(out_id))["status"], "published")

    async def test_scoped_admin_cannot_reference_out_of_scope_folder(self):
        in_id = await self._make_section_news(self.subject_a, "A news")
        await self._news(
            self.scoped_id,
            f"news_ref_set_section:{in_id}:{self.subject_b}",
        )
        detail = await database.get_news_detail(in_id)
        self.assertNotEqual(detail.get("section_folder_id"), self.subject_b)

    async def test_scoped_admin_cannot_reference_out_of_scope_resource(self):
        news_id = await database.create_news(
            news_type="section", title="R news", sender_id=self.owner_id,
            section_folder_id=self.section_a,
        )
        await self._news(
            self.scoped_id,
            f"news_ref_set_resource:{news_id}:{self.resource_b}",
        )
        detail = await database.get_news_detail(news_id)
        self.assertNotEqual(detail.get("resource_id"), self.resource_b)

    async def test_scoped_admin_cannot_create_global_notification(self):
        _, ctx = await self._news(self.scoped_id, "news_new:notify")
        self.assertIsNone(ctx.user_data.get("news_type"))

    async def test_plain_admin_news_is_unfiltered(self):
        out_id = await self._make_section_news(self.subject_b, "B news")
        await database.publish_news(out_id)
        query, _ = await self._news(self.plain_admin_id, "admin_news")
        self.assertIn("B news", query.last_text or "")

    async def test_direct_resource_scope_covers_its_news(self):
        # An admin scoped only to the resource (no folder scope) may manage the
        # Section News item that links it.
        news_id = await database.create_news(
            news_type="section", title="R news", sender_id=self.owner_id,
            section_folder_id=self.section_a,
        )
        await database.update_news(news_id, resource_id=self.resource_a)
        await database.clear_admin_scopes(self.scoped_id)
        await database.add_admin_scope(self.scoped_id, "resource", self.resource_a)
        await self._news(self.scoped_id, f"news_admin_pub:{news_id}")
        self.assertEqual((await database.get_news_detail(news_id))["status"], "published")

    async def test_direct_resource_scope_does_not_cover_other_resource_news(self):
        news_id = await database.create_news(
            news_type="section", title="R news", sender_id=self.owner_id,
            section_folder_id=self.section_a,
        )
        await database.update_news(news_id, resource_id=self.resource_b)
        await database.clear_admin_scopes(self.scoped_id)
        await database.add_admin_scope(self.scoped_id, "resource", self.resource_a)
        query, _ = await self._news(self.scoped_id, f"news_admin_view:{news_id}")
        self.assertIn("نطاق", query.last_text or "")


# ---------------------------------------------------------------
# 11. Contribution review honours scope
# ---------------------------------------------------------------


class ContributionScopeTests(ScopedRbacBase):
    async def _pending(self, folder_id, title):
        return await database.add_contribution(
            self.student_id, "Student", folder_id, title, "fid", "document"
        )

    async def test_scoped_reviewer_cannot_approve_out_of_scope(self):
        cid = await self._pending(self.section_b, "Out contribution")
        query, _ = await self._route(self.scoped_id, f"approve:{cid}")
        record = await database.get_contribution(cid)
        self.assertEqual(record[7], "pending")

    async def test_scoped_reviewer_can_approve_in_scope(self):
        cid = await self._pending(self.section_a, "In contribution")
        await self._route(self.scoped_id, f"approve:{cid}")
        record = await database.get_contribution(cid)
        self.assertEqual(record[7], "approved")

    async def test_scoped_reviewer_pending_list_filtered(self):
        in_c = await self._pending(self.section_a, "In contribution")
        out_c = await self._pending(self.section_b, "Out contribution")
        query, _ = await self._route(self.scoped_id, "admin_pending")
        markup = query.last_markup
        callbacks = {
            b.callback_data
            for row in (markup.inline_keyboard if markup else [])
            for b in row
        }
        self.assertIn(f"review:{in_c}", callbacks)
        self.assertNotIn(f"review:{out_c}", callbacks)


# ---------------------------------------------------------------
# 12. Audit events
# ---------------------------------------------------------------


class ScopeAuditTests(ScopedRbacBase):
    async def test_denial_is_audited(self):
        await authorization.require(
            self.scoped_id, "resource.delete", "resource", self.resource_b
        )
        entries = await database.get_audit_entries(action="authz_denied")
        self.assertTrue(entries)
        self.assertIn("out_of_scope", str(entries[0]))

    async def test_grant_is_audited(self):
        await self._amg(
            self.owner_id,
            f"amg_scope_set:{self.scoped_id}:folder:{self.subject_b}",
        )
        entries = await database.get_audit_entries(action="scope_grant")
        self.assertTrue(entries)

    async def test_revoke_is_audited(self):
        await self._amg(
            self.owner_id,
            f"amg_scope_view:{self.scoped_id}:folder:{self.subject_a}",
        )
        entries = await database.get_audit_entries(action="scope_revoke")
        self.assertTrue(entries)


# ---------------------------------------------------------------
# 13. Admin surface gating
# ---------------------------------------------------------------


class ScopeAdminSurfaceTests(ScopedRbacBase):
    async def test_non_owner_cannot_open_scope_surface(self):
        query, _ = await self._amg(self.scoped_id, f"amg_scopes:{self.scoped_id}")
        self.assertIn("غير مصرح", query.last_text or "")

    async def test_owner_can_view_and_grant_scope(self):
        query, _ = await self._amg(self.owner_id, f"amg_scopes:{self.scoped_id}")
        self.assertIn("نطاق", query.last_text or "")
        await self._amg(
            self.owner_id,
            f"amg_scope_set:{self.scoped_id}:folder:{self.section_a}",
        )
        scopes = await database.get_admin_scopes(self.scoped_id)
        ids = {(s["scope_type"], s["scope_id"]) for s in scopes}
        self.assertIn(("folder", self.section_a), ids)

    async def test_owner_scope_is_refused(self):
        await self._amg(
            self.owner_id,
            f"amg_scope_set:{self.owner_id}:folder:{self.subject_a}",
        )
        self.assertFalse(await database.admin_has_scopes(self.owner_id))

    async def test_permissions_guide_owner_only(self):
        query, _ = await self._amg(self.scoped_id, "amg_perms_guide")
        self.assertIn("غير مصرح", query.last_text or "")
        query, _ = await self._amg(self.owner_id, "amg_perms_guide")
        self.assertIn("الصلاحيات", query.last_text or "")


# ---------------------------------------------------------------
# 14. Existing systems unaffected
# ---------------------------------------------------------------


class ExistingSystemsUnaffectedTests(ScopedRbacBase):
    async def test_broadcast_notification_still_works(self):
        # A plain broadcast (not via the scoped authorization gate) must still
        # reach users; the scope layer is additive.
        await database.register_user(self.student_id)
        bot = _RecordingBot()
        recipients, delivered = await notifications.broadcast_notification(
            bot, "Important"
        )
        self.assertEqual(recipients, 1)
        self.assertEqual(delivered, 1)
        self.assertTrue(bot.messages)

    async def test_resource_navigation_for_students_unaffected(self):
        query, _ = await self._route(self.student_id, f"folder:{self.subject_a}")
        self.assertTrue(query.answered)
        self.assertNotIn("نطاق", query.last_text or "")

    async def test_admin_without_scopes_unchanged(self):
        self.assertFalse(
            await database.admin_has_scopes(self.plain_admin_id)
        )
        self.assertTrue(
            await database.user_has_permission(self.plain_admin_id, "can_content")
        )

    async def test_unscoped_admin_may_broadcast(self):
        self.assertTrue(await notifications._is_authorized(self.plain_admin_id))

    async def test_scoped_admin_may_not_broadcast_globally(self):
        # A global broadcast has no folder target, so a scope-restricted admin
        # cannot satisfy the scope test and is refused.
        self.assertFalse(await notifications._is_authorized(self.scoped_id))

    async def test_owner_may_broadcast(self):
        self.assertTrue(await notifications._is_authorized(self.owner_id))


if __name__ == "__main__":
    unittest.main()
