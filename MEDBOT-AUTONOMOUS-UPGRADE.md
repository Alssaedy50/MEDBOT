# MEDBOT — AUTONOMOUS MASTER UPGRADE SPECIFICATION
# Version: 2026-09-16
#
# ROLE
# You are the autonomous senior engineering team for MEDBOT.
# Act as:
# Product Owner
# Software Architect
# Python Engineer
# Telegram Engineer
# Database Engineer
# AI Architect
# Security Engineer
# QA Engineer
# DevOps/Termux Engineer
#
# OBJECTIVE
# Complete the MEDBOT upgrade without rebuilding the project from scratch.
# Preserve all working functionality and implement all agreed requirements.
#
# OPERATING PRINCIPLE
# INSPECT → BACKUP → DECIDE → IMPLEMENT → TEST → FIX → RETEST → DOCUMENT
#
# Never claim something passed unless it was actually tested.

============================================================
1. PROJECT IDENTITY
============================================================

MEDBOT is a Telegram medical education LMS.

Core:
- Study Library
- Search
- MEDBOT Assistant
- Student Contributions
- Admin Panel
- Runtime/System Monitoring
- AI Provider Discovery
- AI Router
- AI Diagnostics

The system is initially intended for Human Medicine Year 2.

The bot is NOT a generic medical chatbot.

AI must NOT become a free-form arbitrary medical-answering bot.

AI's primary purposes:
- explain MEDBOT
- introduce MEDBOT
- understand what the student wants inside MEDBOT
- find files/resources/information already registered in MEDBOT
- accelerate navigation
- search MEDBOT's existing knowledge/data
- explain grounded MEDBOT content when appropriate
- route users to exact files/folders/resources
- explain how to use MEDBOT
- use newly approved/published MEDBOT content
- never invent resources

SQLite/database is the source of truth.

AI is a layer above deterministic MEDBOT search/data.

============================================================
2. ABSOLUTE PRESERVATION RULES
============================================================

DO NOT:
- rebuild from scratch
- wipe the database
- delete existing content
- delete working tools
- unnecessarily reinstall dependencies
- remove the tested Search Engine
- remove IPv4 socket workaround
- remove contribution state-machine protections
- expose API keys
- print secrets
- inspect/copy OpenCode auth.json
- invent medical facts
- invent doctor/team facts
- invent files/folders/resources
- hardcode resource folder names
- force every AI request through one model
- declare a model "best" without current evidence
- replace SQLite with AI
- destroy existing project architecture merely to simplify it

Existing working components have priority.

============================================================
3. CURRENT IMPORTANT FACTS
============================================================

Real project:
$HOME/MEDBOT

Known files include:
main.py
database.py
search_engine.py
run_bot.sh
medbot.sqlite3
medbot_v2.sqlite3
venv/

Existing tested components include:
- Telegram bot
- SQLite
- Search Engine
- Arabic normalization
- Search ranking
- contribution state machine
- IPv4 workaround
- real Telegram search flow
- real contribution flow
- OpenRouter benchmark work
- Google Gemini direct HTTP testing

Preserve them.

============================================================
4. EXISTING SEARCH ENGINE
============================================================

Search Engine has already been tested.

It supports:
- Unicode NFKC normalization
- Arabic tashkeel removal
- Arabic normalization
- exact matching
- prefix matching
- partial matching
- folder search
- content search
- content count
- empty-folder detection
- NOT_FOUND
- Arabic search
- deterministic ordering

Do not replace it.

Integrate new features around it.

Regression tests must continue to pass.

============================================================
5. NETWORK / IPV4
============================================================

The following IPv4 workaround is important because Android/Termux IPv6 caused network failures:

_orig_getaddrinfo = socket.getaddrinfo

def getaddrinfo_ipv4(*args, **kwargs):
    responses = _orig_getaddrinfo(*args, **kwargs)
    return [r for r in responses if r[0] == socket.AF_INET]

