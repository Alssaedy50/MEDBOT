"""Tests for the two separated MEDBOT assistant modes.

MODE 1 — PLATFORM RESOURCE SEARCH
    Understands natural-language Arabic/English resource queries and returns
    short answers plus one-tap access buttons built ONLY from verified
    registry ids. It may read the complete catalog, but it can never invent a
    section, subject, path, or Telegram target.

MODE 2 — AI CHAT
    Conversational answers. A medical question gets an English academic answer
    plus a concise Arabic explanation and PubMed grounding; a general question
    is answered concisely. It never touches the platform registry.

Runs against a temporary SQLite database; no network, keys, or real Telegram
calls. Provider functions are replaced so routing can be inspected, mirroring
the existing AI-policy test style (no business logic is mocked).
"""

import asyncio
import os
import tempfile
import unittest

import ai
import database
import main
import search_engine


class ModeBase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="medbot-modes-")
        self.db_path = os.path.join(self.tmp_dir, "test.sqlite3")

        self._old_db_name = database.DB_NAME
        self._old_search_db_name = search_engine.DB_NAME

        database.DB_NAME = self.db_path
        search_engine.DB_NAME = self.db_path

        asyncio.run(database.init_db())

        # A registered structure shaped like the real MEDBOT tree, with NO
        # "First Year" anywhere: it must never be claimed to exist.
        second_year = asyncio.run(
            database.add_folder(None, "Second Year", "general")
        )
        semester = asyncio.run(
            database.add_folder(second_year, "Semester 7", "general")
        )
        blocks = asyncio.run(
            database.add_folder(semester, "Blocks", "general")
        )

        self.micro = asyncio.run(
            database.add_folder(blocks, "Microbiology", "general")
        )
        self.micro_practical = asyncio.run(
            database.add_folder(self.micro, "عملي", "general")
        )
        self.micro_lectures = asyncio.run(
            database.add_folder(self.micro, "محاضرات", "general")
        )
        self.micro_file = asyncio.run(
            database.add_content(
                self.micro_practical, "Micro practical file", "f1", "pdf"
            )
        )

        self.histo = asyncio.run(
            database.add_folder(blocks, "HISTOLOGY", "general")
        )
        self.histo_file = asyncio.run(
            database.add_content(
                self.histo, "Epithelium lecture", "f2", "pdf"
            )
        )

        self.physio = asyncio.run(
            database.add_folder(blocks, "PHYSIOLOGY", "general")
        )
        self.physio_file = asyncio.run(
            database.add_content(self.physio, "GIT Physiology", "f3", "pdf")
        )

        # Provider/search doubles are installed per-test; remember originals.
        self._orig_request = ai._request
        self._orig_candidates = ai._get_candidates
        self._orig_pubmed = ai._fetch_pubmed_sources
        self._orig_search = ai._search_medbot

    def tearDown(self):
        ai._request = self._orig_request
        ai._get_candidates = self._orig_candidates
        ai._fetch_pubmed_sources = self._orig_pubmed
        ai._search_medbot = self._orig_search

        database.DB_NAME = self._old_db_name
        search_engine.DB_NAME = self._old_search_db_name

        import shutil

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # -- provider doubles --------------------------------------------------

    def install_provider(self, answer="MODEL ANSWER", calls=None):
        """Replace the model with a recording stub answering ``answer``."""
        calls = calls if calls is not None else {}

        async def fake_candidates():
            return [{"provider": "test", "model": "test-model", "endpoint": "x"}]

        async def fake_request(client, item, prompt, system_prompt=None):
            calls.setdefault("prompts", []).append(prompt)
            calls.setdefault("systems", []).append(system_prompt)
            return answer

        ai._get_candidates = fake_candidates
        ai._request = fake_request
        return calls

    # -- helpers -----------------------------------------------------------

    def search(self, query):
        return asyncio.run(ai.generate_platform_search_result(query))

    def chat(self, query):
        return asyncio.run(ai.generate_ai_chat_result(query))

    def real_callbacks(self, actions):
        """Assert every action callback resolves to a real registered row."""
        for action in actions:
            kind, _, item_id = action["callback"].partition(":")
            self.assertIn(kind, ("folder", "file"))
            self.assertTrue(item_id.isdigit(), action)
            if kind == "folder":
                row = asyncio.run(database.get_folder(int(item_id)))
            else:
                row = asyncio.run(database.get_file_record(int(item_id)))
            self.assertIsNotNone(row, action)


