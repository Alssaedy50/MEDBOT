"""Phase 2 tests: contribution review workflow, validation and resubmission.

Runs against a temporary SQLite database. No network, no real file_ids,
no real tokens.
"""

import os
import tempfile
import unittest

import database
import main
from test_medbot_router import (
    _FakeContext,
    _FakeDoc,
    _FakePhoto,
    _FakeQuery,
    _FakeUpdate,
    _TextMessage,
    _MediaMessage,
    _MediaUpdate,
)


class ContributionReviewBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-phase2-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        database.DB_NAME = self.db_path
        await database.init_db()

        await database.add_folder(0, "Contribs", "general", 1)
        self.folder_id = (await database.get_folders(0))[0][0]

        await database.add_sub_admin(500, "admin")
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

    async def _make_contribution(self, title="Thesis", file_id="fid-1", status=None):
        cid = await database.add_contribution(
            self.student_id, "Student", self.folder_id, title, file_id, "document"
        )
        if status == "needs_revision":
            await database.request_contribution_revision(
                cid, reviewer_id=self.admin_id, note="fix it"
            )
        elif status == "rejected":
            await database.reject_contribution(
                cid, reviewer_id=self.admin_id, reason="bad"
            )
        elif status == "approved":
            await database.approve_contribution(cid, reviewer_id=self.admin_id)
        return cid


# ---------------------------------------------------------------
# 1. Migration / schema
# ---------------------------------------------------------------


class MigrationV3Tests(ContributionReviewBase):
    async def test_review_columns_exist(self):
        db = await database.get_db()
        try:
            async with db.execute("PRAGMA table_info(contributions)") as cur:
                cols = {row[1] for row in await cur.fetchall()}
        finally:
            await db.close()

        for col in (
            "reviewed_by",
            "reviewed_at",
            "review_note",
            "rejection_reason",
            "resubmitted_count",
        ):
            self.assertIn(col, cols)

    async def test_migration_is_idempotent(self):
        # init_db runs migrations again; must not raise.
        await database.init_db()
        await database.init_db()

        db = await database.get_db()
        try:
            async with db.execute(
                "PRAGMA index_list(contributions)"
            ) as cur:
                names = {row[1] for row in await cur.fetchall()}
        finally:
            await db.close()

        self.assertIn("idx_contrib_user_status", names)


# ---------------------------------------------------------------
# 2. Backward compatibility with pre-Phase-2 rows
# ---------------------------------------------------------------


