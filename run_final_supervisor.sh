#!/data/data/com.termux/files/usr/bin/bash

set -u

ROOT="$HOME/MEDBOT"
cd "$ROOT" || exit 1

LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

SUP_LOG="$LOG_DIR/final-supervisor-$(date +%Y%m%d-%H%M%S).log"
SPEC="$ROOT/MEDBOT-FINAL-CONTINUATION.md"

echo "==================================================" | tee -a "$SUP_LOG"
echo "MEDBOT FINAL CONTINUATION SUPERVISOR" | tee -a "$SUP_LOG"
echo "START: $(date)" | tee -a "$SUP_LOG"
echo "ROOT: $ROOT" | tee -a "$SUP_LOG"
echo "SPEC: $SPEC" | tee -a "$SUP_LOG"
echo "==================================================" | tee -a "$SUP_LOG"

if [ ! -f "$SPEC" ]; then
    echo "FATAL: continuation spec not found: $SPEC" | tee -a "$SUP_LOG"
    exit 1
fi

# Keep Android/Termux awake while the autonomous work is running.
if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock || true
    echo "Wake lock: enabled" | tee -a "$SUP_LOG"
fi

# We intentionally use the already-tested OpenCode CLI mechanism.
command -v opencode >/dev/null 2>&1 || {
    echo "FATAL: opencode command not found" | tee -a "$SUP_LOG"
    exit 1
}

echo "OpenCode: $(command -v opencode)" | tee -a "$SUP_LOG"
opencode --version 2>&1 | tee -a "$SUP_LOG"

# --------------------------------------------------
# Detect completion / blocking status from generated
# reports and logs.
# --------------------------------------------------
check_final_status() {
    local status=""
    local f=""

    # Only inspect small/relevant report files.
    # Never recursively scan the entire logs directory.
    for f in \
        "$ROOT"/logs/*final*report*.txt \
        "$ROOT"/logs/*FINAL*.txt \
        "$ROOT"/logs/*continuation*.log \
        "$ROOT"/logs/*supervisor*.log
    do
        [ -f "$f" ] || continue

        # Read only the last 200 lines of each candidate.
        status="$(
            tail -200 "$f" 2>/dev/null |
            grep -hoE 'FINAL_STATUS[[:space:]]*=[[:space:]]*[A-Z_]+' |
            tail -n 1 |
            sed -E 's/.*=[[:space:]]*//'
        )"

        case "$status" in
            COMPLETE|COMPLETE_WITH_WARNINGS|BLOCKED)
                echo "$status"
                return 0
                ;;
        esac
    done

    return 1
}
# --------------------------------------------------
# Prevent accidental parallel MEDBOT OpenCode agents.
# --------------------------------------------------
another_agent_running() {
    pgrep -af 'opencode.*MEDBOT' 2>/dev/null |
        grep -v "$$" |
        grep -q .
}

ATTEMPT=0

while true; do
    ATTEMPT=$((ATTEMPT + 1))

    echo "" | tee -a "$SUP_LOG"
    echo "==================================================" | tee -a "$SUP_LOG"
    echo "ATTEMPT #$ATTEMPT — $(date)" | tee -a "$SUP_LOG"
    echo "==================================================" | tee -a "$SUP_LOG"

    # First check whether a previous attempt actually completed.
    if STATUS="$(check_final_status)"; then
        echo "FINAL STATUS ALREADY PRESENT: $STATUS" | tee -a "$SUP_LOG"
        break
    fi

    # Never launch two autonomous agents simultaneously.
    if another_agent_running; then
        echo "Another MEDBOT OpenCode agent is currently running." | tee -a "$SUP_LOG"
        echo "Waiting 30 seconds..." | tee -a "$SUP_LOG"
        sleep 30
        continue
    fi

    ATTEMPT_LOG="$LOG_DIR/final-continuation-attempt-${ATTEMPT}-$(date +%Y%m%d-%H%M%S).log"

    echo "Launching OpenCode..." | tee -a "$SUP_LOG"
    echo "Attempt log: $ATTEMPT_LOG" | tee -a "$SUP_LOG"

    # IMPORTANT:
    # OpenCode is the CHILD.
    # This supervisor remains alive after OpenCode exits.
    #
    # The task specification is already stored in the project, so the
    # agent can resume from the real current state rather than relying
    # on conversation history.
    timeout 3600s \
        opencode run \
        --dir "$ROOT" \
        --model openrouter/deepseek/deepseek-v4-flash \
        --agent build \
        --dangerously-skip-permissions \
        --title "MEDBOT FINAL CONTINUATION — Attempt $ATTEMPT" \
        "Read ~/MEDBOT/MEDBOT-FINAL-CONTINUATION.md and continue the MEDBOT project from the ACTUAL CURRENT STATE. Do not restart or rebuild working components. Execute the full continuation specification autonomously. Audit first, repair only verified issues, test every important change, and continue until the specification's exit condition is genuinely satisfied. You MUST save and PRINT the final report with an explicit FINAL_STATUS=COMPLETE, COMPLETE_WITH_WARNINGS, or BLOCKED. Do not stop merely because one phase is finished. If the previous attempt stopped before the final report, resume the unfinished work." \
        2>&1 | tee "$ATTEMPT_LOG"

    RC=${PIPESTATUS[0]}

    echo "OpenCode exit code: $RC" | tee -a "$SUP_LOG"

    # Check immediately after the child exits.
    if STATUS="$(check_final_status)"; then
        echo "FINAL STATUS DETECTED: $STATUS" | tee -a "$SUP_LOG"
        break
    fi

    if [ "$RC" -eq 124 ]; then
        echo "OpenCode timed out after 3600 seconds." | tee -a "$SUP_LOG"
    elif [ "$RC" -ne 0 ]; then
        echo "OpenCode exited with error code $RC." | tee -a "$SUP_LOG"
    else
        echo "OpenCode exited normally WITHOUT final status." | tee -a "$SUP_LOG"
    fi

    echo "The supervisor will resume the continuation automatically." | tee -a "$SUP_LOG"
    echo "Waiting 20 seconds..." | tee -a "$SUP_LOG"
    sleep 20
done

echo "" | tee -a "$SUP_LOG"
echo "==================================================" | tee -a "$SUP_LOG"
echo "SUPERVISOR FINISHED" | tee -a "$SUP_LOG"
echo "FINAL STATUS: ${STATUS:-UNKNOWN}" | tee -a "$SUP_LOG"
echo "END: $(date)" | tee -a "$SUP_LOG"
echo "SUPERVISOR LOG: $SUP_LOG" | tee -a "$SUP_LOG"
echo "==================================================" | tee -a "$SUP_LOG"

# Release wake lock only after the whole continuation is finished.
if command -v termux-wake-unlock >/dev/null 2>&1; then
    termux-wake-unlock || true
fi
