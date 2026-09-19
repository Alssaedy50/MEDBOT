#!/data/data/com.termux/files/usr/bin/bash

set -u
set -o pipefail

PROJECT="$HOME/MEDBOT"
LOG_DIR="$PROJECT/logs"
MAX_ATTEMPTS="${MEDBOT_MAX_AUTONOMOUS_ATTEMPTS:-5}"
RETRY_DELAY="${MEDBOT_AUTONOMOUS_RETRY_DELAY:-20}"

mkdir -p "$LOG_DIR"
cd "$PROJECT" || exit 1

if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock >/dev/null 2>&1 || true
fi

echo "============================================================"
echo " MEDBOT AUTONOMOUS SUPERVISOR"
echo "============================================================"
echo "PROJECT=$PROJECT"
echo "MAX_ATTEMPTS=$MAX_ATTEMPTS"
echo "START=$(date)"

ATTEMPT=0

while [ "$ATTEMPT" -lt "$MAX_ATTEMPTS" ]; do

    ATTEMPT=$((ATTEMPT + 1))

    echo
    echo "============================================================"
    echo " AUTONOMOUS ATTEMPT #$ATTEMPT"
    echo " TIME=$(date)"
    echo "============================================================"

    RUN_LOG="$LOG_DIR/supervisor-attempt-$ATTEMPT-$(date +%Y%m%d-%H%M%S).log"

    bash "$PROJECT/run_autonomous_upgrade.sh" \
        2>&1 | tee -a "$RUN_LOG"

    STATUS=${PIPESTATUS[0]}

    echo
    echo "ATTEMPT=$ATTEMPT"
    echo "EXIT=$STATUS"
    echo "TIME=$(date)"

    if [ "$STATUS" -eq 0 ]; then
        echo "AUTONOMOUS_STATUS=SUCCESS"
        echo "Supervisor stopping normally."
        exit 0
    fi

    # Permanent/configuration failures:
    # Do not blindly repeat the same failed configuration.
    case "$STATUS" in
        20|21|22|23|24|30|31|32)
            echo "AUTONOMOUS_STATUS=STOPPED_PERMANENT_OR_PRECONDITION_FAILURE"
            echo "NO_BLIND_RETRY=YES"
            echo "See: $RUN_LOG"
            exit "$STATUS"
            ;;
    esac

    if [ "$ATTEMPT" -ge "$MAX_ATTEMPTS" ]; then
        echo "AUTONOMOUS_STATUS=MAX_ATTEMPTS_REACHED"
        echo "NO_FURTHER_RETRIES=YES"
        exit "$STATUS"
    fi

    echo "AUTONOMOUS_STATUS=RETRYABLE_FAILURE"
    echo "Waiting ${RETRY_DELAY}s before next pass..."

    sleep "$RETRY_DELAY"
done

exit 1
