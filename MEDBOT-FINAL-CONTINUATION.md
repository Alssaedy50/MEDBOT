# MEDBOT — FINAL CONTINUATION / RELEASE CANDIDATE

You are the final autonomous senior engineer for the existing MEDBOT project.

IMPORTANT:
- DO NOT rebuild MEDBOT.
- DO NOT wipe, recreate, reset, or replace medbot_v2.sqlite3.
- DO NOT destroy working code.
- DO NOT repeat successful migrations unnecessarily.
- DO NOT inspect ~/.local/share/opencode/auth.json.
- NEVER expose, print, copy, or persist API secrets.
- NEVER invent providers, models, credentials, file_ids, files, folders, doctors, qualifications, or content.
- The human operator only executes Termux commands.
- Continue from the CURRENT filesystem and database state.
- Make engineering decisions yourself.
- Work incrementally.
- Backup before risky changes.
- If something is already correct, verify it and leave it alone.

============================================================
CURRENT VERIFIED STATE
============================================================

Project:
~/MEDBOT

Python:
3.14.x

Current source:
main.py
database.py
search_engine.py
ai_router.py
ai_discovery.py

All five currently compile.

Database:
medbot_v2.sqlite3

Current database counts:
users = 2
folders = 27
content = 2
contributions = 4
about_us = 1
ai_registry = 4
settings = 0

SQLite integrity_check:
ok

database.py get_db():
PRAGMA foreign_keys = ON

Current folder/content metadata exists:
folders.accepts_contributions
content.source_type
content.source_contribution_id
content.created_by

Admin security:
ADMIN_ID=0 must NOT promote the first user.
Current main.py uses:
ADMIN_ID != 0 and user_id == ADMIN_ID

Current approved contributions:
1 -> folder 21 -> approved -> NO corresponding content row
3 -> folder 11 -> approved -> content exists
4 -> folder 27 -> approved -> content exists

Contribution 2:
folder 32 -> pending

IMPORTANT LEGACY INCONSISTENCY:
Contribution #1 is a real approved contribution with a real Telegram file_id.
It has no corresponding content row.
This is a real legacy inconsistency.
Repair it safely and idempotently ONLY if the evidence in the database/backups confirms the file_id.
Do NOT invent a file_id.
Do NOT delete the contribution.
Do NOT change approved -> rejected.
Do NOT create duplicate content if a matching content row already exists.
The repaired content MUST remain in exact folder 21.

============================================================
PHASE 1 — AUDIT
============================================================

Read the actual current files before modifying anything.

Verify:
- main.py
- database.py
- search_engine.py
- ai_router.py
- ai_discovery.py

Verify actual Telegram handlers and callbacks.

Verify:
- admin authorization
- first-user security
- contribution eligibility
- contribution approval/rejection
- idempotency
- exact target publication
- file delivery by file type
- content rename/move/delete
- folder add/rename/move/delete
- cycle prevention
- invalid ID handling
- About Us
- logging
- deterministic search

Do not assume previous logs are sufficient.
Use current source/database state.

============================================================
PHASE 2 — DATABASE / LEGACY REPAIR
============================================================

Before risky changes create a fresh backup under:
backups/

Never overwrite the original database backup.

Verify:
PRAGMA integrity_check
PRAGMA foreign_keys through database.py get_db()

Inspect schema.

Repair Contribution #1 safely and idempotently:
approved contribution -> exact folder 21 -> content.

Use:
source_type = contribution
source_contribution_id = 1
created_by = contribution.user_id

If a matching content row already exists, do NOT insert another.

Verify:
- contribution 1 remains approved
- content exists exactly once
- folder_id = 21
- title matches contribution
- file_id matches contribution
- file_type matches contribution
- search can find it
- navigation can open it

Verify contributions 3 and 4 have correct source metadata.
If metadata is missing, repair only when the relationship is unambiguous from exact matching evidence.
Do not guess.