class BackwardCompatibilityTests(ContributionReviewBase):
    async def _insert_legacy_row(self, status):
        """Insert a row exactly as the old schema wrote it (no review cols)."""
        db = await database.get_db()
        try:
            cursor = await db.execute(
                """
                INSERT INTO contributions
                    (user_id, user_name, folder_id, title, file_id, file_type, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.student_id,
                    "Legacy Student",
                    self.folder_id,
                    f"Legacy {status}",
                    "legacy-fid",
                    "document",
                    status,
                ),
            )
            await db.commit()
            return cursor.lastrowid
        finally:
            await db.close()

    async def test_legacy_pending_row_readable_and_reviewable(self):
        cid = await self._insert_legacy_row("pending")

        record = await database.get_contribution(cid)
        self.assertIsNotNone(record)
        self.assertEqual(record[7], "pending")
        # Review columns are NULL for legacy rows.
        self.assertIsNone(record[9])
        self.assertIsNone(record[10])

        result = await database.approve_contribution(cid, reviewer_id=self.admin_id)
        self.assertIsNotNone(result)
        self.assertEqual(record[7], "pending")

    async def test_legacy_approved_row_still_blocks_double_review(self):
        cid = await self._insert_legacy_row("approved")
        self.assertIsNone(await database.approve_contribution(cid))
        self.assertIsNone(await database.reject_contribution(cid))

    async def test_legacy_rejected_row_still_blocks_double_review(self):
        cid = await self._insert_legacy_row("rejected")
        self.assertIsNone(await database.reject_contribution(cid))
        self.assertIsNone(await database.approve_contribution(cid))

    async def test_legacy_rows_listed_when_pending(self):
        await self._insert_legacy_row("pending")
        items = await database.get_pending_contributions_list()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][7], "pending")


# ---------------------------------------------------------------
# 3. Reviewer metadata recorded on approve / reject
# ---------------------------------------------------------------


class ReviewerMetadataTests(ContributionReviewBase):
    async def test_approve_records_reviewer(self):
        cid = await self._make_contribution()
        await database.approve_contribution(cid, reviewer_id=self.admin_id)

        record = await database.get_contribution(cid)
        self.assertEqual(record[7], "approved")
        self.assertEqual(record[9], self.admin_id)
        self.assertIsNotNone(record[10])

    async def test_reject_records_reviewer_and_reason(self):
        cid = await self._make_contribution()
        await database.reject_contribution(
            cid, reviewer_id=self.admin_id, reason="مصدر غير موثوق"
        )

        record = await database.get_contribution(cid)
        self.assertEqual(record[7], "rejected")
        self.assertEqual(record[9], self.admin_id)
        self.assertEqual(record[12], "مصدر غير موثوق")
        self.assertIsNotNone(record[10])

    async def test_revision_records_note(self):
        cid = await self._make_contribution()
        await database.request_contribution_revision(
            cid, reviewer_id=self.admin_id, note="أضف المرجع"
        )

        record = await database.get_contribution(cid)
        self.assertEqual(record[7], "needs_revision")
        self.assertEqual(record[9], self.admin_id)
        self.assertEqual(record[11], "أضف المرجع")


# ---------------------------------------------------------------
# 4. Atomicity / double review prevention
# ---------------------------------------------------------------


class DoubleReviewTests(ContributionReviewBase):
    async def test_no_double_approval(self):
        cid = await self._make_contribution()
        self.assertIsNotNone(
            await database.approve_contribution(cid, reviewer_id=self.admin_id)
        )
        self.assertIsNone(
            await database.approve_contribution(cid, reviewer_id=self.admin_id)
        )
        self.assertEqual(len(await database.get_files(self.folder_id)), 1)

    async def test_no_approve_after_reject(self):
        cid = await self._make_contribution()
        await database.reject_contribution(cid, reviewer_id=self.admin_id, reason="x")
        self.assertIsNone(await database.approve_contribution(cid))
        self.assertEqual(len(await database.get_files(self.folder_id)), 0)

    async def test_no_reject_after_approve(self):
        cid = await self._make_contribution()
        await database.approve_contribution(cid, reviewer_id=self.admin_id)
        self.assertIsNone(await database.reject_contribution(cid, reviewer_id=501))

    async def test_needs_revision_can_then_be_approved(self):
        cid = await self._make_contribution()
        self.assertIsNotNone(
            await database.request_contribution_revision(cid, self.admin_id, "fix")
        )
        self.assertIsNotNone(
            await database.approve_contribution(cid, reviewer_id=self.admin_id)
        )
        self.assertEqual(len(await database.get_files(self.folder_id)), 1)

    async def test_cannot_review_missing_contribution(self):
        self.assertIsNone(await database.approve_contribution(999999))
        self.assertIsNone(await database.reject_contribution(999999))
        self.assertIsNone(await database.request_contribution_revision(999999))


# ---------------------------------------------------------------
# 5. Needs Revision + resubmission
# ---------------------------------------------------------------


class ResubmissionTests(ContributionReviewBase):
    async def test_resubmit_reopens_and_clears_review_fields(self):
        cid = await self._make_contribution(status="needs_revision")

        ok, result = await database.resubmit_contribution(
            cid, self.student_id, "Thesis v2", "fid-v2", "document"
        )
        self.assertTrue(ok, result)

        record = await database.get_contribution(cid)
        self.assertEqual(record[7], "pending")
        self.assertEqual(record[4], "Thesis v2")
        self.assertEqual(record[5], "fid-v2")
        self.assertIsNone(record[9])
        self.assertIsNone(record[10])
        self.assertIsNone(record[11])
        self.assertEqual(record[13], 1)

    async def test_resubmit_by_other_user_refused(self):
        cid = await self._make_contribution(status="needs_revision")
        ok, _ = await database.resubmit_contribution(
            cid, 777, "Hijack", "fid-h", "document"
        )
        self.assertFalse(ok)
        self.assertEqual((await database.get_contribution(cid))[7], "needs_revision")

    async def test_resubmit_when_not_needs_revision_refused(self):
        cid = await self._make_contribution()
        ok, _ = await database.resubmit_contribution(
            cid, self.student_id, "Nope", "fid-n", "document"
        )
        self.assertFalse(ok)

    async def test_resubmit_bad_file_type_refused(self):
        cid = await self._make_contribution(status="needs_revision")
        ok, _ = await database.resubmit_contribution(
            cid, self.student_id, "T", "fid-x", "malware"
        )
        self.assertFalse(ok)

    async def test_resubmitted_item_returns_to_review_queue(self):
        cid = await self._make_contribution(status="needs_revision")
        await database.resubmit_contribution(
            cid, self.student_id, "Fixed", "fid-f", "document"
        )
        items = await database.get_pending_contributions_list()
        ids = [i[0] for i in items]
        self.assertIn(cid, ids)

    async def test_needs_revision_counted_as_pending(self):
        await self._make_contribution(status="needs_revision")
        self.assertEqual(await database.get_pending_contributions_count(), 1)


# ---------------------------------------------------------------
# 6. Submission validation
# ---------------------------------------------------------------


class SubmissionValidationTests(ContributionReviewBase):
    async def test_valid_submission_accepted(self):
        error = await database.validate_contribution_submission(
            self.folder_id, "Good", "fid-g", "document", user_id=self.student_id
        )
        self.assertIsNone(error)

    async def test_missing_file_id_rejected(self):
        error = await database.validate_contribution_submission(
            self.folder_id, "T", "", "document", user_id=self.student_id
        )
        self.assertIsNotNone(error)

    async def test_empty_title_rejected(self):
        error = await database.validate_contribution_submission(
            self.folder_id, "   ", "fid-t", "document", user_id=self.student_id
        )
        self.assertIsNotNone(error)

    async def test_overlong_title_rejected(self):
        error = await database.validate_contribution_submission(
            self.folder_id,
            "x" * (database.MAX_CONTRIBUTION_TITLE_LENGTH + 1),
            "fid-t",
            "document",
            user_id=self.student_id,
        )
        self.assertIsNotNone(error)

    async def test_disallowed_file_type_rejected(self):
        error = await database.validate_contribution_submission(
            self.folder_id, "T", "fid-t", "executable", user_id=self.student_id
        )
        self.assertIsNotNone(error)

    async def test_missing_folder_rejected(self):
        error = await database.validate_contribution_submission(
            999999, "T", "fid-t", "document", user_id=self.student_id
        )
        self.assertIsNotNone(error)

    async def test_non_contributing_folder_rejected(self):
        await database.add_folder(0, "Closed", "general", 0)
        closed_id = [f[0] for f in await database.get_folders(0) if f[1] == "Closed"][0]
        error = await database.validate_contribution_submission(
            closed_id, "T", "fid-t", "document", user_id=self.student_id
        )
        self.assertIsNotNone(error)

    async def test_duplicate_submission_rejected(self):
        await self._make_contribution(title="Dup", file_id="fid-dup")
        error = await database.validate_contribution_submission(
            self.folder_id, "Dup", "fid-dup", "document", user_id=self.student_id
        )
        self.assertIsNotNone(error)

    async def test_add_contribution_raises_on_invalid(self):
        with self.assertRaises(database.ContributionValidationError):
            await database.add_contribution(
                self.student_id, "S", self.folder_id, "", "fid-e", "document"
            )

    async def test_overlong_file_id_rejected(self):
        error = await database.validate_contribution_submission(
            self.folder_id,
            "T",
            "f" * (database.MAX_CONTRIBUTION_FILE_ID_LENGTH + 1),
            "document",
            user_id=self.student_id,
        )
        self.assertIsNotNone(error)


# ---------------------------------------------------------------
# 7. Router wiring (notifications, review states, UI)
# ---------------------------------------------------------------


class RouterPhase2Tests(ContributionReviewBase):
    async def test_new_contribution_notifies_all_admins(self):
        await database.add_sub_admin(501, "admin2")

        ctx = _FakeContext()
        query = _FakeQuery(self.student_id, f"contrib_folder:{self.folder_id}")
        await main.callback_router(_FakeUpdate(query), ctx)
        self.assertEqual(ctx.user_data.get("contribution_folder"), self.folder_id)

        msg = _MediaMessage(document=_FakeDoc("fid-notify", "n.pdf"))
        handled = await main.contribution_media_handler(
            _MediaUpdate(self.student_id, msg), ctx
        )
        self.assertTrue(handled)

        admins_notified = {c["chat_id"] for c in ctx.bot.sent}
        self.assertIn(500, admins_notified)
        self.assertIn(501, admins_notified)

    async def test_notification_failure_is_non_fatal(self):
        class _BrokenBot:
            async def send_message(self, **kwargs):
                raise RuntimeError("blocked")

        delivered = await main.notify_admins_new_contribution(
            _BrokenBot(), 1, "T", "S"
        )
        self.assertEqual(delivered, 0)

    async def test_invalid_submission_does_not_notify(self):
        ctx = _FakeContext()
        ctx.user_data["contribution_folder"] = self.folder_id

        # Empty title cannot happen via Telegram; simulate a bad file type.
        msg = _MediaMessage(
            document=_FakeDoc("f" * (database.MAX_CONTRIBUTION_FILE_ID_LENGTH + 1), "x")
        )
        handled = await main.contribution_media_handler(
            _MediaUpdate(self.student_id, msg), ctx
        )
        self.assertTrue(handled)
        self.assertEqual(ctx.bot.sent, [])

    async def test_reject_callback_arms_reason_state(self):
        cid = await self._make_contribution()
        ctx = _FakeContext()
        query = _FakeQuery(self.admin_id, f"reject:{cid}")
        await main.callback_router(_FakeUpdate(query), ctx)

        self.assertEqual(ctx.user_data.get("review_note_kind"), "reject")
        self.assertEqual(ctx.user_data.get("review_note_id"), cid)
        # Contribution is not rejected yet.
        self.assertEqual((await database.get_contribution(cid))[7], "pending")

    async def test_non_admin_cannot_arm_reject_state(self):
        cid = await self._make_contribution()
        ctx = _FakeContext()
        query = _FakeQuery(self.student_id, f"reject:{cid}")
        await main.callback_router(_FakeUpdate(query), ctx)

        self.assertIsNone(ctx.user_data.get("review_note_kind"))
        self.assertEqual((await database.get_contribution(cid))[7], "pending")

    async def test_typed_rejection_reason_applies_and_notifies(self):
        cid = await self._make_contribution()
        ctx = _FakeContext()
        ctx.user_data["review_note_kind"] = "reject"
        ctx.user_data["review_note_id"] = cid

        msg = _TextMessage("المرجع غير موثوق")
        handled = await main.process_review_text(_MediaUpdate(self.admin_id, msg), ctx)
        self.assertTrue(handled)

        record = await database.get_contribution(cid)
        self.assertEqual(record[7], "rejected")
        self.assertEqual(record[12], "المرجع غير موثوق")
        self.assertEqual(record[9], self.admin_id)
        # Contributor notified.
        self.assertIn(self.student_id, {c["chat_id"] for c in ctx.bot.sent})
        # State cleared.
        self.assertIsNone(ctx.user_data.get("review_note_kind"))

    async def test_typed_revision_note_applies(self):
        cid = await self._make_contribution()
        ctx = _FakeContext()
        ctx.user_data["review_note_kind"] = "revise"
        ctx.user_data["review_note_id"] = cid

        msg = _TextMessage("أضف المراجع")
        handled = await main.process_review_text(_MediaUpdate(self.admin_id, msg), ctx)
        self.assertTrue(handled)

        record = await database.get_contribution(cid)
        self.assertEqual(record[7], "needs_revision")
        self.assertEqual(record[11], "أضف المراجع")
        self.assertIn(self.student_id, {c["chat_id"] for c in ctx.bot.sent})

    async def test_typed_review_cancel_aborts(self):
        cid = await self._make_contribution()
        ctx = _FakeContext()
        ctx.user_data["review_note_kind"] = "reject"
        ctx.user_data["review_note_id"] = cid

        msg = _TextMessage("/cancel")
        handled = await main.process_review_text(_MediaUpdate(self.admin_id, msg), ctx)
        self.assertTrue(handled)
        self.assertEqual((await database.get_contribution(cid))[7], "pending")
        self.assertIsNone(ctx.user_data.get("review_note_kind"))

    async def test_process_review_text_noop_without_state(self):
        ctx = _FakeContext()
        msg = _TextMessage("hello")
        handled = await main.process_review_text(_MediaUpdate(self.admin_id, msg), ctx)
        self.assertFalse(handled)

    async def test_non_admin_cannot_apply_typed_reason(self):
        cid = await self._make_contribution()
        ctx = _FakeContext()
        ctx.user_data["review_note_kind"] = "reject"
        ctx.user_data["review_note_id"] = cid

        msg = _TextMessage("hijack")
        handled = await main.process_review_text(
            _MediaUpdate(self.student_id, msg), ctx
        )
        self.assertTrue(handled)
        self.assertEqual((await database.get_contribution(cid))[7], "pending")

    async def test_my_contributions_lists_and_offers_resubmit(self):
        cid = await self._make_contribution(status="needs_revision")

        query = _FakeQuery(self.student_id, "my_contributions")
        await main.callback_router(_FakeUpdate(query), _FakeContext())
        self.assertIn(str(cid), query.last_text)

    async def test_begin_resubmission_by_owner_arms_state(self):
        cid = await self._make_contribution(status="needs_revision")
        ctx = _FakeContext()
        query = _FakeQuery(self.student_id, f"resubmit:{cid}")
        await main.callback_router(_FakeUpdate(query), ctx)
        self.assertEqual(ctx.user_data.get("contribution_resubmit_id"), cid)

    async def test_begin_resubmission_by_non_owner_refused(self):
        cid = await self._make_contribution(status="needs_revision")
        ctx = _FakeContext()
        query = _FakeQuery(777, f"resubmit:{cid}")
        await main.callback_router(_FakeUpdate(query), ctx)
        self.assertIsNone(ctx.user_data.get("contribution_resubmit_id"))

    async def test_resubmission_media_flow_reopens_contribution(self):
        cid = await self._make_contribution(status="needs_revision")
        ctx = _FakeContext()

        begin = _FakeQuery(self.student_id, f"resubmit:{cid}")
        await main.callback_router(_FakeUpdate(begin), ctx)

        msg = _MediaMessage(document=_FakeDoc("fid-fixed", "fixed.pdf"))
        handled = await main.contribution_media_handler(
            _MediaUpdate(self.student_id, msg), ctx
        )
        self.assertTrue(handled)

        record = await database.get_contribution(cid)
        self.assertEqual(record[7], "pending")
        self.assertEqual(record[5], "fid-fixed")
        self.assertIsNone(ctx.user_data.get("contribution_resubmit_id"))

    async def test_review_screen_blocks_stale_review(self):
        cid = await self._make_contribution(status="approved")
        ctx = _FakeContext()
        query = _FakeQuery(self.admin_id, f"revise:{cid}")
        await main.callback_router(_FakeUpdate(query), ctx)
        # Approved contributions cannot be sent back for revision.
        self.assertIsNone(ctx.user_data.get("review_note_kind"))

    async def test_cancel_command_clears_contribution_states(self):
        ctx = _FakeContext()
        ctx.user_data["review_note_kind"] = "reject"
        ctx.user_data["review_note_id"] = 5
        ctx.user_data["contribution_folder"] = self.folder_id

        msg = _TextMessage("/cancel")
        await main.cancel_command(_MediaUpdate(self.admin_id, msg), ctx)

        self.assertIsNone(ctx.user_data.get("review_note_kind"))
        self.assertIsNone(ctx.user_data.get("contribution_folder"))

    async def test_home_clears_armed_review_prompt(self):
        cid = await self._make_contribution()
        ctx = _FakeContext()
        ctx.user_data["review_note_kind"] = "reject"
        ctx.user_data["review_note_id"] = cid

        go_home = _FakeQuery(self.admin_id, "home")
        await main.callback_router(_FakeUpdate(go_home), ctx)

        self.assertIsNone(ctx.user_data.get("review_note_kind"))
        # A later stray text must not silently reject the contribution.
        msg = _TextMessage("some unrelated text")
        handled = await main.process_review_text(_MediaUpdate(self.admin_id, msg), ctx)
        self.assertFalse(handled)
        self.assertEqual((await database.get_contribution(cid))[7], "pending")


if __name__ == "__main__":
    unittest.main()
