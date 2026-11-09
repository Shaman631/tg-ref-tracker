#!/usr/bin/env bash
# Проверка готовности хостинга: запускается один раз после заливки файлов.
set -u

TG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TG_DIR" || exit 1

PYTHON="${TG_PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    PYTHON=python
fi

echo "=== Проверка окружения ==="
"$PYTHON" --version
echo "Каталог: $TG_DIR"

if [ ! -f config.json ]; then
    echo "⚠️  config.json не найден — скопируйте config.json из локальной папки и заполните его"
fi

chmod 600 config.json 2>/dev/null
mkdir -p logs 2>/dev/null

echo
echo "=== Диагностика tg_ref ==="
"$PYTHON" tg_ref.py health
RC=$?

echo
if [ $RC -ne 0 ]; then
    echo "Настройка не завершена. Заполните в config.json:"
    echo "  bot_token       — токен от @BotFather"
    echo "  report_chat_id  — ваш chat_id (напишите боту /start)"
    echo "  channels        — список каналов"
    echo
    echo "Дальше:"
    echo "  ./deploy/botctl.sh start     # запустить бота"
    echo "  ./deploy/daily.sh            # создать ссылки и отчёт прямо сейчас"
fi
exit $RC
