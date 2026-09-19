#!/bin/bash
cd ~/MEDBOT
source venv/bin/activate

echo "=========================================="
echo "🛡️ MEDBOT Watchdog Daemon Started"
echo "=========================================="

while true; do
    echo "[$(date)] Starting MEDBOT process..."
    python main.py
    EXIT_CODE=$?
    echo "[$(date)] Bot stopped with exit code $EXIT_CODE. Restarting in 3 seconds..."
    sleep 3
done