# ---------------------------------------------------------------------------
# MODE 1 — Platform resource search
# ---------------------------------------------------------------------------

class PlatformSearchTests(ModeBase):
    def test_overview_lists_only_registered_sections(self):
        result = self.search("ما هي الاقسام الموجودة؟")
        text = result["text"]

        for expected in ("Second Year", "HISTOLOGY", "Microbiology", "PHYSIOLOGY"):
            self.assertIn(expected, text)
        # Never invent a curriculum branch that is not registered.
        self.assertNotIn("First Year", text)
        self.assertEqual(result["actions"], [])

    def test_overview_does_not_call_a_model_or_search(self):
        calls = self.install_provider()
        searches = []

        async def spy_search(query):
            searches.append(query)
            return []

        ai._search_medbot = spy_search
        self.search("ما هي الأقسام الموجودة؟")

        self.assertEqual(calls.get("prompts", []), [])
        self.assertEqual(searches, [])

    def test_histology_content_query_finds_real_section_and_file(self):
        result = self.search("ماهو محتوى قسم الهستو؟")
        text = result["text"]

        self.assertIn("HISTOLOGY", text)
        self.assertIn("Second Year", text)
        callbacks = {a["callback"] for a in result["actions"]}
        self.assertIn(f"folder:{self.histo}", callbacks)
        self.assertIn(f"file:{self.histo_file}", callbacks)
        self.real_callbacks(result["actions"])

    def test_micro_practical_query_returns_verified_buttons(self):
        result = self.search("وين أحد موارد الميكرو العملي؟")
        text = result["text"]

        self.assertIn("Microbiology", text)
        self.assertIn("عملي", text)
        callbacks = {a["callback"] for a in result["actions"]}
        self.assertIn(f"folder:{self.micro_practical}", callbacks)
        self.real_callbacks(result["actions"])

    def test_physiology_query_returns_real_path_and_button(self):
        result = self.search("وين الفسيولوجي؟")
        text = result["text"]

        self.assertIn("PHYSIOLOGY", text)
        self.assertIn("Second Year", text)
        self.assertIn("Semester 7", text)
        self.assertIn(
            f"folder:{self.physio}", {a["callback"] for a in result["actions"]}
        )

    def test_nonexistent_subject_is_not_hallucinated(self):
        result = self.search("هل يوجد قسم اسمه Neurology؟")
        text = result["text"]

        self.assertEqual(text, ai.PLATFORM_SEARCH_NO_MATCH)
        self.assertNotIn("Neurology", text)
        self.assertEqual(result["actions"], [])

    def test_colloquial_arabic_still_resolves_real_resources(self):
        result = self.search("حاجة زي الميكرو العملي")
        text = result["text"]

        self.assertIn("Microbiology", text)
        self.assertNotEqual(text, ai.PLATFORM_SEARCH_NO_MATCH)
        self.real_callbacks(result["actions"])

    def test_multiple_matches_yield_multiple_verified_buttons(self):
        result = self.search("وين ملفات الميكرو؟")
        callbacks = {a["callback"] for a in result["actions"]}

        self.assertGreaterEqual(len(callbacks), 2)
        self.assertIn(f"folder:{self.micro}", callbacks)
        self.assertLessEqual(len(result["actions"]), ai.MAX_RESULT_ACTIONS)
        self.real_callbacks(result["actions"])

    def test_fast_path_calls_no_model_when_deterministic_match_exists(self):
        calls = self.install_provider()
        self.search("وين الفسيولوجي؟")
        self.assertEqual(calls.get("prompts", []), [])

    def test_no_match_degrades_honestly(self):
        # No provider installed in CI: a hard query must degrade honestly.
        result = self.search("zzz-لا-يوجد-هنا-zzz")
        self.assertEqual(result["text"], ai.PLATFORM_SEARCH_NO_MATCH)
        self.assertEqual(result["actions"], [])