socket.getaddrinfo = getaddrinfo_ipv4

Do not remove it unless an equivalent tested solution is proven.

============================================================
6. ACADEMIC LIBRARY ARCHITECTURE
============================================================

The intended conceptual hierarchy is:

Academic Year
├── First Year
└── Second Year
    ├── Term 1
    │   ├── Blocks
    │   └── Longitudinal Subjects
    └── Term 2
        ├── Blocks
        └── Longitudinal Subjects

Blocks contain subjects.

Subject contains:
- Theoretical
- Practical

Resource/terminal sections can contain:
- Books & References
- Model Exams / Banks
- Video Lectures
- Audio Lectures
- Transcriptions & Reviews
- Interactive Practice Tests
- Summaries
- Other educational resources

This is a conceptual architecture.

DO NOT hardcode folder names.

Use semantic metadata/type information.

Existing folder data must remain valid.

============================================================
7. RESOURCE / TERMINAL FOLDER LOGIC
============================================================

A Contribution button must NOT appear on arbitrary container folders.

Examples:
- Academic Year → no contribution
- Term → normally no contribution
- Blocks → no contribution
- Subject → normally no contribution
- Theoretical → normally no contribution
- Practical → normally no contribution

Terminal/resource sections can allow contributions.

Examples:
- Books
- References
- Video Lectures
- Audio Lectures
- Summaries
- Banks
- Models
- Practice Tests
- Other explicitly configured resource sections

Determine this from metadata/semantic node type.

DO NOT determine it by comparing folder names.

If current schema needs extension:
- use backward-compatible migration
- preserve existing data
- document migration
- test migration

Possible safe metadata:
is_resource
resource_type
accepts_contributions

Use the minimum schema change necessary.

============================================================
8. CONTRIBUTIONS
============================================================

Student flow:

Student
→ enters eligible resource section
→ Contribution
→ selects/sends file
→ title
→ confirmation
→ pending

Admin:
Pending Contributions
→ inspect
→ approve OR reject

Approval must:
- preserve exact target folder_id
- create content in that exact folder
- never move it to parent/block/subject automatically
- become immediately searchable
- become immediately visible
- become AI-discoverable through DB/search
- be idempotent

Never publish the same contribution twice.

State machine:
pending → approved
pending → rejected

Invalid:
approved → approved
approved → rejected
rejected → rejected
rejected → approved

Use transaction safety.

Existing state-machine protections must remain.

============================================================
9. HISTORICAL CONTRIBUTION INCONSISTENCY
============================================================

Current real DB contains:
- published photo content in folder 27
- published photo content in folder 11

There is also a historical approved contribution that may not have a matching content row.

DO NOT blindly:
- delete it
- recreate it
- change it

Investigate.

Determine:
- contribution ID
- status
- target folder
- whether matching content exists
- whether duplicate content exists
- timestamps if available

Document the inconsistency and the evidence.

If safe reconciliation is possible without risking duplicate publication:
implement it.

Otherwise preserve the data and document it as a known legacy inconsistency.

============================================================
10. CONTENT MANAGEMENT
============================================================

Every published content item must support Admin operations:

- Open
- Rename
- Move
- Delete

This applies to:
- direct Admin uploads
- approved student contributions

Rename:
- changes title only

Move:
- updates folder_id
- valid destination folders only
- content disappears from old folder
- content appears in new folder
- no duplicate row

Delete:
- confirmation required
- removes content from active library
- contribution history must remain if source was a contribution

If needed, add backward-compatible content metadata:

source_type
source_contribution_id
created_by
created_at

Do not break existing rows.

============================================================
11. FOLDER / TREE ADMIN CONTROL
============================================================

Admin must have broad control over:

- root folders
- academic years
- terms
- blocks
- subjects
- theoretical/practical branches
- resource folders

Admin operations:
- add
- rename
- move/reorganize
- delete
- inspect

Deletion must require confirmation.

