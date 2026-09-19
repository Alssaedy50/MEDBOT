#!/data/data/com.termux/files/usr/bin/bash

set -u

ROOT="$HOME/MEDBOT"
LOG_DIR="$ROOT/logs"
cd "$ROOT" || exit 1

mkdir -p "$LOG_DIR"

WATCH_LOG="$LOG_DIR/final-watchdog-$(date +%Y%m%d-%H%M%S).log"
START_EPOCH="$(date +%s)"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$WATCH_LOG"
}

log "=================================================="
log "MEDBOT FINAL WATCHDOG STARTED"
log "=================================================="
log "START_EPOCH=$START_EPOCH"

# Keep Android awake.
if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock || true
    log "Wake lock enabled"
fi

# ------------------------------------------------------------
# Find FINAL_STATUS only in files MODIFIED after this watchdog
# started. This prevents old reports from falsely completing
# the current task.
# ------------------------------------------------------------
new_final_status() {
    local f=""
    local status=""

    for f in "$LOG_DIR"/*.txt "$LOG_DIR"/*.log; do
        [ -f "$f" ] || continue

        local mtime
        mtime="$(stat -c %Y "$f" 2>/dev/null || echo 0)"

        if [ "$mtime" -lt "$START_EPOCH" ]; then
            continue
        fi

        status="$(
            tail -300 "$f" 2>/dev/null |
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

# ------------------------------------------------------------
# Current OpenCode detector.
# ------------------------------------------------------------
find_opencode() {
    pgrep -f 'opencode.*MEDBOT' 2>/dev/null |
        while read -r p; do
            [ "$p" = "$$" ] && continue
            echo "$p"
        done
}

# ------------------------------------------------------------
# Monitor the EXISTING OpenCode process first.
# We do not launch another one while it is alive.
# ------------------------------------------------------------
log "Checking existing MEDBOT OpenCode process..."

EXISTING="$(find_opencode | head -1 || true)"

if [ -n "$EXISTING" ]; then
    log "Existing OpenCode detected: PID=$EXISTING"
    log "This process will NOT be interrupted."
else
    log "No existing OpenCode process detected."
    log "Launching continuation agent..."

    ATTEMPT_LOG="$LOG_DIR/watchdog-continuation-$(date +%Y%m%d-%H%M%S).log"

    timeout 3600s \
        opencode run \
        --dir "$ROOT" \
        --model openrouter/deepseek/deepseek-v4-flash \
        --agent build \
        --dangerously-skip-permissions \
        --title "MEDBOT FINAL CONTINUATION WATCHDOG" \
        "Read ~/MEDBOT/MEDBOT-FINAL-CONTINUATION.md and continue MEDBOT from the actual current state. Execute every unfinished phase. Do not rebuild or wipe working components. Audit first, repair only verified issues, test changes, and do not stop merely because one phase is finished. You must save a final report and explicitly print FINAL_STATUS=COMPLETE, COMPLETE_WITH_WARNINGS, or BLOCKED only after the entire specification is actually completed." \
        >"$ATTEMPT_LOG" 2>&1 &

    EXISTING="$!"
    log "New OpenCode launched: PID=$EXISTING"
fi

# ------------------------------------------------------------
# Persistent monitoring loop.
# ------------------------------------------------------------
while true; do

    # A valid NEW status always wins.
    if STATUS="$(new_final_status)"; then
        log "NEW FINAL STATUS DETECTED: $STATUS"

        if [ "$STATUS" = "BLOCKED" ]; then
            log "Task is blocked. Stopping watchdog for human review."
        else
            log "Task completed according to a NEW report."
        fi

        break
    fi

    # Check whether OpenCode is still alive.
    CURRENT="$(find_opencode | head -1 || true)"

    if [ -n "$CURRENT" ]; then
        log "OpenCode alive: PID=$CURRENT"
    else
        log "OpenCode is no longer running and no final status exists."
        log "Starting a fresh continuation attempt..."

        ATTEMPT_LOG="$LOG_DIR/watchdog-continuation-$(date +%Y%m%d-%H%M%S).log"

        timeout 3600s \
            opencode run \
            --dir "$ROOT" \
            --model openrouter/deepseek/deepseek-v4-flash \
            --agent build \
            --dangerously-skip-permissions \
            --title "MEDBOT FINAL CONTINUATION WATCHDOG RESUME" \
            "Read ~/MEDBOT/MEDBOT-FINAL-CONTINUATION.md and resume the MEDBOT project from its actual current state. A previous autonomous attempt ended before producing the required final report. Continue ALL unfinished work. Do not wipe, rebuild, or undo working components. Audit, repair, test, document, and continue until the complete specification is satisfied. You MUST save and print a NEW final report containing FINAL_STATUS=COMPLETE, COMPLETE_WITH_WARNINGS, or BLOCKED. Do not use old reports as evidence of completion." \
            >"$ATTEMPT_LOG" 2>&1

        RC=$?
        log "OpenCode resume exit code=$RC"

        sleep 10
    fi

    sleep 30
done

if command -v termux-wake-unlock >/dev/null 2>&1; then
    termux-wake-unlock || true
fi

log "=================================================="
log "FINAL WATCHDOG STOPPED"
log "STATUS=${STATUS:-UNKNOWN}"
log "END=$(date)"
log "=================================================="