class CatalogFallbackTests(ModeBase):
    """The model may select from the real catalog — never create in it."""

    def test_invented_entity_is_discarded_and_real_one_kept(self):
        folders, contents, paths = asyncio.run(ai._load_registry())

        answer = (
            "وجدت قسم Microbiology عملي، وكذلك قسم Neurology غير الموجود "
            "وقسم First Year."
        )
        verified = ai._verify_catalog_answer(answer, folders, contents, paths)
        titles = {v["title"] for v in verified}

        self.assertIn("Microbiology", titles)
        self.assertIn("عملي", titles)
        # Invented entities are never surfaced.
        self.assertNotIn("Neurology", titles)
        self.assertNotIn("First Year", titles)

    def test_fallback_bridges_colloquial_to_real_english_title(self):
        # The deterministic pass misses "الجراثيم"; the catalog fallback lets
        # the model bridge it to the real English title.
        self.install_provider(
            answer="قسم Microbiology يحتوي على موارد، وتحديداً عملي."
        )
        result = self.search("ابحث عن الجراثيم")
        callbacks = {a["callback"] for a in result["actions"]}

        self.assertIn(f"folder:{self.micro}", callbacks)
        self.real_callbacks(result["actions"])

    def test_fallback_with_only_invented_entities_is_refused(self):
        self.install_provider(answer="ستجده في First Year ثم Semester 1.")
        result = self.search("ابحث عن الفصل الأول")

        self.assertEqual(result["text"], ai.PLATFORM_SEARCH_NO_MATCH)
        self.assertEqual(result["actions"], [])

    def test_fallback_does_not_run_without_provider(self):
        # No provider: the query degrades to the honest no-match answer.
        result = self.search("ابحث عن الجراثيم")
        self.assertEqual(result["text"], ai.PLATFORM_SEARCH_NO_MATCH)


# ---------------------------------------------------------------------------
# MODE 2 — AI Chat
# ---------------------------------------------------------------------------

class AiChatRoutingTests(ModeBase):
    def _capture(self, query):
        calls = self.install_provider()
        search_calls = []
        pubmed_calls = []

        async def spy_search(q):
            search_calls.append(q)
            return []

        async def spy_pubmed(q):
            pubmed_calls.append(q)
            return []

        ai._search_medbot = spy_search
        ai._fetch_pubmed_sources = spy_pubmed
        result = self.chat(query)
        return calls, search_calls, pubmed_calls, result

    def test_english_medical_question_uses_medical_contract(self):
        calls, search_calls, pubmed_calls, result = self._capture(
            "What is the cardiac cycle?"
        )

        self.assertEqual(calls["systems"][-1], ai.UNIFIED_ASSISTANT_PROMPT)
        self.assertIn("PubMed", calls["prompts"][-1])
        self.assertEqual(len(pubmed_calls), 1)
        # Chat never searches or exposes the platform registry.
        self.assertEqual(search_calls, [])
        self.assertEqual(result["actions"], [])

    def test_arabic_medical_question_uses_medical_contract(self):
        calls, search_calls, pubmed_calls, result = self._capture(
            "اشرح لي Action Potential"
        )

        self.assertEqual(calls["systems"][-1], ai.UNIFIED_ASSISTANT_PROMPT)
        self.assertEqual(len(pubmed_calls), 1)
        self.assertEqual(search_calls, [])
        self.assertEqual(result["actions"], [])

    def test_general_question_uses_general_contract_without_pubmed(self):
        calls, search_calls, pubmed_calls, result = self._capture(
            "What is the capital of France?"
        )

        self.assertEqual(calls["systems"][-1], ai.GENERAL_ASSISTANT_PROMPT)
        self.assertNotIn("PubMed", calls["prompts"][-1])
        self.assertEqual(pubmed_calls, [])
        self.assertEqual(search_calls, [])
        self.assertEqual(result["actions"], [])

    def test_general_question_never_queries_the_registry(self):
        calls, search_calls, _pubmed, _result = self._capture("كيف حالك؟")
        self.assertEqual(search_calls, [])
        self.assertEqual(calls["systems"][-1], ai.GENERAL_ASSISTANT_PROMPT)

    def test_medical_question_does_not_expose_platform_structure(self):
        calls, search_calls, _pubmed, result = self._capture(
            "اشرح لي دورة القلب"
        )

        self.assertEqual(search_calls, [])
        prompt = calls["prompts"][-1]
        for platform_fact in (
            "Second Year", "Semester 7", "PHYSIOLOGY", "HISTOLOGY"
        ):
            self.assertNotIn(platform_fact, prompt)
        self.assertEqual(result["actions"], [])

    def test_chat_never_returns_platform_actions(self):
        self.install_provider()
        for query in (
            "What is the cardiac cycle?",
            "What is the capital of France?",
        ):
            self.assertEqual(self.chat(query)["actions"], [])


