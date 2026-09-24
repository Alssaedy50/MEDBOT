#!/bin/bash
# MEDBOT launcher (Arabic messages). Same behavior as run_bot.sh: pull the
# latest merged code from GitHub when safe, then keep the bot alive.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Best-effort update from GitHub; skipped while local tracked changes exist.
if command -v git >/dev/null 2>&1 && [ -d .git ]; then
    echo "[$(date)] جارٍ التحقق من تحديثات GitHub..."
    if [ -z "$(git status --porcelain --untracked-files=no)" ]; then
        git pull --ff-only origin main \
            && echo "[$(date)] الكود محدّث." \
            || echo "[$(date)] تم تخطي التحديث (تعذّر الاتصال أو يوجد تفريع)."
    else
        echo "[$(date)] توجد تعديلات محلية؛ تم تخطي التحديث."
    fi
fi

if [ -f venv/bin/activate ]; then
    # shellcheck disable=SC1091
    source venv/bin/activate
fi

while true; do
    echo "[$(date)] جارٍ تشغيل البوت..."
    python main.py
    EXIT_CODE=$?
    echo "[$(date)] توقف البوت مع كود خروج ($EXIT_CODE). إعادة المحاولة خلال 5 ثوانٍ..."
    sleep 5
done
