# MEDBOT — RESUME AFTER REBOOT

You are resuming an interrupted autonomous MEDBOT upgrade.

IMPORTANT:
The previous agent already completed and persisted a substantial migration.
DO NOT restart the project from scratch.
DO NOT wipe, recreate, reset, or replace the SQLite database.
DO NOT repeat completed migrations unnecessarily.
DO NOT destroy working code.

Current verified state:
- main.py exists and currently compiles.
- database.py exists and currently compiles.
- search_engine.py exists and currently compiles.
- ai_router.py exists and currently compiles.
- ai_discovery.py exists and currently compiles.
- medbot_v2.sqlite3 integrity_check = ok.
- folders = 27
- content = 2
- contributions = 4
- about_us = 1
- ai_registry = 0
- settings = 0
- contribution eligibility metadata was already added.
- content source metadata was already added.
- Admin/content-management work was already implemented.
- correct Telegram file-type delivery was already implemented.
- About Us was already initialized.
- logging infrastructure was already created.
- previous AI work created ai_router.py and ai_discovery.py but AI integration/verification is NOT YET COMPLETE.

The previous run stopped immediately after creating:
- ai_router.py
- ai_discovery.py

YOUR TASK:
Continue from the current filesystem/database state and finish the MEDBOT upgrade.

First AUDIT the current implementation. Read the actual files before modifying them.

Then continue in this order:

1. Verify database migration and schema.
2. Verify actual main.py integration of:
   - security fix
   - contribution eligibility
   - contribution approval/publication
   - content open/rename/move/delete
   - folder add/rename/move/delete
   - About Us
   - logging
3. Verify all Telegram file types:
   document, photo, video, audio, voice.
4. Verify contribution lifecycle:
   pending -> approved/rejected
   double approval/rejection must be blocked.
5. Verify exact target publication:
   approved content must appear in the exact resource folder selected by the contributor.
6. Verify Admin authorization on every sensitive callback/action.
7. Verify first-user security:
   ADMIN_ID=0 must NEVER promote the first Telegram user to admin.
8. Verify folder contribution eligibility is metadata-driven, not based on folder names.
9. Verify content management for both direct uploads and approved contributions.
10. Verify move/rename/delete safety.
11. Verify About Us is editable/config-driven.
   Known factual value:
   founding developer = D.Umair_HQ
   Do NOT invent doctor names, credentials, specialties, institutions, biographies, or medical claims.
12. Finish AI architecture:
   MEDBOT
   -> intent
   -> deterministic SQLite search
   -> relevant folders/content only
   -> AI Router
   -> Provider Adapter
   -> provider
   -> grounding validator
   -> Telegram response.
13. AI MUST NOT be a generic medical chatbot.
   It is for:
   - introducing MEDBOT
   - understanding MEDBOT-related requests
   - finding resources already registered in MEDBOT
   - explaining how to use MEDBOT
   - explaining content only when grounded in registered MEDBOT content.
14. AI MUST NEVER invent a file, folder, path, lecture, link, or resource.
   If absent, use:
   "The requested resource is not currently registered in MEDBOT."
15. Do NOT send the entire MEDBOT tree to an AI model on every request.
   SQLite deterministic search is the source of truth.
16. Implement/finish provider discovery.
   Discover actual usable providers/models from:
   - Google/Gemini
   - OpenRouter
   - OpenAI-compatible endpoints
   - Anthropic/Groq if actually configured
   - local/Ollama if actually available
   - OpenCode-supported providers/models
   - actual environment/config/scripts/endpoints.
   Never invent credentials.
   NEVER inspect ~/.local/share/opencode/auth.json.
17. Persistent ai_registry must contain actual provider/model health data without secrets.
18. Benchmark actual discovered providers safely.
   Record:
   availability
   auth status
   latency
   success/failure
   timeout
   rate-limit
   upstream error
   capabilities
   timestamps
19. Do NOT call any model "best".
   Rank operationally only using current availability, reliability, latency, task suitability, rate limits, timeout history, and recent health.
20. Use actual currently verified model information where available.
   Previously verified OpenCode model:
   openrouter/deepseek/deepseek-v4-flash
   Previous real test succeeded in ~22 seconds.
   This is NOT a permanent "best model" designation.
21. Previous observed providers/models included:
   Google Gemini direct API: currently rate-limited.
   OpenRouter:
   - deepseek-v4-flash previously succeeded
   - nemotron-3-super previously succeeded in an earlier benchmark but later OpenCode direct test timed out
   - nex-n2.5-mini previously succeeded in an earlier benchmark but later OpenCode direct test timed out
   - other models had timeouts/upstream errors.
   Treat these as historical evidence only and re-test current status before registering as healthy.
22. Implement router failover:
   transient timeout/429/5xx -> bounded retry/fallback
   permanent auth/config/model errors -> do not endlessly retry.
23. AI logs must never expose API keys or secrets.
24. Complete deterministic search + grounding validation.
25. Run comprehensive QA.
26. If a test fails:
   diagnose actual cause -> patch -> retest.
   Never fabricate success.
27. Preserve backups and create additional backups before risky changes.
28. Do not kill unrelated processes.
29. If MEDBOT is running, stop only MEDBOT safely before DB/schema changes.
30. Keep IPv4 workaround in main.py; do not remove it.
31. Keep the project runnable from Termux with its existing venv.
32. Preserve run_bot.sh and runtime structure.
33. Do not introduce unnecessary dependencies.
34. Make changes incrementally and idempotently.
35. Document all significant decisions, migrations, bugs, fixes, warnings, and QA results.

CRITICAL:
The existing database is valuable and must be preserved.
Never use DROP DATABASE, delete medbot_v2.sqlite3, recreate the database, or replace it with an empty database.

At the end produce a factual report with:
IMPLEMENTED
TESTED
BACKED_UP
MIGRATIONS
AI_PROVIDERS_DISCOVERED
AI_BENCHMARKS
REMAINING
IMPORTANT_WARNINGS

Only report tests that actually ran successfully.
