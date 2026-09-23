"""Intent-aware search tests for MEDBOT.

Verifies that search understands abbreviations and synonyms (CBC ->
Complete Blood Count -> blood count/hematology), searches real registered
rows only, and never invents resources or folders.

Runs against a temporary SQLite database; no network, keys, or mocks.
"""

import asyncio
import os
import tempfile
import unittest

import database
import search_engine


class SearchIntentTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-intent-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")

        self._old_db = database.DB_NAME
        self._old_search_db = search_engine.DB_NAME

        database.DB_NAME = self.db_path
        search_engine.DB_NAME = self.db_path

        asyncio.run(database.init_db())

        # Registered structure. Deliberately, no title contains "CBC".
        self.heme_id = asyncio.run(
            database.add_folder(0, "Hematology", "general")
        )
        asyncio.run(
            database.add_content(
                self.heme_id,
                "Blood Count Interpretation",
                "f-1",
                "document",
            )
        )
        self.anatomy_id = asyncio.run(
            database.add_folder(0, "Anatomy", "general")
        )
        asyncio.run(
            database.add_content(self.anatomy_id, "Upper Limb", "f-2", "document")
        )

    def tearDown(self):
        database.DB_NAME = self._old_db
        search_engine.DB_NAME = self._old_search_db

        import shutil

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _search(self, query, limit=15):
        return asyncio.run(search_engine.search_library_summary(query, limit))

    def test_cbc_abbreviation_reaches_hematology(self):
        summary = self._search("CBC")
        titles = {r["title"] for r in summary["results"]}
        self.assertIn("Hematology", titles)
        self.assertIn("Blood Count Interpretation", titles)

    def test_cbc_expands_through_synonyms(self):
        for query in ("CBC", "complete blood count", "blood count", "hematology"):
            summary = self._search(query)
            titles = {r["title"] for r in summary["results"]}
            self.assertIn("Hematology", titles, msg=f"query={query}")
            self.assertIn(
                "Blood Count Interpretation",
                titles,
                msg=f"query={query}",
            )

    def test_arabic_synonym_matches_english_resource(self):
        summary = self._search("تحليل الدم")
        titles = {r["title"] for r in summary["results"]}
        self.assertIn("Hematology", titles)
        self.assertIn("Blood Count Interpretation", titles)

    def test_unrelated_query_returns_nothing(self):
        summary = self._search("zzz-totally-unrelated-token-zzz")
        self.assertEqual(summary["result_count"], 0)

    def test_results_are_real_registered_rows_only(self):
        summary = self._search("CBC")

        for result in summary["results"]:
            self.assertIn(result["result_type"], ("FOLDER", "EMPTY_FOLDER", "CONTENT"))
            # Every returned id must exist in the database.
            if result["result_type"] == "CONTENT":
                record = asyncio.run(database.get_file_record(result["id"]))
                self.assertIsNotNone(record)
                self.assertEqual(record[2], result["title"])
            else:
                folder = asyncio.run(database.get_folder(result["id"]))
                self.assertIsNotNone(folder)
                self.assertEqual(folder[2], result["title"])

    def test_exact_title_ranks_above_concept_match(self):
        summary = self._search("Anatomy")
        self.assertGreaterEqual(summary["result_count"], 1)
        self.assertEqual(summary["results"][0]["title"], "Anatomy")

    def test_empty_query_returns_nothing(self):
        self.assertEqual(self._search("   ")["result_count"], 0)

    def test_limit_is_respected(self):
        summary = self._search("CBC", limit=1)
        self.assertLessEqual(summary["result_count"], 1)


if __name__ == "__main__":
    unittest.main()
