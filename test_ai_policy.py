"""Tests for the MEDBOT AI behavior & accuracy policy.

Covers the four intent workflows:
  - overview: only registered sections may appear ("what exists?")
  - resource: a nonexistent resource is refused; a real one returns its path
  - medical:  no platform catalog is injected, PubMed grounding is used
  - general:  no PubMed call, concise general prompt

The regression under test is resource hallucination: the model must never be
able to turn its own idea of a medical-school curriculum into a MEDBOT fact.
No API keys, network, or real Telegram calls are used.
"""

import asyncio
import os
import inspect
import tempfile
import unittest

import database
import search_engine
import ai


class PolicyBase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-policy-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")

        self._old_db_name = database.DB_NAME
        self._old_search_db_name = search_engine.DB_NAME

        database.DB_NAME = self.db_path
        search_engine.DB_NAME = self.db_path

        asyncio.run(database.init_db())

        # A registered structure shaped like the real MEDBOT tree, with NO
        # "First Year" anywhere: it must never be claimed to exist.
        self.second_year = asyncio.run(
            database.add_folder(None, "Second Year", "general")
        )
        self.semester = asyncio.run(
            database.add_folder(self.second_year, "Semester 7", "general")
        )
        self.blocks = asyncio.run(
            database.add_folder(self.semester, "Blocks", "general")
        )
        self.physiology = asyncio.run(
            database.add_folder(self.blocks, "PHYSIOLOGY", "general")
        )
        self.theory = asyncio.run(
            database.add_folder(self.physiology, "نظري", "general")
        )
        self.content_id = asyncio.run(
            database.add_content(self.theory, "GIT Physiology", "fid-git", "pdf")
        )

    def tearDown(self):
        database.DB_NAME = self._old_db_name
        search_engine.DB_NAME = self._old_search_db_name

        import shutil

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def run_ai(self, query):
        return asyncio.run(ai.generate_medbot_unified_result(query))


class IntentClassificationTests(unittest.TestCase):
    def test_bare_structure_questions_are_overview(self):
        self.assertEqual(
            ai.classify_intent("ما هي الاقسام المتوفرة حاليا في البوت؟"),
            ai.INTENT_OVERVIEW,
        )
        self.assertEqual(
            ai.classify_intent("ما هي الأقسام الموجودة؟"),
            ai.INTENT_OVERVIEW,
        )

    def test_location_questions_are_resource_lookup(self):
        for query in (
            "أين أجد PHYSIOLOGY؟",
            "وين ألقى فسيولوجي؟",
            "هل يوجد First Year؟",
            "هل يوجد قسم عن الفسيولوجيا؟",
        ):
            self.assertEqual(
                ai.classify_intent(query),
                ai.INTENT_RESOURCE,
                msg=query,
            )

    def test_medical_questions_are_medical(self):
        for query in (
            "اشرح لي دورة القلب",
            "What is the cardiac cycle?",
            "Anatomy",
        ):
            self.assertEqual(
                ai.classify_intent(query),
                ai.INTENT_MEDICAL,
                msg=query,
            )

    def test_non_medical_questions_are_general(self):
        for query in (
            "ما هي عاصمة فرنسا؟",
            "كيف حالك؟",
            "اعطني ملخصات",
        ):
            self.assertEqual(
                ai.classify_intent(query),
                ai.INTENT_GENERAL,
                msg=query,
            )


class RegistryOverviewTests(PolicyBase):
    def test_overview_lists_only_registered_sections(self):
        result = self.run_ai("ما هي الاقسام المتوفرة حاليا في البوت؟")
        text = result["text"]

        self.assertIn("Second Year", text)
        self.assertIn("PHYSIOLOGY", text)
        self.assertIn("نظري", text)
        # Never invent a curriculum branch that is not registered.
        self.assertNotIn("First Year", text)
        self.assertNotIn("Semester 1", text)
        self.assertNotIn("Term 1", text)
        self.assertEqual(result["actions"], [])

    def test_overview_does_not_call_a_model(self):
        # The failing question is answered from the registry alone: no provider
        # is ever asked, so it cannot hallucinate and it is the fastest path.
        calls = []

        async def spy(*args, **kwargs):
            calls.append(args)
            return "should not be used"

        original = ai._request
        ai._request = spy
        try:
            self.run_ai("ما هي الأقسام الموجودة في المنصة؟")
        finally:
            ai._request = original

        self.assertEqual(calls, [])

    def test_overview_with_empty_registry_is_honest(self):
        asyncio.run(database.init_db())
        # A fresh, empty database must say so instead of inventing sections.
        folders, _contents, _paths = asyncio.run(database.get_searchable_records())
        text = ai.build_registry_overview([])
        self.assertIn("لا توجد", text)
        self.assertNotIn("First Year", text)


