#!/data/data/com.termux/files/usr/bin/bash

set -u
set -o pipefail

PROJECT="$HOME/MEDBOT"
# Effective SQLite location, matching database.resolve_db_path(): an explicit
# MEDBOT_DB_PATH (e.g. /data/medbot_v2.sqlite3 on Deployka) wins, otherwise the
# default relative file inside the project.
DB_FILE="${MEDBOT_DB_PATH:-$PROJECT/medbot_v2.sqlite3}"
LOG_DIR="$PROJECT/logs"
RUN_ID="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="$LOG_DIR/autonomous-upgrade-$RUN_ID.log"

# Current model verified by a real OpenCode execution on this device.
# Keep this as a bootstrap model only; MEDBOT's own AI Router will later
# maintain the persistent provider/model registry and failover logic.
VERIFIED_MODEL="${MEDBOT_OPENCODE_MODEL:-openrouter/deepseek/deepseek-v4-flash}"

mkdir -p "$LOG_DIR"
cd "$PROJECT" || exit 1

if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock >/dev/null 2>&1 || true
fi

echo "============================================================"
echo " MEDBOT AUTONOMOUS UPGRADE"
echo "============================================================"
echo "START=$(date)"
echo "PROJECT=$PROJECT"
echo "MODEL=$VERIFIED_MODEL"
echo "LOG=$LOG_FILE"

if ! command -v opencode >/dev/null 2>&1; then
    echo "ERROR: opencode not found"
    exit 20
fi

if [ ! -f "$PROJECT/MEDBOT-AUTONOMOUS-UPGRADE.md" ]; then
    echo "ERROR: specification missing"
    exit 21
fi

PY="$PROJECT/venv/bin/python"

if [ ! -x "$PY" ]; then
    echo "ERROR: project Python not found: $PY"
    exit 22
fi

echo
echo "===== INITIAL SAFETY CHECK ====="

"$PY" -m py_compile \
    main.py \
    database.py \
    search_engine.py \
    2>&1 | tee -a "$LOG_FILE"

COMPILE_STATUS=${PIPESTATUS[0]}

if [ "$COMPILE_STATUS" -ne 0 ]; then
    echo "ERROR: existing project does not compile."
    echo "NO AUTONOMOUS MODIFICATION STARTED."
    exit 23
fi

echo "INITIAL_COMPILE=PASS"

echo
echo "===== DATABASE SAFETY ====="

if [ ! -f "$DB_FILE" ]; then
    echo "ERROR: medbot_v2.sqlite3 missing at $DB_FILE."
    exit 24
fi

DB_HASH_BEFORE="$(sha256sum "$DB_FILE" | awk '{print $1}')"
echo "DB_HASH_BEFORE=$DB_HASH_BEFORE"

echo
echo "===== PROJECT SNAPSHOT ====="

{
    echo "RUN=$RUN_ID"
    echo "DATE=$(date)"
    echo "MODEL=$VERIFIED_MODEL"
    echo
    echo "GIT_STATUS"
    git status --short 2>/dev/null || true
    echo
    echo "FILES"
    find . -maxdepth 2 -type f \
        ! -path './venv/*' \
        ! -path './.git/*' \
        | sort | head -300
} >> "$LOG_FILE" 2>&1

echo "Snapshot recorded."

echo
echo "===== OPENCODE MODEL HEALTH CHECK ====="

HEALTH_LOG="$LOG_DIR/model-health-$RUN_ID.log"

HEALTH_START="$(date +%s)"

timeout 50s opencode run \
    --dir "$PROJECT" \
    --model "$VERIFIED_MODEL" \
    --pure \
    --format json \
    --title "MEDBOT Autonomous Model Health Check" \
    "MODEL HEALTH CHECK ONLY.

Do NOT modify, create, delete, rename, move, or write any project file.
Do NOT inspect credentials or secrets.
Do NOT run shell commands.
Respond with exactly:
MEDBOT_AUTONOMOUS_MODEL_OK" \
    2>&1 | tee "$HEALTH_LOG"

HEALTH_STATUS=${PIPESTATUS[0]}

HEALTH_END="$(date +%s)"
HEALTH_SECONDS=$((HEALTH_END - HEALTH_START))

echo "MODEL_HEALTH_EXIT=$HEALTH_STATUS"
echo "MODEL_HEALTH_SECONDS=$HEALTH_SECONDS"

if [ "$HEALTH_STATUS" -eq 124 ]; then
    echo "ERROR_CLASS=TIMEOUT"
    echo "MODEL=$VERIFIED_MODEL"
    echo "AUTONOMOUS_RUN_ABORTED=MODEL_UNAVAILABLE"
    exit 30
fi

if [ "$HEALTH_STATUS" -ne 0 ]; then
    echo "ERROR_CLASS=MODEL_EXECUTION_FAILURE"
    echo "MODEL=$VERIFIED_MODEL"
    echo "AUTONOMOUS_RUN_ABORTED=MODEL_UNAVAILABLE"
    exit 31
fi

if ! grep -q "MEDBOT_AUTONOMOUS_MODEL_OK" "$HEALTH_LOG"; then
    echo "ERROR_CLASS=MODEL_OUTPUT_VALIDATION_FAILURE"
    echo "AUTONOMOUS_RUN_ABORTED=MODEL_OUTPUT_INVALID"
    exit 32
fi

echo "MODEL_HEALTH=PASS"

echo
echo "===== STARTING AUTONOMOUS AGENT ====="

opencode run \
    --dir "$PROJECT" \
    --model "$VERIFIED_MODEL" \
    --agent build \
    --dangerously-skip-permissions \
    --title "MEDBOT Autonomous Full Upgrade" \
    "$(cat "$PROJECT/MEDBOT-AUTONOMOUS-UPGRADE.md")" \
    2>&1 | tee -a "$LOG_FILE"

STATUS=${PIPESTATUS[0]}

echo
echo "============================================================"
echo " OPENCODE RUN FINISHED"
echo "============================================================"
echo "EXIT_CODE=$STATUS"
echo "END=$(date)"

echo
echo "===== POST-RUN COMPILE ====="

"$PY" -m py_compile \
    main.py \
    database.py \
    search_engine.py \
    2>&1 | tee -a "$LOG_FILE"

POST_COMPILE_STATUS=${PIPESTATUS[0]}

echo "POST_COMPILE_EXIT=$POST_COMPILE_STATUS"

echo
echo "===== DATABASE HASH AFTER RUN ====="

if [ -f "$DB_FILE" ]; then
    DB_HASH_AFTER="$(sha256sum "$DB_FILE" | awk '{print $1}')"
    echo "DB_HASH_AFTER=$DB_HASH_AFTER"
fi

echo
echo "===== RECENT BACKUPS ====="

ls -lht \
    main.py.backup-* \
    database.py.backup-* \
    medbot_v2.sqlite3.backup-* \
    run_autonomous*.backup-* \
    2>/dev/null | head -40 | tee -a "$LOG_FILE" || true

echo
echo "===== GIT STATUS ====="

git status --short 2>/dev/null | tee -a "$LOG_FILE" || true

echo
echo "===== AUTONOMOUS RUN SUMMARY ====="

echo "RUN_ID=$RUN_ID"
echo "MODEL=$VERIFIED_MODEL"
echo "OPENCODE_EXIT=$STATUS"
echo "POST_COMPILE_EXIT=$POST_COMPILE_STATUS"
echo "LOG_FILE=$LOG_FILE"

if [ "$STATUS" -ne 0 ]; then
    exit "$STATUS"
fi

if [ "$POST_COMPILE_STATUS" -ne 0 ]; then
    exit 40
fi

exit 0
