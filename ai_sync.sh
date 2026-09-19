#!/bin/bash

REPORT_FILE=$(mktemp)

{
    echo "=================================================="
    echo "       MEDBOT DIAGNOSTIC & TELEMETRY REPORT       "
    echo "       Date: $(date '+%Y-%m-%d %H:%M:%S')         "
    echo "=================================================="
    echo ""
    
    echo "--- [1] PM2 & PROCESS STATUS ---"
    pm2 status 2>&1 || echo "PM2 not running"
    echo ""

    echo "--- [2] PROJECT STRUCTURE & FILES ---"
    ls -la ~/MEDBOT 2>&1
    echo ""

    if [ $# -gt 0 ]; then
        echo "--- [3] EXECUTED COMMAND: $@ ---"
        "$@" 2>&1
        echo ""
    fi

    echo "--- [4] RECENT RUNTIME & ERROR LOGS (MEDBOT) ---"
    pm2 logs MEDBOT --lines 60 --nostream 2>&1 || true
    echo ""

    echo "--- [5] KEY SCRIPTS SNAPSHOTS ---"
    for pyfile in main.py ai_architect.py ai_router.py; do
        if [ -f "$HOME/MEDBOT/$pyfile" ]; then
            echo ">>> FILE: $pyfile <<<"
            cat "$HOME/MEDBOT/$pyfile"
            echo ""
        fi
    done
} > "$REPORT_FILE"

echo "📡 جاري رفع التقرير عبر IPv4..."

# الرفع المباشر عبر dpaste.com المعتمد دولياً وبإجبار IPv4
LINK=$(curl -4 -k -s -m 20 -F "content=@$REPORT_FILE" -F "expiry_days=7" https://dpaste.com/api/v2/ 2>/dev/null | tr -d '\r\n')

# مزود احتياطي سريع إذا تعثر الأول
if [[ ! "$LINK" =~ ^https?:// ]]; then
    LINK=$(curl -4 -k -s -m 20 --data-binary @"$REPORT_FILE" https://paste.c-net.org/ 2>/dev/null | tr -d '\r\n')
fi

rm -f "$REPORT_FILE"

echo ""
if [[ "$LINK" =~ ^https?:// ]]; then
    echo -e "\033[1;32m====================================================\033[0m"
    echo -e "\033[1;32m✅ تم إنشاء الرابط بنجاح!\033[0m"
    echo -e "\033[1;33m🔗 الرابط: $LINK\033[0m"
    echo -e "\033[1;32m====================================================\033[0m"
    echo "انسخ هذا الرابط والصقه هنا مباشرة."
else
    echo -e "\033[1;31m❌ تعذر الرفع عبر الشبكة.\033[0m"
fi
