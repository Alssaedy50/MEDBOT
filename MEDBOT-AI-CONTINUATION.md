# MEDBOT — AI / DATA CONSISTENCY CONTINUATION

You are the autonomous senior engineer continuing an existing MEDBOT project.

IMPORTANT:
Do NOT rebuild MEDBOT.
Do NOT wipe/recreate the database.
Do NOT replace working components unnecessarily.
Do NOT inspect ~/.local/share/opencode/auth.json.
Do NOT expose, print, copy, or persist any API secret.
Do NOT invent providers, models, files, file_ids, doctors, credentials, or content.

The human operator only executes Termux commands.
You are responsible for engineering decisions.

==================================================
CURRENT VERIFIED STATE
==================================================

Project:
~/MEDBOT

Current source:
main.py
database.py
search_engine.py
ai_router.py
ai_discovery.py

All currently compile.

Current DB:
medbot_v2.sqlite3

Current DB integrity:
PRAGMA integrity_check = ok

Current counts:
users = 2
folders = 27
content = 2
contributions = 4
about_us = 1
ai_registry = 2
settings = 0

Current AI registry:
google_gemini / gemini-flash-lite-latest
google_gemini / gemma-4-26b-a4b-it

The registry currently contains only Gemini because ai_discovery.py currently calls:
AIRouter(gemini_key)
instead of discovering all actual available provider sources.

Environment variable names currently found:
GEMINI_API_KEY

OpenCode:
version 1.17.9

OpenCode credential sources are known to include:
Google
OpenRouter

NEVER inspect ~/.local/share/opencode/auth.json.

Historical verified OpenCode model:
openrouter/deepseek/deepseek-v4-flash
It previously completed a real test successfully, but MUST be health-tested again before use.
Historical other models are NOT assumed available now.

Historical direct Google API:
Gemini previously returned HTTP 429 quota/rate limit.
Do not assume current availability merely from historical results.
Current discovery recently reported:
gemini-flash-lite-latest AVAILABLE ~2369 ms
gemma-4-26b-a4b-it AVAILABLE ~4103 ms

==================================================
KNOWN DATA INCONSISTENCY
==================================================

Contribution:
ID 1
user_id 5687336378
folder_id 21
title = صورة توضيحية
file_type = photo
status = approved

But there is NO corresponding content row.

Current content:
ID 2 -> folder 27 -> صورة توضيحية -> photo
ID 3 -> folder 11 -> صورة توضيحية -> photo

Current approved contributions without content:
Contribution 1 only.

IMPORTANT:
Investigate before modifying.

Possible sources:
- current database schema
- contribution file_id and metadata
- git history if available
- backups
- application logs
- previous autonomous logs

Do NOT:
- invent a file_id
- create fake content
- delete the contribution merely to make the check pass
- change approved to rejected without evidence
- silently erase history

If the original file_id still exists and the contribution was genuinely approved, repair it safely and idempotently.
If evidence is insufficient, preserve the record and document it as a legacy unresolved inconsistency.
If there is a deterministic safe repair supported by existing data, perform it and verify exact folder_id=21.

==================================================
TASK 1 — AUDIT CURRENT AI IMPLEMENTATION
==================================================

Inspect actual:
ai_router.py
ai_discovery.py
main.py
database.py

Do not trust stale TODO comments.

Determine whether these actually work:

1. GeminiProvider
2. OpenRouterProvider
3. OpenAIProvider
4. AIFactory
5. GroundingValidator
6. AIRouter
7. generate_with_failover
8. ai_generate_grounded
9. main.py integration

Compile first.

==================================================
TASK 2 — EXPAND REAL PROVIDER DISCOVERY
==================================================

Discovery must find actual usable provider sources, not theoretical classes.

Inspect safely:

- environment variable NAMES only
- installed Python packages
- actual HTTP/API-compatible configuration files that are safe to inspect
- project scripts/configs
- OpenCode supported CLI/provider information
- local model executables if actually present
- Ollama only if actually installed/running
- OpenAI-compatible endpoints only if actually configured
- Google/Gemini
- OpenRouter
- Anthropic
- Groq
- other real providers discovered in the environment

Never print secrets.

Do NOT claim a provider exists merely because its class exists in ai_router.py.

Do NOT claim a provider is usable merely because OpenCode lists a credential.

For OpenCode:
use supported CLI commands such as:
opencode auth list
opencode models
opencode run ...
only where appropriate.

Never inspect auth.json.

==================================================
TASK 3 — AI REGISTRY SCHEMA
==================================================

Current schema:

id
provider
model
endpoint
availability
auth_status
latency_ms
success_rate
capabilities
last_success
last_failure
last_test
notes

Upgrade only if necessary, backward-compatibly.

Registry should be capable of recording:

provider
model
endpoint
type
availability
auth_status
latency_ms
success_rate
timeout behavior
rate-limit behavior
error category/status
capabilities
last_success
last_failure
last_test
notes

Use ALTER TABLE only where needed.
Never recreate the table.
Never lose existing rows.

Use migration tracking or an idempotent migration guard.

No secrets.

==================================================
TASK 4 — PROVIDER TESTING
==================================================

For each ACTUALLY discovered provider/model:

Test safely:

A. availability
B. authentication status
C. basic response
D. latency
E. timeout behavior
F. rate-limit behavior where naturally encountered
G. simple MEDBOT navigation task
H. grounding/navigation response behavior

Use bounded timeouts.

Classify:

AVAILABLE
VERIFIED
RATE_LIMITED
TIMEOUT
AUTH_FAILED
UPSTREAM_ERROR
UNAVAILABLE

Do not waste quota by repeatedly testing known permanent failures.

Historical benchmark results are evidence only, NOT current truth.

==================================================
TASK 5 — AI ROUTER
==================================================

The architecture must remain:

MEDBOT
↓
Intent/Search
↓
deterministic SQLite search
↓
relevant folders/content only
↓
AI Router
↓
Provider Adapter
↓
AI Provider
↓
Grounding Validator
↓
Telegram response

The AI must NOT receive the complete MEDBOT tree on every message.

The database remains source of truth.

Router ranking should consider:

- current availability
- successful verification
- latency
- recent reliability
- task suitability
- timeout history
- rate limits
- current health
- recent failures

Do NOT use "smartest model" as the only ranking factor.

Students normally do not select the model.

Admin/diagnostics may inspect provider/model/health.

==================================================
TASK 6 — FAILOVER
==================================================

Implement/verify bounded failover.

Transient:
- timeout
- 429
- 500
- 502
- 503
- temporary upstream/network errors

→ bounded retry/fallback.

Permanent:
- invalid API key
- authentication failure
- unavailable model
- invalid endpoint

→ do not endlessly retry.

Log:
provider
model
latency
success/failure
error category
fallback

Never log:
API keys
tokens
authorization headers
secrets
credential-containing prompts

==================================================
TASK 7 — GROUNDING
==================================================

AI is NOT a generic medical chatbot.

MEDBOT AI purpose:

- introduce MEDBOT
- understand what the user wants within MEDBOT
- search registered MEDBOT content
- locate files/folders
- explain how to use MEDBOT
- explain content only when grounded in registered MEDBOT content
- navigate users to actual resources

It must NOT invent:

files
folders
paths
links
lectures
resources
database records

If requested resource is absent, use:

"The requested resource is not currently registered in MEDBOT."

Use actual DB search results.

Preferred UX:

FIND → UNDERSTAND → NAVIGATE

For resource navigation:
short response
actual title
actual path
Open button where supported

Do not produce long generic medical essays when the user is asking where a resource is.

==================================================
TASK 8 — MAIN.PY INTEGRATION
==================================================

Verify actual Telegram path:

user asks AI
→ deterministic search/context
→ ai router
→ grounded generation
→ validator
→ Telegram response

Do not retain an obsolete parallel Gemini-only path.

Do not break existing search/navigation.

==================================================
TASK 9 — CONTRIBUTION CONSISTENCY
==================================================

Contribution approval must remain idempotent.

Approved contribution must publish into EXACT submitted folder.

Never move it to parent block/subject.

Contribution buttons must be controlled by folder metadata, not folder-name guessing.

Terminal resource sections may accept contributions.

Container sections should not.

Preserve contribution history.

==================================================
TASK 10 — COMPREHENSIVE QA
==================================================

Run:

- Python compile
- DB integrity
- foreign keys using the application's actual DB connection
- schema migration checks
- deterministic search
- Arabic search
- folder navigation
- empty folders
- content retrieval
- photo delivery
- video delivery
- audio delivery
- voice delivery
- document delivery
- contribution eligibility
- contribution lifecycle
- double approval protection
- double rejection protection
- exact target folder
- admin authorization
- first-user cannot become admin
- invalid callback IDs
- folder add/rename/move/delete safety
- content rename/move/delete
- AI discovery
- AI registry
- provider health
- benchmark
- router selection
- failover
- grounding/no hallucinated resource
- Telegram integration if safe to test
- runtime compatibility

If a test fails:
inspect cause
patch minimally
rerun the failed test
then rerun relevant regression tests.

Never claim success without actual evidence.

==================================================
TASK 11 — DOCUMENTATION
==================================================

Update project documentation/logs with:

- decisions
- schema changes
- migrations
- bugs
- fixes
- contribution inconsistency
- AI providers discovered
- tests
- benchmark results
- known external limitations
- unresolved warnings

Do not overwrite useful historical documentation.

==================================================
TASK 12 — BACKUPS
==================================================

Before modifying:

main.py
database.py
search_engine.py
ai_router.py
ai_discovery.py
medbot_v2.sqlite3

create timestamped backups.

Do not create unnecessary duplicate backups during every tiny operation.

==================================================
FINAL REPORT
==================================================

At the end print a concise final report containing:

IMPLEMENTED
TESTED
BACKED_UP
MIGRATIONS
AI_PROVIDERS_DISCOVERED
AI_BENCHMARKS
ROUTING
FAILOVER
GROUNDING
CONTRIBUTION_REPAIR
QA
REMAINING
IMPORTANT_WARNINGS

Use actual evidence.

Never include secrets.

==================================================
STOP CONDITIONS
==================================================

Do not stop merely because one provider is unavailable.

Continue using other actually available providers.

Do not stop because historical OpenCode models changed.

Do not rebuild working MEDBOT features.

Do not wipe the database.

Do not invent missing credentials.

Do not inspect OpenCode auth.json.

When complete, leave the project in a runnable state.
