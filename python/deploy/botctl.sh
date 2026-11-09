#!/usr/bin/env bash
# Управление фоновым ботом: ./botctl.sh start|stop|restart|status|logs|ensure
# Бот отвечает в Telegram на /links, /newlinks, /stats, /channels.
set -u

TG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TG_DIR" || exit 1

LOG_DIR="$TG_DIR/logs"
mkdir -p "$LOG_DIR" 2>/dev/null || LOG_DIR="/tmp"
PID_FILE="$TG_DIR/.bot.pid"

PYTHON="${TG_PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    PYTHON=python
fi

# PID пишет сам процесс бота (tg_ref.py) — так он точно соответствует нужному процессу.
is_running() {
    if [ -f "$PID_FILE" ]; then
        pid="$(cat "$PID_FILE" 2>/dev/null)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    # запасной вариант: ищем процесс по командной строке
    if command -v pgrep >/dev/null 2>&1; then
        pgrep -f "tg_ref.py bot" >/dev/null 2>&1 && return 0
    fi
    return 1
}

start() {
    if is_running; then
        echo "Бот уже запущен"
        return 0
    fi
    nohup "$PYTHON" tg_ref.py bot >> "$LOG_DIR/bot.log" 2>&1 &
    sleep 2
    if is_running; then
        echo "✅ Бот запущен"
    else
        echo "❌ Не удалось запустить бота, смотрите $LOG_DIR/bot.log"
        return 1
    fi
}

stop() {
    stopped=0
    if [ -f "$PID_FILE" ]; then
        pid="$(cat "$PID_FILE" 2>/dev/null)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            sleep 1
            kill -9 "$pid" 2>/dev/null
            stopped=1
        fi
        rm -f "$PID_FILE"
    fi
    if command -v pkill >/dev/null 2>&1; then
        pkill -f "tg_ref.py bot" 2>/dev/null && stopped=1
    fi
    if [ "$stopped" = "1" ]; then
        echo "✅ Бот остановлен"
    else
        echo "Бот не запущен"
    fi
}

status() {
    if is_running; then
        echo "Бот работает"
        return 0
    fi
    echo "Бот не запущен"
    return 1
}

case "${1:-status}" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; start ;;
    status)  status ;;
    logs)    tail -n 50 "$LOG_DIR/bot.log" ;;
    ensure)  is_running || start ;;   # для крона: поднять бота, если он упал
    *) echo "Использование: $0 start|stop|restart|status|logs|ensure"; exit 1 ;;
esac
