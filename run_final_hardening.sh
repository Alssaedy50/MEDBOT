#!/data/data/com.termux/files/usr/bin/bash
set -u

ROOT="$HOME/MEDBOT"
cd "$ROOT" || exit 1

mkdir -p logs
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="logs/final-hardening-${STAMP}.log"

{
    echo "=================================================="
    echo "MEDBOT FINAL HARDENING PASS"
    echo "START: $(date)"
    echo "MODEL: openrouter/nex-agi/nex-n2.5-mini:free"
    echo "=================================================="
} | tee -a "$LOG"

if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock || true
    echo "Wake lock: enabled" | tee -a "$LOG"
fi

echo "Launching OpenCode hardening agent..." | tee -a "$LOG"

exec opencode run \
  --dir "$ROOT" \
  --model openrouter/nex-agi/nex-n2.5-mini:free \
  --agent build \
  --dangerously-skip-permissions \
  --title "MEDBOT FINAL HARDENING" \
'
You are the FINAL HARDENING ENGINEER for MEDBOT.

READ FIRST:
~/MEDBOT/MEDBOT-FINAL-CONTINUATION.md
~/MEDBOT/logs/final-report-20260916.txt

Your job is ONLY FINAL HARDENING.
Do NOT rebuild MEDBOT.

==================================================
1. AUDIT FIRST
==================================================

Inspect the actual current state before changing anything.

Inspect:
- main.py
- database.py
- search_engine.py
- ai_router.py
- ai_discovery.py
- current SQLite database/schema/data
- final-report-20260916.txt

Preserve all working functionality.

NEVER:
- wipe/recreate the database
- reset existing data
- reinstall working tools
- remove IPv4 workaround
- remove run_bot.sh
- inspect ~/.local/share/opencode/auth.json
- invent API keys
- invent providers
- expose secrets

==================================================
2. GROUNDING VALIDATOR
==================================================

Current weakness:
GroundingValidator.validate() is effectively pass-through.

Implement a practical deterministic post-response resource-grounding validator.

Its purpose is ONLY to verify MEDBOT resource claims.

It must detect unsupported claims involving:
- filenames
- folders
- paths
- lectures/resources
- links/resources

Use actual grounded search/context data.

Do NOT build a medical fact checker.
Do NOT build a huge NLP system.

Safe behavior:
- verified resource claims remain allowed
- unsupported resource claims must not be presented as verified
- non-existent resources must preserve:
"الموارد المطلوبة غير مسجلة حالياً في MEDBOT."

Add focused tests for:
1. existing resource
2. existing folder
3. invented filename
4. invented folder/path
5. non-existent resource
6. MEDBOT meta question
7. Arabic names
8. English names

Preserve current AI router behavior.

==================================================
3. AI REGISTRY DEDUPLICATION
==================================================

Repeated discovery currently creates duplicate provider/model rows.

Implement idempotent registry upsert.

Canonical identity:
provider + model + endpoint

Existing identity:
UPDATE.

New identity:
INSERT.

Do NOT overwrite useful non-null historical fields with NULL.

Existing duplicate rows in the real database MUST NOT be blindly wiped.

Perform verified deduplication:
- identify true duplicates
- preserve useful information
- merge where appropriate
- delete only true duplicates
- verify foreign-key safety
- run PRAGMA integrity_check

If adding a unique index/constraint, make the migration idempotent and safe.

==================================================
4. PROVIDERS
==================================================

Current known configuration:
- Google Gemini: configured/tested
- OpenRouter: runtime application key may be absent
- OpenAI: not configured

Do not invent credentials.

Do not make OpenCode a permanent MEDBOT runtime dependency.

Keep the existing adapters capable of using valid environment variables later.

==================================================
5. ROUTER
==================================================

Preserve:
- provider ranking
- VERIFIED preference
- success-rate/latency logic
- transient error fallback
- permanent error handling
- timeout handling
- registry updates

Do not replace the router with a simplified implementation.

==================================================
6. DATA SAFETY
==================================================

Preserve the current real data.

Known important state:

Contribution #1:
- folder 21
- approved
- repaired content exists exactly once

Contribution #2:
- folder 32
- pending
- MUST remain pending

Contribution #3:
- approved
- metadata repaired

Contribution #4:
- approved
- metadata repaired

Do not change these states unless a verified integrity problem requires it.

==================================================
7. TEST EVERYTHING
==================================================

After modifications run:

- Python compilation
- database integrity
- foreign-key verification through get_db()
- registry uniqueness test
- repeated discovery/upsert test
- grounding validator tests
- search regression
- contribution lifecycle regression
- admin authorization regression
- folder safety regression
- content retrieval regression
- file delivery regression
- AI router regression
- failover classification regression
- AI benchmark where credentials permit
- grounding benchmark
- verify IPv4 workaround remains
- verify run_bot.sh remains executable
- verify no secrets are printed

If live Telegram integration cannot be performed, report it as NOT TESTED rather than claiming success.

==================================================
8. FINAL REPORT
==================================================

Create:

~/MEDBOT/logs/final-hardening-report-${STAMP}.txt

Required sections:

IMPLEMENTED
TESTED
DATABASE
AI_ROUTING
GROUNDING
REGISTRY
WARNINGS
REMAINING
FINAL_STATUS

FINAL_STATUS must be exactly:

FINAL_STATUS=COMPLETE

or:

FINAL_STATUS=COMPLETE_WITH_WARNINGS

or:

FINAL_STATUS=BLOCKED

Do not claim COMPLETE if important hardening tests fail.

Environmental limitations such as unavailable OpenRouter/OpenAI credentials or unavailable live Telegram testing may be recorded as warnings.

==================================================
9. EXIT CONDITION
==================================================

Do not stop after editing.
Do not stop after compilation.
Do not stop after one test.

Continue until:
- hardening is implemented
- tests are completed
- final report is saved
- FINAL_STATUS is printed

Print the complete final report at the end.

This is the FINAL HARDENING PASS.
'

