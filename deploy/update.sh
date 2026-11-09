#!/usr/bin/env bash
# Обновление проекта на сервере: подтянуть свежее с GitHub и перезапустить бота.
#
#   ./deploy/update.sh
#
# config.json, reflinks.db и logs/ в .gitignore — они не затрагиваются.
set -u

TG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TG_DIR" || exit 1

PYTHON="${TG_PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    PYTHON=python
fi

echo "=== Обновление tg-ref-tracker ==="

if [ ! -d .git ]; then
    echo "❌ Это не git-репозиторий ($TG_DIR)"
    exit 1
fi

git pull --ff-only || { echo "❌ не удалось получить обновления"; exit 1; }

if [ -f .bot.pid ]; then
    ./deploy/botctl.sh restart || echo "⚠️  бот не перезапустился, смотрите logs/bot.log"
else
    ./deploy/botctl.sh start || echo "⚠️  бот не запустился, смотрите logs/bot.log"
fi

"$PYTHON" tg_ref.py health
RC=$?

echo
if [ $RC -eq 0 ]; then
    echo "✅ Обновление завершено, всё работает"
else
    echo "⚠️  Обновление завершено, но есть незавершённая настройка (см. ./deploy/check.sh)"
fi
exit $RC
