#!/bin/bash
# MEDBOT watchdog launcher. Starts the bot from the repo root and keeps it
# alive. Before starting it pulls the latest merged code from GitHub when it is
# safe to do so, so a reboot always runs the newest version.
set -u

# Resolve the directory this script lives in, so it works whether it is invoked
# from $HOME/MEDBOT or by an absolute path.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Best-effort update from GitHub. Skipped unless this is a git checkout with a
# clean tracked tree, so local operator edits or a half-finished change are
# never overwritten. A failed fetch (offline, no credentials) is non-fatal.
if command -v git >/dev/null 2>&1 && [ -d .git ]; then
    echo "[$(date)] Checking GitHub for updates..."
    if [ -z "$(git status --porcelain --untracked-files=no)" ]; then
        if git pull --ff-only origin main; then
            echo "[$(date)] Code is up to date."
        else
            echo "[$(date)] Update skipped (diverged or offline); continuing."
        fi
    else
        echo "[$(date)] Local changes present; update skipped."
    fi
fi

# Activate the virtualenv if this checkout ships one.
if [ -f venv/bin/activate ]; then
    # shellcheck disable=SC1091
    source venv/bin/activate
fi

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