Do not permit unsafe recursive deletion without explicit confirmation.

Prevent:
- cycles
- moving a folder into itself
- moving a folder into its descendant
- orphaned nodes
- invalid parent IDs

Root handling must remain safe.

============================================================
12. FILE DELIVERY
============================================================

Telegram file delivery must respect actual file type.

photo → reply_photo
video → reply_video
audio → reply_audio
voice → reply_voice
document → reply_document

Do not send every file as document.

Use DB file_type.

Test every supported type where test fixtures are available.

============================================================
13. SECURITY
============================================================

CRITICAL:

Do NOT allow the first Telegram user to become Admin merely because:
ADMIN_ID == 0

Never use:
if ADMIN_ID == 0:
    ADMIN_ID = user_id

Admin identity must come from configured authorization.

If ADMIN_ID is missing:
- fail safely
- show configuration error
- do not promote arbitrary users

Every admin callback must re-check authorization.

Validate:
- callback IDs
- folder IDs
- content IDs
- contribution IDs

Never trust client callback data.

Prevent unauthorized:
- upload
- delete
- move
- rename
- approve
- reject
- admin settings

Never expose:
BOT_TOKEN
ADMIN_ID secrets where sensitive
GEMINI_API_KEY
OpenRouter keys
OpenCode credentials
.env contents
authorization files

============================================================
14. ABOUT US
============================================================

Create an editable/config-driven About Us section.

Known fact:

Founding Developer:
D.Umair_HQ

Do not invent:
- doctor names
- qualifications
- specialties
- titles
- institutional affiliations
- biographies

Allow Admin/configuration to add verified doctor/editor/publisher information later.

About Us should explain briefly:
- MEDBOT purpose
- educational focus
- publishing/review workflow
- founding developer
- responsible medical publishing/review team when configured

Keep content professional and concise.

============================================================
15. AI ARCHITECTURE
============================================================

Architecture:

MEDBOT
↓
Intent / Search
↓
Deterministic SQLite Search
↓
Relevant folders/content only
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

Do NOT send the entire folder tree to AI on every message.

AI receives only relevant context.

AI must not invent:
- files
- folders
- paths
- links
- lectures
- doctors
- resources
- database records

If something does not exist:
"The requested resource is not currently registered in MEDBOT."

============================================================
16. AI USER EXPERIENCE
============================================================

Target flow:

FIND → UNDERSTAND → NAVIGATE

Examples:

User:
"أين محاضرات الفسيولوجي؟"

AI should locate actual registered folders/files and return concise navigation.

Example format:

📚 محاضرات الفسيولوجي

📍 Second Year → Term 1 → Blocks → PHYSIOLOGY → نظري → محاضرات فيديو

[فتح القسم]

If multiple actual resources:
📄 File 1
📄 File 2

[Open]

Do not generate fake links.

Do not produce long medical essays when the user asked for navigation.

============================================================
17. AI PROVIDER DISCOVERY
============================================================

Do NOT assume only Gemini and OpenRouter.

Audit the environment for:

- OpenAI-compatible APIs
- Google/Gemini
- OpenRouter
- Anthropic
- Groq
- local models
- Ollama
- CLI models
- OpenCode providers
- environment variables
- project configuration
- scripts
- endpoints
- installed SDKs
- HTTP-compatible providers
- any other actually available provider

Do not inspect secret credential files such as:
~/.local/share/opencode/auth.json

Only determine existence/configuration/status without exposing secrets.

Provider discovery:

1. discover provider
2. discover models
3. discover credentials/config
4. verify authentication
5. test endpoint
6. test latency
7. test reliability
8. test MEDBOT task
9. record result

============================================================
18. AI REGISTRY
============================================================

Create a persistent AI registry if not already present.

Recommended fields:

provider
model
endpoint/type
availability
auth_status
latency_ms
success_rate
timeout_behavior
rate_limit_behavior
error_status
capabilities
last_success
last_failure
last_test
notes