Contribution 2 remains pending unless an existing workflow/test explicitly changes it.

============================================================
PHASE 3 — ADMIN / CONTENT QA
============================================================

Verify all admin actions require:
is_admin(user_id)

Verify every sensitive callback checks authorization.

Test logically:
- unauthorized user cannot approve
- unauthorized user cannot reject
- unauthorized user cannot rename
- unauthorized user cannot move
- unauthorized user cannot delete
- unauthorized user cannot add folder
- unauthorized user cannot move folder
- invalid IDs fail safely

Verify first user cannot become admin when ADMIN_ID=0.

Verify content:
- open
- rename
- move
- delete

Verify file types:
document
photo
video
audio
voice

Do not need real Telegram uploads for every type if that is impossible locally.
At minimum verify dispatch logic statically and run all available safe tests.

Verify folder:
- add
- rename
- move
- delete
- cycle prevention
- child/content safety

============================================================
PHASE 4 — ABOUT US
============================================================

About Us must be editable/config-driven.

Known verified factual value:
founding developer = D.Umair_HQ

Do NOT invent:
- doctor names
- medical credentials
- specialties
- institutions
- biographies
- reviewer claims

Keep About Us concise and factual.

============================================================
PHASE 5 — AI ARCHITECTURE
============================================================

Required architecture:

Student
 ↓
MEDBOT
 ↓
intent / request understanding
 ↓
deterministic SQLite search
 ↓
relevant folders/content only
 ↓
AI Router
 ↓
Provider Adapter
 ↓
actual AI provider
 ↓
Grounding Validator
 ↓
Telegram response + buttons

CRITICAL:
AI is NOT a generic medical chatbot.

AI is for:
- introducing MEDBOT
- explaining how MEDBOT works
- understanding MEDBOT navigation/search requests
- finding resources already registered in MEDBOT
- explaining registered MEDBOT content when grounded in that content
- routing users to exact existing resources

AI MUST NOT invent:
- files
- folders
- paths
- lectures
- links
- resources
- content records

When a requested resource is absent, use exactly:

"The requested resource is not currently registered in MEDBOT."

Do NOT send the entire MEDBOT tree to the model on every request.

SQLite remains source of truth.

============================================================
PHASE 6 — AI PROVIDER DISCOVERY
============================================================

Discover only ACTUAL provider sources.

Check:
- GEMINI_API_KEY
- OPENROUTER_API_KEY
- OPENAI_API_KEY
- other OpenAI-compatible environment configuration
- actual scripts/configuration
- installed SDKs
- OpenCode supported provider/model information using supported CLI/config commands

DO NOT inspect:
~/.local/share/opencode/auth.json

Do NOT invent credentials.

Environment variables currently known:
GEMINI_API_KEY
OPENROUTER_API_KEY
OPENAI_API_KEY

Historical OpenCode evidence:
Google credential exists.
OpenRouter credential exists.

Historical real model:
openrouter/deepseek/deepseek-v4-flash

It previously succeeded.
This is historical evidence only.
Health-test again before marking healthy.

Do not call any model "best".

============================================================
PHASE 7 — AI REGISTRY
============================================================

Persistent registry must track actual tested state.

Required information:
provider
model
endpoint/type
availability
auth_status
latency
success_rate
capabilities
last_success
last_failure
last_test
error_category
timeout_behavior
rate_limit_behavior
notes

No secrets.

Avoid duplicate registry rows for the same provider/model unless there is a justified historical record.

Registry must distinguish:
AVAILABLE
VERIFIED
RATE_LIMITED
TIMEOUT
AUTH_FAILED
UPSTREAM_ERROR
UNAVAILABLE

A provider being listed by OpenCode does NOT prove its credentials are usable.

============================================================
PHASE 8 — AI BENCHMARK
============================================================

For each actually discoverable provider/model that can be safely tested:

Test:
A. availability
B. basic response
C. latency
D. reliability
E. MEDBOT navigation task
F. grounding task
G. formatting
H. error classification where applicable

Do not waste quota repeating known failures unnecessarily.

Record actual results.

Operational ranking must consider:
- current availability
- success/reliability
- latency
- task suitability
- rate limits
- timeout history
- current health

Do NOT rank as "best".
Use operational scores internally only.

============================================================
PHASE 9 — ROUTER / FAILOVER
============================================================

Router must:
- choose currently healthy provider
- prefer verified operational providers
- use task suitability
- avoid unhealthy providers
- use bounded retries
- fallback on transient timeout/429/5xx
- not endlessly retry permanent auth/config/model errors
- update registry after failures
- log provider/model/latency/result/error category
- never log secrets

Students normally do not choose models.

Admin diagnostics may display:
provider
model
availability
latency
success/failure
error category
last test

============================================================
PHASE 10 — GROUNDING
============================================================

Test at minimum:

1. Existing registered resource request.
2. Existing folder request.
3. Existing content request.
4. Non-existent resource request.
5. Hallucinated path.
6. Hallucinated filename.
7. MEDBOT meta question.

The AI must refuse fabricated resources.

Verify:
"The requested resource is not currently registered in MEDBOT."

is returned for absent resources.

============================================================
PHASE 11 — QA
============================================================

Run comprehensive QA.

Required:

python compilation
database integrity
foreign key application
schema
tree
breadcrumbs
navigation
search
Arabic search
content retrieval
content delivery dispatch
contribution lifecycle
double approval blocked
double rejection blocked
exact target publication
search after publication
admin authorization
first-user security
folder safety
content safety
About Us
AI discovery
AI registry
AI health
AI benchmark
AI routing
AI failover
AI grounding
Telegram API integration where possible
IPv4 workaround preservation
run_bot.sh preservation
runtime structure

If a test fails:
diagnose actual cause
patch
retest

Never fabricate success.

============================================================
PHASE 12 — DOCUMENTATION
============================================================

Document:
- architecture
- database schema/migrations
- contribution lifecycle
- admin controls
- security
- AI architecture
- provider discovery
- benchmark results
- routing/failover
- grounding
- bugs/fixes
- legacy repair
- QA
- remaining warnings

Do not claim tests that did not run.

============================================================
PHASE 13 — FINAL REPORT
============================================================

Before exiting, PRINT and save a final report.

Required headings exactly:

IMPLEMENTED
TESTED
BACKED_UP
MIGRATIONS
AI_PROVIDERS_DISCOVERED
AI_BENCHMARKS
CONTRIBUTION_REPAIR
ROUTING
FAILOVER
GROUNDING
QA
REMAINING
IMPORTANT_WARNINGS
FINAL_STATUS

Final status must be one of:

FINAL_STATUS=COMPLETE
FINAL_STATUS=COMPLETE_WITH_WARNINGS
FINAL_STATUS=BLOCKED

Use COMPLETE only if all required acceptance criteria actually pass.

Use COMPLETE_WITH_WARNINGS only if MEDBOT is runnable and remaining issues are non-blocking.

Use BLOCKED if a required feature genuinely cannot be completed.

Never claim COMPLETE merely because code compiles.

============================================================
EXIT CONDITION
============================================================

Do not stop immediately after a partial success.

Do not stop after saying "All QA passes".

You MUST:
1. finish documentation
2. print final report
3. save final report under logs/
4. verify final state one last time
5. print FINAL_STATUS

Only then exit.

============================================================
SAFETY
============================================================

Never:
- delete medbot_v2.sqlite3
- recreate database
- wipe rows
- inspect auth.json
- expose secrets
- invent data
- kill unrelated processes
- install unnecessary dependencies
- replace working architecture

Preserve:
IPv4 workaround
venv
run_bot.sh
existing successful search
existing contribution state machine
existing admin security
existing working handlers

END OF TASK