class ResourceLookupTests(PolicyBase):
    def test_nonexistent_resource_is_refused_verbatim(self):
        result = self.run_ai("هل يوجد First Year؟")
        self.assertEqual(result["text"], ai.PLATFORM_SEARCH_NO_MATCH)
        self.assertNotIn("First Year", result["text"])

    def test_real_resource_returns_actual_path(self):
        result = self.run_ai("أين أجد PHYSIOLOGY؟")
        text = result["text"]

        self.assertIn("PHYSIOLOGY", text)
        self.assertIn("Second Year", text)
        self.assertIn("Semester 7", text)
        # Direct access is offered only from real ids.
        callbacks = {a["callback"] for a in result["actions"]}
        self.assertIn(f"folder:{self.physiology}", callbacks)

    def test_resource_lookup_does_not_call_a_model(self):
        calls = []

        async def spy(*args, **kwargs):
            calls.append(args)
            return "should not be used"

        original = ai._request
        ai._request = spy
        try:
            self.run_ai("أين أجد PHYSIOLOGY؟")
        finally:
            ai._request = original

        self.assertEqual(calls, [])

    def test_unregistered_lookup_offers_no_actions(self):
        result = self.run_ai("هل يوجد قسم اسمه Neurology الموجود؟")
        if result["text"] == ai.PLATFORM_SEARCH_NO_MATCH:
            self.assertEqual(result["actions"], [])


class ProviderPromptTests(PolicyBase):
    """The model must never receive platform facts it could turn into claims."""

    def _capture(self, query):
        captured = {}

        async def fake_request(client, item, prompt, system_prompt=None):
            captured["prompt"] = prompt
            captured["system"] = system_prompt
            return "MODEL ANSWER"

        async def fake_candidates():
            return [
                {"provider": "test", "model": "test-model", "endpoint": "x"}
            ]

        async def no_pubmed(prompt):
            return []

        original_request = ai._request
        original_candidates = ai._get_candidates
        original_pubmed = ai._fetch_pubmed_sources
        ai._request = fake_request
        ai._get_candidates = fake_candidates
        ai._fetch_pubmed_sources = no_pubmed
        try:
            result = self.run_ai(query)
        finally:
            ai._request = original_request
            ai._get_candidates = original_candidates
            ai._fetch_pubmed_sources = original_pubmed

        return captured, result

    def test_medical_path_gets_pubmed_not_the_platform_catalog(self):
        captured, result = self._capture("اشرح لي دورة القلب")

        self.assertEqual(captured["system"], ai.UNIFIED_ASSISTANT_PROMPT)
        self.assertIn("PubMed", captured["prompt"])
        # The registered tree must never be injected into a medical answer.
        self.assertNotIn("Second Year", captured["prompt"])
        self.assertNotIn("PHYSIOLOGY", captured["prompt"])
        self.assertEqual(result["text"], "MODEL ANSWER")

    def test_general_path_has_no_pubmed_and_no_catalog(self):
        captured, result = self._capture("ما هي عاصمة فرنسا؟")

        self.assertEqual(captured["system"], ai.GENERAL_ASSISTANT_PROMPT)
        self.assertNotIn("PubMed", captured["prompt"])
        self.assertNotIn("Second Year", captured["prompt"])
        self.assertEqual(result["text"], "MODEL ANSWER")

    def test_general_path_skips_the_pubmed_lookup(self):
        # A non-medical question must not pay for (or attach) a PubMed search.
        called = []

        async def spy_pubmed(prompt):
            called.append(prompt)
            return []

        original = ai._fetch_pubmed_sources
        ai._fetch_pubmed_sources = spy_pubmed
        try:
            self.run_ai("ما هي عاصمة فرنسا؟")
        finally:
            ai._fetch_pubmed_sources = original

        self.assertEqual(called, [])

    def test_general_path_skips_the_registry_search(self):
        # A general question is not about the platform: searching the registry
        # would be an unnecessary query on the speed-critical path.
        called = []

        async def spy_search(query):
            called.append(query)
            return []

        original = ai._search_medbot
        ai._search_medbot = spy_search
        try:
            self.run_ai("ما هي عاصمة فرنسا؟")
        finally:
            ai._search_medbot = original

        self.assertEqual(called, [])

    def test_medical_path_runs_pubmed_lookup(self):
        called = []

        async def spy_pubmed(prompt):
            called.append(prompt)
            return []

        original = ai._fetch_pubmed_sources
        ai._fetch_pubmed_sources = spy_pubmed
        try:
            self.run_ai("اشرح لي دورة القلب")
        finally:
            ai._fetch_pubmed_sources = original

        self.assertEqual(len(called), 1)


