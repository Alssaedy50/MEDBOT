"""Tests for the MEDBOT-grounded AI layer.

Covers grounding (no hallucinated resources), the exact refusal string,
deterministic fallback when no provider is available, and the admin
bootstrap security rule.

No API keys, network, or real Telegram calls are used. A temporary SQLite
database is created and removed for each test.
"""

import asyncio
import os
import tempfile
import unittest

import database
import search_engine
import ai


class GroundingTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-grounding-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")

        self._old_db_name = database.DB_NAME
        self._old_search_db_name = search_engine.DB_NAME

        database.DB_NAME = self.db_path
        search_engine.DB_NAME = self.db_path

        asyncio.run(database.init_db())

        self.folder_id = asyncio.run(
            database.add_folder(None, "Anatomy", "general")
        )
        self.content_id = asyncio.run(
            database.add_content(
                self.folder_id,
                "Upper Limb Muscles",
                "file-abc",
                "document",
            )
        )

    def tearDown(self):
        database.DB_NAME = self._old_db_name
        search_engine.DB_NAME = self._old_search_db_name

        try:
            import shutil

            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        except Exception:
            pass

    def test_absent_resource_returns_exact_refusal(self):
        answer = asyncio.run(
            ai.generate_medbot_assistant_response(
                "zzz-this-resource-does-not-exist-zzz"
            )
        )
        self.assertEqual(answer, ai.NOT_REGISTERED_MESSAGE)

    def test_existing_resource_grounded_without_provider(self):
        # No API keys are configured in the test environment, so the pipeline
        # must fall back to deterministic, grounded library results instead of
        # inventing content.
        answer = asyncio.run(
            ai.generate_medbot_assistant_response("Anatomy")
        )

        self.assertNotEqual(answer, ai.NOT_REGISTERED_MESSAGE)
        self.assertIn("Anatomy", answer)
        self.assertIn("MEDBOT", answer)

    def test_grounded_answer_never_invents_unlisted_resource(self):
        answer = asyncio.run(
            ai.generate_medbot_assistant_response("Anatomy")
        )
        # A resource that does not exist must never appear in a grounded reply.
        self.assertNotIn("Pharmacology", answer)

    def test_build_library_context_empty(self):
        self.assertIn("لا توجد نتائج", ai.build_library_context([]))

    def test_validator_rejects_empty(self):
        validator = ai.GroundingValidator()
        self.assertFalse(validator.allows(""))
        self.assertFalse(validator.allows("   "))
        self.assertTrue(validator.allows("Anatomy / Upper Limb Muscles"))


class UnifiedAssistantTests(unittest.TestCase):
    """The single assistant answers questions AND navigates the platform.

    These tests run with no API keys, so the unified pipeline always takes its
    deterministic, grounded fallback path — which is exactly the path that
    must never invent a resource.
    """

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-unified-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")

        self._old_db_name = database.DB_NAME
        self._old_search_db_name = search_engine.DB_NAME

        database.DB_NAME = self.db_path
        search_engine.DB_NAME = self.db_path

        asyncio.run(database.init_db())

        self.folder_id = asyncio.run(
            database.add_folder(None, "Anatomy", "general")
        )
        self.content_id = asyncio.run(
            database.add_content(
                self.folder_id,
                "Upper Limb Muscles",
                "file-abc",
                "document",
            )
        )

    def tearDown(self):
        database.DB_NAME = self._old_db_name
        search_engine.DB_NAME = self._old_search_db_name

        import shutil

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_empty_prompt_is_rejected(self):
        answer = asyncio.run(ai.generate_medbot_unified_response("   "))
        self.assertIn("سؤال واضح", answer)

    def test_no_provider_returns_grounded_results_for_known_resource(self):
        answer = asyncio.run(ai.generate_medbot_unified_response("Anatomy"))
        self.assertIn("Anatomy", answer)
        self.assertIn("MEDBOT", answer)
        # The unfound resource must never be invented.
        self.assertNotIn("Pharmacology", answer)

    def test_no_provider_and_no_match_does_not_invent(self):
        answer = asyncio.run(
            ai.generate_medbot_unified_response("zzz-does-not-exist-zzz")
        )
        self.assertNotIn("zzz-does-not-exist-zzz", answer)
        self.assertIn("MEDBOT", answer)

    def test_catalog_renders_registered_folder_and_content(self):
        folders, contents, paths = asyncio.run(
            database.get_searchable_records()
        )
        catalog = ai.build_platform_catalog(folders, contents, paths)
        self.assertIn("Anatomy", catalog)
        self.assertIn("Upper Limb Muscles", catalog)
        self.assertIn("الرئيسية", catalog)

    def test_catalog_empty_library(self):
        catalog = ai.build_platform_catalog([], [], {})
        self.assertIn("لا تحتوي", catalog)

    def test_catalog_is_bounded(self):
        folders = [
            (i, None, f"Section {i}", "general")
            for i in range(1, 5000)
        ]
        catalog = ai.build_platform_catalog(folders, [], {})
        self.assertLessEqual(len(catalog), ai.CATALOG_MAX_CHARS + 200)


class AdminBootstrapSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-admin-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")

        self._old_db_name = database.DB_NAME
        database.DB_NAME = self.db_path

        asyncio.run(database.init_db())

    def tearDown(self):
        database.DB_NAME = self._old_db_name

        try:
            import shutil

            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        except Exception:
            pass

    def test_zero_id_never_promotes(self):
        self.assertFalse(
            asyncio.run(database.ensure_configured_admin(0))
        )
        self.assertFalse(
            asyncio.run(database.ensure_configured_admin(-1))
        )
        self.assertFalse(asyncio.run(database.is_user_admin(0)))

    def test_configured_id_is_promoted_idempotently(self):
        self.assertTrue(
            asyncio.run(database.ensure_configured_admin(999, "owner"))
        )
        self.assertTrue(asyncio.run(database.is_user_admin(999)))

        # Re-running must not error or duplicate.
        self.assertTrue(
            asyncio.run(database.ensure_configured_admin(999, "owner"))
        )

    def test_first_user_not_auto_promoted(self):
        # A user registering is never granted admin without configuration.
        asyncio.run(database.register_user(111, "first", "First User"))
        self.assertFalse(asyncio.run(database.is_user_admin(111)))


if __name__ == "__main__":
    unittest.main()