class ChatNoProviderTests(ModeBase):
    def test_medical_no_provider_is_honest(self):
        result = self.chat("اشرح لي دورة القلب")
        self.assertIn("الذكاء الاصطناعي", result["text"])
        self.assertEqual(result["actions"], [])

    def test_general_no_provider_is_honest(self):
        result = self.chat("ما هي عاصمة فرنسا؟")
        self.assertIn("الذكاء الاصطناعي", result["text"])

    def test_empty_prompt_is_rejected(self):
        self.assertIn("سؤال واضح", self.chat("   ")["text"])


# ---------------------------------------------------------------------------
# Local anti-repetition output guard
# ---------------------------------------------------------------------------

_REPEATED_LINE = (
    "The cardiac cycle consists of systole and diastole phases."
)


class RepetitionGuardTests(ModeBase):
    """Local output guard: fix repetition without capping legitimate length."""

    def setUp(self):
        super().setUp()

        async def no_pubmed(query):
            return []

        # Keep the medical path offline: the guard is what is under test.
        ai._fetch_pubmed_sources = no_pubmed

    def test_repeated_line_is_collapsed(self):
        repeated = "\n".join([_REPEATED_LINE] * 6)
        cleaned = ai._guard_answer(repeated)
        self.assertEqual(cleaned.split("\n"), [_REPEATED_LINE])
        self.assertFalse(ai._has_repetition(cleaned))

    def test_repeated_paragraph_is_collapsed(self):
        para = (
            "Cardiac output is the product of heart rate and stroke volume, "
            "and it reflects the total blood pumped per minute."
        )
        repeated = "\n\n".join([para] * 4)
        cleaned = ai._guard_answer(repeated)
        self.assertEqual(cleaned.split("\n\n"), [para])
        self.assertFalse(ai._has_repetition(cleaned))

    def test_repeated_table_row_is_collapsed(self):
        row = "| Drug | Dose | Route |"
        repeated = "\n".join([row] * 5)
        cleaned = ai._guard_answer(repeated)
        self.assertEqual(cleaned.split("\n"), [row])
        self.assertFalse(ai._has_repetition(cleaned))

    def test_long_varied_answer_passes_untouched(self):
        long_answer = "\n".join(
            f"Step {i}: a distinct physiological detail number {i} explained "
            f"with its own wording about system {i}."
            for i in range(40)
        )
        self.assertFalse(ai._has_repetition(long_answer))
        self.assertEqual(ai._guard_answer(long_answer), long_answer)

    def test_bilingual_medical_answer_passes_untouched(self):
        answer = (
            "🩺 Cardiac cycle\n"
            "**English (academic):**\n"
            "The cardiac cycle is the sequence of electrical and mechanical "
            "events occurring during one complete heartbeat.\n"
            "**العربية — شرح مختصر:**\n"
            "هي سلسلة الأحداث الكهربائية والميكانيكية خلال نبضة قلب واحدة."
        )
        self.assertFalse(ai._has_repetition(answer))
        self.assertEqual(ai._guard_answer(answer), answer)

    def test_short_repeated_bullets_are_not_touched(self):
        # Tiny, genuinely distinct list items must never be merged.
        answer = "\n".join(["- a", "- b", "- c", "- a", "- b"])
        self.assertEqual(ai._guard_answer(answer), answer)

    def test_guard_is_local_and_does_not_call_db_or_network(self):
        """The normal path must not gain DB/network calls."""
        calls = {"db": 0}

        async def spy_get_db(*a, **k):
            calls["db"] += 1
            raise AssertionError("guard must not touch the database")

        original = database.get_db
        database.get_db = spy_get_db
        try:
            ai._guard_answer("\n".join([_REPEATED_LINE] * 5))
            ai._has_repetition("\n".join([_REPEATED_LINE] * 5))
        finally:
            database.get_db = original
        self.assertEqual(calls["db"], 0)

    # -- integration with the provider failover ----------------------------

    def chat_with_answer(self, answer, query):
        calls = self.install_provider(answer=answer)
        result = self.chat(query)
        return calls, result

    def test_repetition_is_repaired_before_delivery(self):
        repeated = "\n".join([_REPEATED_LINE] * 5)
        calls, result = self.chat_with_answer(
            repeated, "What is the cardiac cycle?"
        )
        self.assertEqual(result["text"].split("\n"), [_REPEATED_LINE])
        # A locally repaired answer needs no regeneration.
        self.assertEqual(len(calls["prompts"]), 1)

    # A repeated two-block sequence: the local line/paragraph pass cannot merge
    # non-adjacent blocks, but the guard still detects the repetition, which is
    # exactly what should trigger a single regeneration.
    _INTERLEAVED_REPEAT = "\n\n".join(
        [
            "alpha beta gamma delta epsilon",
            "zeta eta theta iota kappa",
        ]
        * 3
    )

    def test_persistent_repetition_triggers_exactly_one_regeneration(self):
        # The retry is clean; exactly one regeneration must happen (no loop).
        repeated = self._INTERLEAVED_REPEAT

        calls = {"prompts": [], "systems": []}
        answers = iter([
            repeated,          # first generation: repetitive
            "🩺 Clean answer.\n**English (academic):**\nDone.\n"
            "**العربية — شرح مختصر:**\nتم.",  # retry: clean
        ])

        async def fake_candidates():
            return [{"provider": "test", "model": "test-model", "endpoint": "x"}]

        async def fake_request(client, item, prompt, system_prompt=None):
            calls["prompts"].append(prompt)
            calls["systems"].append(system_prompt)
            return next(answers)

        ai._get_candidates = fake_candidates
        ai._request = fake_request

        result = self.chat("What is the cardiac cycle?")

        # Exactly one regeneration: two calls total, never a loop.
        self.assertEqual(len(calls["prompts"]), 2)
        self.assertIn("دون أي تكرار", calls["prompts"][-1])
        self.assertIn("Clean answer", result["text"])

    def test_failed_regeneration_falls_back_to_locally_cleaned_answer(self):
        # Both attempts repetitive: the locally repaired first answer is kept
        # (never an empty reply, never an infinite retry).
        repeated = "\n".join([_REPEATED_LINE + " again"] * 6)
        interleaved = self._INTERLEAVED_REPEAT

        calls = {"prompts": []}

        async def fake_candidates():
            return [{"provider": "test", "model": "test-model", "endpoint": "x"}]

        async def fake_request(client, item, prompt, system_prompt=None):
            calls["prompts"].append(prompt)
            return interleaved

        ai._get_candidates = fake_candidates
        ai._request = fake_request

        result = self.chat("What is the cardiac cycle?")
        # One generation plus one bounded regeneration, then stop.
        self.assertEqual(len(calls["prompts"]), 2)
        self.assertEqual(result["text"], interleaved.strip())