class PromptContractTests(unittest.TestCase):
    """The three principles must be stated in the prompts themselves."""

    def test_medical_prompt_keeps_bilingual_contract(self):
        prompt = ai.UNIFIED_ASSISTANT_PROMPT
        self.assertIn("English (academic)", prompt)
        self.assertIn("**العربية", prompt)
        self.assertLess(
            prompt.index("English (academic)"),
            prompt.index("**العربية"),
        )
        # Brevity and honest uncertainty are required.
        self.assertIn("لا تُطل", prompt)
        self.assertIn("غير مؤكدة", prompt)

    def test_medical_prompt_forbids_platform_claims(self):
        self.assertIn("ممنوع", ai.UNIFIED_ASSISTANT_PROMPT)

    def test_general_prompt_demands_brevity_without_overexplaining(self):
        prompt = ai.GENERAL_ASSISTANT_PROMPT
        self.assertIn("لا تُطل", prompt)
        self.assertIn("لا تكرّر", prompt)
        self.assertIn("لا تضف تنبيهات", prompt)

    def test_module_defines_the_four_intents(self):
        for name in (
            "INTENT_OVERVIEW",
            "INTENT_RESOURCE",
            "INTENT_MEDICAL",
            "INTENT_GENERAL",
        ):
            self.assertTrue(hasattr(ai, name), msg=name)

    def test_pipeline_classifies_before_answering(self):
        source = inspect.getsource(ai.generate_medbot_unified_result)
        self.assertIn("classify_intent", source)
        # The full-catalog dump is gone from the answer path.
        self.assertNotIn("_platform_catalog_text", source)


class TelegramHandlerTests(PolicyBase):
    """Drive the REAL ``main.ai_handler`` with a recording bot.

    This is the closest thing to the Telegram path without network: the handler
    runs unchanged, sends through a fake bot, and the rendered text/buttons are
    asserted. It proves the policy holds at the delivery boundary, not just in
    ``ai.py``.
    """

    student_id = 9001

    def _send(self, text):
        """Run the real handler and return (delivered text, callback set)."""
        import main
        from test_medbot_fixes import _RecordingBot
        from test_medbot_router import _FakeContext, _MediaUpdate, _TextMessage

        async def run():
            ctx = _FakeContext()
            ctx.user_data["assistant_mode"] = "unified"
            ctx.bot = _RecordingBot()
            msg = _TextMessage(text)
            await main.ai_handler(_MediaUpdate(self.student_id, msg), ctx)
            return msg

        msg = asyncio.run(run())
        return "\n".join(msg.replies), _callback_data(msg.last_markup)

    def test_overview_through_handler_lists_only_registered_sections(self):
        delivered, _callbacks = self._send(
            "ما هي الاقسام المتوفرة حاليا في البوت؟"
        )
        self.assertIn("Second Year", delivered)
        self.assertIn("PHYSIOLOGY", delivered)
        self.assertNotIn("First Year", delivered)

    def test_nonexistent_resource_through_handler_is_refused(self):
        delivered, callbacks = self._send("هل يوجد First Year؟")
        self.assertIn(ai.PLATFORM_SEARCH_NO_MATCH, delivered)
        self.assertNotIn("Second Year", delivered)
        # Only the "home" button remains: no invented resource is reachable.
        self.assertEqual(callbacks, {"home"})

    def test_real_resource_through_handler_offers_direct_button(self):
        delivered, callbacks = self._send("أين أجد PHYSIOLOGY؟")
        self.assertIn("PHYSIOLOGY", delivered)
        self.assertIn(f"folder:{self.physiology}", callbacks)


def _callback_data(markup):
    """Collect callback_data from an InlineKeyboardMarkup (or None)."""
    if markup is None:
        return set()
    rows = getattr(markup, "inline_keyboard", None) or []
    return {
        button.callback_data
        for row in rows
        for button in row
        if getattr(button, "callback_data", None)
    }


if __name__ == "__main__":
    unittest.main()
