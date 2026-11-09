#!/usr/bin/env bash
# Установка трекера реферальных ссылок на Android (Termux).
#
#   bash deploy/setup-termux.sh
#
# Что делает:
#   1) ставит python, cron, openssh, termux-boot
#   2) готовит каталог проекта и config.json
#   3) прописывает crontab: ежечасная догоняющая проверка + watchdog бота
#   4) запускает crond и бота
set -u

PROJ="$HOME/tg-ref-tracker"
LOG_DIR="$PROJ/logs"
CRONTAB_FILE="$PREFIX/etc/crontab"

echo "=== Установка трекера в Termux ==="

if [ -z "${PREFIX:-}" ] || [ ! -d "$PREFIX" ]; then
    echo "❌ Это не Termux. Запускайте скрипт внутри Termux."
    exit 1
fi

echo "1) Ставлю пакеты…"
pkg install -y python cron openssh termux-boot >/dev/null 2>&1 || {
    echo "   pkg install не сработал, пробую без termux-boot"
    pkg install -y python cron openssh >/dev/null 2>&1
}
echo "   ok: $(python --version 2>&1), cron: $(command -v crond || echo 'нет')"

echo "2) Готовлю каталог проекта: $PROJ"
mkdir -p "$PROJ" "$LOG_DIR"

if [ ! -f "$PROJ/tg_ref.py" ]; then
    echo "   Проекта ещё нет. Выберите способ (см. README):"
    echo "     а) deploy key:  bash deploy/setup-git.sh $PROJ"
    echo "     б) архив:       скопируйте tg-ref-deploy.zip на телефон и выполните"
    echo "                     cd ~ && unzip -o tg-ref-deploy.zip && mv tg-ref-tracker/* tg-ref-tracker/ 2>/dev/null; true"
    echo
    echo "   Скачайте проект одним из способов выше и запустите скрипт заново."
    exit 1
fi

if [ ! -f "$PROJ/config.json" ]; then
    cp "$PROJ/config.example.json" "$PROJ/config.json"
    chmod 600 "$PROJ/config.json"
    echo "   Создан config.json из шаблона — заполните токен и каналы"
fi

chmod +x "$PROJ"/deploy/*.sh

echo "3) Прописываю crontab"
CRON_DIR="$HOME/.termux/boot"
mkdir -p "$CRON_DIR"

cat > "$CRONTAB_FILE" <<EOF
# Трекер реферальных ссылок — Termux
SHELL=$PREFIX/bin/bash
PATH=$PREFIX/bin:/system/bin
TERMUX_DIR=$PROJ

# Каждый час: догоняющая проверка. Скрипт сам решит, наступило ли время
# (schedule_time в config.json) и не отправит повторный отчёт.
0 * * * * $PROJ/deploy/daily.sh --catchup >> $LOG_DIR/cron.log 2>&1

# Каждые 10 минут: поднять бота, если он упал.
*/10 * * * * $PROJ/deploy/botctl.sh ensure >> $LOG_DIR/cron.log 2>&1
EOF
echo "   $CRONTAB_FILE обновлён"

echo "4) Запускаю cron и бота"
if ! pgrep -x crond >/dev/null 2>&1; then
    crond >/dev/null 2>&1 || echo "   ⚠️  не удалось запустить crond"
else
    echo "   crond уже работает"
fi

"$PROJ/deploy/botctl.sh" start || true

# Автозапуск после перезагрузки телефона (нужно приложение Termux:Boot)
if [ -d "$PREFIX/../boot" ] || [ -d "$CRON_DIR" ]; then
    cat > "$CRON_DIR/run-tg-ref.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
# Запускается Termux:Boot после перезагрузки телефона
export PATH=$PREFIX/bin:/system/bin
termux-wake-lock 2>/dev/null || true
pgrep -x crond >/dev/null 2>&1 || crond
sleep 5
$PROJ/deploy/botctl.sh start >/dev/null 2>&1
EOF
    chmod +x "$CRON_DIR/run-tg-ref.sh"
    echo "   Скрипт автозапуска: $CRON_DIR/run-tg-ref.sh"
fi

echo
echo "Готово. Дальше:"
echo "  1) Заполните config.json (токен, chat_id, каналы) — nano $PROJ/config.json"
echo "  2) Проверьте:  cd $PROJ && python tg_ref.py health"
echo "  3) Первый прогон:  $PROJ/deploy/daily.sh"
echo
echo "Важно для Android:"
echo "  • Отключите оптимизацию батареи для Termux (Настройки → Приложения → Termux → Батарея → Без ограничений)."
echo "  • Разрешите автозапуск (на MIUI/HyperOS есть отдельный тумблер «Автозапуск»)."
echo "  • Установите Termux:Boot, чтобы всё поднималось после перезагрузки телефона."
echo "  • Следите за батареей: Android убьёт процесс, но watchdog поднимет его за 10 минут."