Never store secrets.

Registry may be:
- SQLite
- JSON
- another safe local configuration

Prefer SQLite if architecture supports it cleanly.

============================================================
19. AI BENCHMARK
============================================================

Benchmark available providers/models.

Tests:

A. availability
B. basic response
C. latency
D. repeated reliability
E. MEDBOT navigation
F. grounding
G. formatting
H. failure behavior

Use safe lightweight prompts.

Do not waste quotas unnecessarily.

Do not claim "best".

Instead classify:
- AVAILABLE
- RATE_LIMITED
- TIMEOUT
- AUTH_FAILED
- UPSTREAM_ERROR
- UNAVAILABLE
- VERIFIED

Record actual evidence.

Previously observed examples are historical only:
- DeepSeek V4 Flash succeeded around 19s
- Nemotron 3 Super Free succeeded around 20s
- NEX N2.5 Mini Free succeeded around 19s
- Gemma tests timed out
- one Nemotron upstream failure occurred
- Google Gemini direct request was rate-limited

These are not permanent truths.
Re-test current status.

============================================================
20. AI ROUTER
============================================================

Router should consider:

- current availability
- recent success rate
- latency
- MEDBOT task suitability
- rate limits
- timeout history
- provider health
- recent failures

Do not select only by model prestige.

Use:
- primary
- secondary
- fallback
- recovery

For transient:
- timeout
- temporary network failure
- HTTP 429
- HTTP 5xx

Retry/fallback.

For permanent:
- invalid key
- invalid endpoint
- authentication failure
- unavailable model

Do not endlessly retry the same broken configuration.

Students normally do not manually choose models.

Admin diagnostics may display:
provider
model
status
latency
success rate
last failure
last success

============================================================
21. AI FAILOVER
============================================================

Implement safe failover.

Example:

Provider A
↓ timeout
Provider B
↓ rate limit
Provider C
↓ success

AI logs must record:
- selected provider
- selected model
- latency
- success/failure
- failure category
- fallback provider

Never log secrets or prompts containing sensitive credentials.

============================================================
22. DATABASE SAFETY
============================================================

Before schema changes:

1. backup
2. inspect schema
3. migration
4. verify
5. test
6. document

Use:
PRAGMA foreign_keys = ON

Never wipe medbot_v2.sqlite3.

Never recreate the database merely to simplify development.

Preserve existing rows.

Use backward-compatible ALTER TABLE migrations when possible.

============================================================
23. LOGGING
============================================================

Maintain:

logs/bot.log
logs/error.log
logs/actions.log
logs/ai.log

If already present, preserve.

Record:
- major decisions
- schema changes
- important bugs
- fixes
- QA results
- migrations
- AI provider benchmark/health
- important runtime events

Create/update a clear project change log.

Suggested:
PROJECT-DOCUMENTATION/
or
logs/CHANGELOG.md

Use timestamps.

============================================================
24. DOCUMENTATION
============================================================

Update project documentation after meaningful changes.

At minimum document:

- architecture changes
- DB schema changes
- migrations
- security fixes
- contribution behavior
- admin controls
- AI architecture
- provider discovery
- benchmark results
- QA results
- known warnings
- unresolved legacy data issues

Never claim a feature passed unless actually tested.

============================================================
25. RUNTIME
============================================================

Preserve current:
run_bot.sh
venv
Termux setup
tmux compatibility
termux-wake-lock

Do not unnecessarily reinstall packages.

If runtime crashes:
- diagnose
- fix
- compile
- retest
- restart

Do not kill unrelated Termux processes.

Do not kill unrelated OpenCode sessions.

============================================================
26. BOT SAFETY DURING MODIFICATION
============================================================

Before modifying main runtime code:

Detect whether MEDBOT bot is currently running.

If running:
- identify the MEDBOT process/session
- stop only the MEDBOT bot process safely
- do not kill unrelated processes
- perform changes
- test
- restart MEDBOT if safe

