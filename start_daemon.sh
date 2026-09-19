#!/bin/bash
cd ~/MEDBOT
source venv/bin/activate

while true; do
    echo "[$(date)] جارٍ تشغيل البوت..."
    python main.py
    EXIT_CODE=$?
    echo "[$(date)] توقف البوت مع كود خروج ($EXIT_CODE). إعادة المحاولة خلال 5 ثوانٍ..."
    sleep 5
done
