#!/usr/bin/env bash
# Установка трекера на VPS с Ubuntu/Debian (без Docker).
#
#   sudo bash deploy/install-vps.sh
#
# Что делает:
#   1) ставит nginx и php-cli
#   2) копирует проект в /opt/tg-ref
#   3) создаёт /etc/tg-ref.env из примера (если его ещё нет)
#   4) ставит systemd-службы веба и воркера
#   5) подключает nginx и перезагружает его
#
# После установки впишите токен и каналы в /etc/tg-ref.env и выполните:
#   sudo systemctl restart tg-ref-web tg-ref-worker

set -euo pipefail

APP_DIR="/opt/tg-ref"
ENV_FILE="/etc/tg-ref.env"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== Установка трекера на VPS ==="

if [ "$(id -u)" != "0" ]; then
    echo "❌ Нужен root: sudo bash $0"
    exit 1
fi

echo "1) Ставлю nginx и php…"
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx php-cli >/dev/null
else
    echo "   ⚠️  apt-get не найден — поставьте nginx и php-cli вручную"
fi

echo "2) Копирую проект в $APP_DIR"
mkdir -p "$APP_DIR"
for f in index.php worker.php lib.php cron.php config.local.example.php; do
    [ -f "$SRC_DIR/$f" ] && cp "$SRC_DIR/$f" "$APP_DIR/$f"
done
mkdir -p "$APP_DIR/logs"
chown -R www-data:www-data "$APP_DIR"

if [ ! -f "$ENV_FILE" ]; then
    echo "3) Создаю $ENV_FILE из примера — впишите токен и каналы"
    cp "$SRC_DIR/deploy/tg-ref.env.example" "$ENV_FILE"
    sed -i "s#STATE_FILE=.*#STATE_FILE=/var/lib/tg-ref/state.json#" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
else
    echo "3) $ENV_FILE уже есть — не трогаю"
fi

mkdir -p /var/lib/tg-ref
chown www-data:www-data /var/lib/tg-ref

echo "4) Ставлю systemd-службы"
cp "$SRC_DIR/deploy/tg-ref-web.service" /etc/systemd/system/
cp "$SRC_DIR/deploy/tg-ref-worker.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tg-ref-web tg-ref-worker

echo "5) Подключаю nginx"
cp "$SRC_DIR/deploy/nginx.conf" /etc/nginx/sites-available/tg-ref
ln -sf /etc/nginx/sites-available/tg-ref /etc/nginx/sites-enabled/tg-ref
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo
echo "=== Готово ==="
echo "1) Впишите настройки:  nano $ENV_FILE"
echo "   (BOT_TOKEN, CHAT_ID, CHANNELS)"
echo "2) Перезапустите:      sudo systemctl restart tg-ref-web tg-ref-worker"
echo "3) Проверьте:          curl http://127.0.0.1:8080/health"
echo "4) Домен: в deploy/nginx.conf замените tracker.example.com и выполните"
echo "   sudo nginx -t && sudo systemctl reload nginx"
echo "5) Ключ доступа: напишите боту /webkey в Telegram"