# ---------------------------------------------------------------------------
# Prompt / architecture contracts
# ---------------------------------------------------------------------------

class ModeContractTests(unittest.TestCase):
    def test_platform_search_prompt_forbids_platform_invention(self):
        prompt = ai.PLATFORM_SEARCH_PROMPT
        self.assertIn("لا يجوز لك اختلاق", prompt)
        self.assertIn("الدليل", prompt)
        # The model must never mint navigation targets.
        self.assertIn("callbacks", prompt)

    def test_medical_prompt_keeps_bilingual_contract(self):
        prompt = ai.UNIFIED_ASSISTANT_PROMPT
        self.assertIn("English (academic)", prompt)
        self.assertIn("**العربية", prompt)
        self.assertLess(
            prompt.index("English (academic)"),
            prompt.index("**العربية"),
        )

    def test_general_prompt_demands_brevity(self):
        self.assertIn("لا تُطل", ai.GENERAL_ASSISTANT_PROMPT)

    def test_two_modes_are_separate_entry_points(self):
        import inspect

        # Platform search reads the catalog; chat does not.
        search_src = inspect.getsource(ai.generate_platform_search_result)
        chat_src = inspect.getsource(ai.generate_ai_chat_result)
        self.assertIn("_platform_search_results", search_src)
        self.assertNotIn("_search_medbot", chat_src)
        self.assertNotIn("build_platform_catalog", chat_src)

    def test_catalog_verification_iterates_real_rows(self):
        import inspect

        source = inspect.getsource(ai._verify_catalog_answer)
        # It iterates the real catalog and never synthesizes an entity.
        self.assertIn("_catalog_rows_as_results", source)


