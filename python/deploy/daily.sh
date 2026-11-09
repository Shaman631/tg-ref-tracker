#!/usr/bin/env bash
# Ежедневная задача трекера ссылок. Запускается по cron.
# Использование: daily.sh [--catchup]
set -u

TG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TG_DIR" || exit 1

LOG_DIR="$TG_DIR/logs"
mkdir -p "$LOG_DIR" 2>/dev/null || LOG_DIR="/tmp"

PYTHON="${TG_PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    PYTHON=python
fi

STAMP="$(date '+%Y-%m-%d %H:%M:%S')"
{
    echo "=============================================================="
    echo "$STAMP  старт"
} >> "$LOG_DIR/daily.log"

if [ "${1:-}" = "--catchup" ]; then
    "$PYTHON" tg_ref.py daily --if-due >> "$LOG_DIR/daily.log" 2>&1
else
    "$PYTHON" tg_ref.py daily >> "$LOG_DIR/daily.log" 2>&1
fi
RC=$?

echo "$STAMP  завершено (код $RC)" >> "$LOG_DIR/daily.log"
exit $RC