Do not modify database concurrently with active write operations if avoidable.

============================================================
27. QA REQUIREMENTS
============================================================

Run actual tests.

At minimum:

Python compile:
main.py
database.py
search_engine.py

Database:
- open
- foreign keys
- schema
- folder tree
- content
- contributions

Search:
- exact
- prefix
- partial
- Arabic
- NOT_FOUND
- empty folder
- content count

Navigation:
- home
- folder
- breadcrumbs
- back

Content:
- open
- photo
- video
- audio
- voice
- document

Contribution:
- eligible folder
- ineligible folder
- submit
- pending
- approve
- reject
- double approve
- double reject
- approved → reject blocked
- rejected → approve blocked
- exact target folder
- searchable after approval

Admin:
- authorization
- add
- rename
- move
- delete
- confirmation
- contribution approval/rejection
- file management

Security:
- first user cannot become admin
- unauthorized callbacks rejected
- invalid IDs rejected

AI:
- provider discovery
- registry
- health
- benchmark
- routing
- fallback
- grounding
- no hallucinated resources

Runtime:
- bot startup
- Telegram API connectivity
- IPv4 workaround
- tmux
- wake lock
- watchdog

============================================================
28. TESTING RULE
============================================================

If a test fails:

DO NOT simply report failure.

First:
1. inspect
2. identify cause
3. patch
4. retest

Repeat while the issue is safely fixable.

If an issue is external/permanent:
- preserve working system
- record exact failure
- continue other independent tasks

Never fabricate success.

============================================================
29. IDEMPOTENCY
============================================================

The entire upgrade must be safe to rerun.

Before creating:
- tables
- columns
- indexes
- config
- docs

check whether they already exist.

Do not duplicate:
- content
- migrations
- registry records
- settings
- handlers

============================================================
30. MODEL USAGE STRATEGY
============================================================

Use all actually available strong/free-capable models opportunistically for:
- discovery
- independent review
- implementation
- testing
- troubleshooting

Do NOT assume that "free" means currently available.

Do NOT waste quota by repeatedly testing known failed models.

Prefer:
- verified availability
- low latency
- reliability
- task suitability
- current limits

If OpenCode exposes usable models:
discover them through supported CLI/configuration.

If multiple models are available:
use different models for independent checks where practical.

Do not expose credentials.

============================================================
31. CURRENT PROJECT HISTORY
============================================================

Important existing decisions:
- Search Engine already tested
- contribution state machine already protected
- IPv4 workaround already required
- Telegram search already tested
- real DB has existing content
- current DB must not be wiped
- previous OpenCode attempt may fail if unsupported CLI flags are used

Use the actual installed OpenCode CLI.

Do not assume unsupported flags.

============================================================
32. AUTONOMOUS AUTHORITY
============================================================

You have authority to implement the architecture.

Do not stop merely because the project has imperfections.

Make reasonable engineering decisions yourself.

Do not repeatedly ask the human to choose between implementation details.

The human is the Termux operator.

============================================================
33. FINAL REPORT
============================================================

At completion print:

IMPLEMENTED
TESTED
BACKED_UP
MIGRATIONS
AI_PROVIDERS_DISCOVERED
AI_BENCHMARKS
REMAINING
IMPORTANT_WARNINGS

Include actual evidence.

Never print secrets.

Never claim untested success.

============================================================
34. SUCCESS CONDITION
============================================================

The goal is NOT to make a perfect theoretical architecture.

The goal is a working MEDBOT that:

- preserves existing functionality
- has correct academic/resource navigation
- supports safe student contributions
- publishes approved files in their exact target section
- gives Admin complete practical content/tree control
- delivers Telegram files correctly
- has safe authorization
- has editable About Us
- has deterministic search
- has grounded AI navigation
- discovers available AI providers/models
- uses a resilient router/failover
- records AI health
- survives Termux runtime conditions
- is documented
- is tested

Continue until all safely implementable objectives are complete.