# ---------------------------------------------------------------------------
# Telegram delivery boundary (real handlers, recording bot)
# ---------------------------------------------------------------------------

class TelegramModeTests(ModeBase):
    student_id = 9100

    def _send(self, mode, text):
        from test_medbot_fixes import _RecordingBot
        from test_medbot_router import _FakeContext, _MediaUpdate, _TextMessage

        async def run():
            ctx = _FakeContext()
            ctx.user_data["assistant_mode"] = mode
            ctx.bot = _RecordingBot()
            msg = _TextMessage(text)
            await main.ai_handler(_MediaUpdate(self.student_id, msg), ctx)
            return msg

        msg = asyncio.run(run())
        return "\n".join(msg.replies), _callbacks(msg.last_markup)

    def test_platform_mode_returns_verified_buttons(self):
        self.install_provider()
        delivered, callbacks = self._send(
            main.MODE_PLATFORM, "وين الفسيولوجي؟"
        )

        self.assertIn("PHYSIOLOGY", delivered)
        self.assertIn(f"folder:{self.physio}", callbacks)
        self.assertIn("home", callbacks)

    def test_chat_mode_medical_does_not_expose_registry(self):
        calls = self.install_provider()
        delivered, callbacks = self._send(
            main.MODE_CHAT, "اشرح لي دورة القلب"
        )

        self.assertEqual(calls["systems"][-1], ai.UNIFIED_ASSISTANT_PROMPT)
        for platform_fact in ("Second Year", "PHYSIOLOGY", "HISTOLOGY"):
            self.assertNotIn(platform_fact, delivered)
        # No direct-access buttons, only Home.
        self.assertEqual(callbacks, {"home"})

    def test_chat_mode_general_is_concise_general(self):
        calls = self.install_provider()
        _delivered, callbacks = self._send(
            main.MODE_CHAT, "ما هي عاصمة فرنسا؟"
        )

        self.assertEqual(calls["systems"][-1], ai.GENERAL_ASSISTANT_PROMPT)
        self.assertEqual(callbacks, {"home"})

    def test_platform_mode_never_invents_through_handler(self):
        self.install_provider(answer="ستجده في First Year ثم Semester 1.")
        delivered, callbacks = self._send(
            main.MODE_PLATFORM, "هل يوجد First Year؟"
        )

        self.assertNotIn("First Year", delivered)
        self.assertEqual(callbacks, {"home"})


def _callbacks(markup):
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
