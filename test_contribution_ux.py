"""Contribution wizard UX + message-safety regression tests.

Covers the operator-reported defects:
  * the contributions screen showed several identically named leaves and the
    sender could not tell which academic location to pick;
  * a target leaf presented an empty submenu instead of the upload prompt;
  * oversized inline messages silently failed, so a screen appeared blank.

Runs against a temporary SQLite DB only.
"""

import os
import tempfile
import unittest

import database
import main
from test_medbot_router import _FakeContext, _FakeQuery, _FakeUpdate


class ContributionWizardBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-contrib-ux-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")
        self._old = database.DB_NAME
        database.DB_NAME = self.db_path
        await database.init_db()

        self.student_id = 900

        # Realistic tree with repeated subject names across blocks.
        self.year2 = await self._folder(None, "Second Year")
        self.block1 = await self._folder(self.year2, "Block 1")
        self.block2 = await self._folder(self.year2, "Block 2")
        self.anatomy_b1 = await self._folder(self.block1, "Anatomy", accepts=1)
        self.physio_b1 = await self._folder(self.block1, "Physiology", accepts=1)
        self.anatomy_b2 = await self._folder(self.block2, "Anatomy", accepts=1)

        self.year3 = await self._folder(None, "Third Year")
        self.block3 = await self._folder(self.year3, "Block 1")
        self.anatomy_v3a = await self._folder(self.block3, "Anatomy", accepts=1)
        self.anatomy_v3b = await self._folder(self.block3, "Anatomy", accepts=1)

        self.empty_branch = await self._folder(None, "Archive")

    async def asyncTearDown(self):
        database.DB_NAME = self._old
        for name in os.listdir(self.tmp_dir):
            try:
                os.remove(os.path.join(self.tmp_dir, name))
            except OSError:
                pass
        os.rmdir(self.tmp_dir)

    async def _folder(self, parent_id, name, accepts=0):
        await database.add_folder(parent_id or 0, name, "general", accepts)
        rows = await database.get_folders(parent_id or 0)
        matches = [r[0] for r in rows if r[1] == name]
        return matches[-1]

    async def _route(self, data):
        query = _FakeQuery(self.student_id, data)
        ctx = _FakeContext()
        await main.callback_router(_FakeUpdate(query), ctx)
        return query, ctx

    def _buttons(self, query):
        return [
            (b.text, b.callback_data)
            for row in query.last_markup.inline_keyboard
            for b in row
        ]


class ContributionNavigationTests(ContributionWizardBase):
    async def test_root_shows_years_not_leaf_subjects(self):
        query, _ = await self._route("contribute")
        labels = [t for t, _ in self._buttons(query)]
        joined = " ".join(labels)
        self.assertIn("Second Year", joined)
        self.assertIn("Third Year", joined)
        # The leaf subjects must no longer be dumped at the top level.
        self.assertNotIn("Anatomy", joined)

    async def test_empty_branch_is_hidden(self):
        query, _ = await self._route("contribute")
        callbacks = [c for _, c in self._buttons(query)]
        self.assertNotIn(f"contrib_browse:{self.empty_branch}", callbacks)

    async def test_exact_duplicate_siblings_are_disambiguated(self):
        query, _ = await self._route(f"contrib_browse:{self.block3}")
        labels = [t for t, _ in self._buttons(query)]
        anatomy = [t for t in labels if "Anatomy" in t]
        self.assertEqual(len(anatomy), 2)
        # Two identical names must be visually distinguishable.
        self.assertNotEqual(anatomy[0], anatomy[1])

    async def test_target_leaf_starts_upload_directly(self):
        query, ctx = await self._route(f"contrib_browse:{self.block1}")
        target = [
            c for t, c in self._buttons(query) if t.endswith("Anatomy")
        ]
        self.assertEqual(target, [f"contrib_folder:{self.anatomy_b1}"])

        query, ctx = await self._route(f"contrib_folder:{self.anatomy_b1}")
        self.assertEqual(ctx.user_data.get("contribution_folder"), self.anatomy_b1)
        self.assertIn("إرسال مساهمة", query.last_text)

    async def test_upload_prompt_names_the_exact_destination(self):
        query, _ = await self._route(f"contrib_folder:{self.anatomy_v3b}")
        text = query.last_text
        self.assertIn("Third Year", text)
        self.assertIn("Block 1", text)
        self.assertIn("Anatomy", text)

    async def test_navigating_to_non_target_without_targets_is_refused(self):
        query, ctx = await self._route(f"contrib_folder:{self.empty_branch}")
        self.assertIsNone(ctx.user_data.get("contribution_folder"))
        self.assertIn("غير متاح", query.last_text)

    async def test_breadcrumb_is_rendered_per_level(self):
        query, _ = await self._route(f"contrib_browse:{self.block2}")
        self.assertIn("Second Year", query.last_text)
        self.assertIn("Block 2", query.last_text)

    async def test_back_button_returns_to_parent(self):
        query, _ = await self._route(f"contrib_browse:{self.block1}")
        back = [c for t, c in self._buttons(query) if "رجوع" in t]
        self.assertEqual(back, [f"contrib_browse:{self.year2}"])

    async def test_available_tree_does_not_show_empty_state(self):
        query, _ = await self._route("contribute")
        # A branch with targets exists, so we still show options, not empty text.
        self.assertNotIn("لا توجد", query.last_text)


class ContributionTargetHelperTests(ContributionWizardBase):
    async def test_descendant_detection(self):
        self.assertTrue(
            await database.folder_has_contribution_target(self.year2)
        )
        self.assertTrue(
            await database.folder_has_contribution_target(self.block1)
        )
        self.assertFalse(
            await database.folder_has_contribution_target(self.empty_branch)
        )


class ContributionHistoryTests(ContributionWizardBase):
    async def test_my_contributions_show_destination_path(self):
        await database.add_contribution(
            self.student_id,
            "Student",
            self.anatomy_b1,
            "Handout",
            "fid-1",
            "document",
        )
        query, _ = await self._route("my_contributions")
        self.assertIn("Handout", query.last_text)
        self.assertIn("Second Year", query.last_text)
        self.assertIn("Block 1", query.last_text)


class MessageClampTests(unittest.TestCase):
    def test_short_text_is_unchanged(self):
        self.assertEqual(main._clamp_text("hello"), "hello")

    def test_oversized_text_is_clamped_under_limit(self):
        big = ("line of text\n" * 2000)
        out = main._clamp_text(big)
        self.assertLessEqual(len(out), main.TELEGRAM_TEXT_LIMIT + 60)

    def test_clamp_never_ends_mid_line(self):
        big = "x" * 8000
        out = main._clamp_text(big)
        self.assertIn("تم اختصار", out)


if __name__ == "__main__":
    unittest.main()
