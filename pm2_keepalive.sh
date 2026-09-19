#!/data/data/com.termux/files/usr/bin/bash
cd "$HOME/MEDBOT" || exit 1
termux-wake-lock
pm2 describe MEDBOT >/dev/null 2>&1 || pm2 start main.py --name MEDBOT --interpreter python --time
pm2 save >/dev/null 2>&1
