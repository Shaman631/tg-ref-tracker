#!/usr/bin/env bash
# Настройка deploy key: хостинг тянет проект с GitHub по SSH.
#
#   ./deploy/setup-git.sh                 # установить в ~/tg-ref-tracker
#   ./deploy/setup-git.sh /home/USER/tg   # установить в свой каталог
#
# Скрипт идемпотентен: ключ создаётся один раз, при повторном запуске
# используется уже существующий.
set -u

REPO_URL="git@github.com:Shaman631/tg-ref-tracker.git"
REPO_WEB="https://github.com/Shaman631/tg-ref-tracker"
KEY_NAME="tg_ref_deploy"
SSH_DIR="$HOME/.ssh"
KEY_PATH="$SSH_DIR/$KEY_NAME"

TARGET_DIR="${1:-$HOME/tg-ref-tracker}"

echo "=== Настройка deploy key для tg-ref-tracker ==="

command -v git >/dev/null 2>&1 || { echo "❌ git не найден"; exit 1; }
command -v ssh-keygen >/dev/null 2>&1 || { echo "❌ ssh-keygen не найден"; exit 1; }
command -v ssh-keyscan >/dev/null 2>&1 || { echo "⚠️  ssh-keyscan не найден, known_hosts не обновится"; }

mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"

if [ -f "$KEY_PATH" ]; then
    echo "Ключ уже существует: $KEY_PATH"
else
    ssh-keygen -t ed25519 -f "$KEY_PATH" -N "" -C "tg-ref-tracker-deploy" >/dev/null 2>&1 \
        || { echo "❌ не удалось создать ключ"; exit 1; }
    chmod 600 "$KEY_PATH"
    echo "✅ Создан ключ: $KEY_PATH"
fi

if ! grep -q "github.com" "$SSH_DIR/known_hosts" 2>/dev/null; then
    if command -v ssh-keyscan >/dev/null 2>&1; then
        ssh-keyscan -t rsa,ecdsa,ed25519 github.com >> "$SSH_DIR/known_hosts" 2>/dev/null
        chmod 600 "$SSH_DIR/known_hosts" 2>/dev/null
        echo "✅ known_hosts обновлён"
    fi
fi

SSH_TEST="ssh -T -i $KEY_PATH -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes git@github.com"
AUTH_OUT="$($SSH_TEST 2>&1 || true)"

if ! echo "$AUTH_OUT" | grep -q "successfully authenticated"; then
    echo
    echo "Шаг 1. Скопируйте публичный ключ ниже и добавьте его в GitHub:"
    echo "        $REPO_WEB/settings/keys/new"
    echo "        Title: tg-ref-tracker deploy"
    echo "        Key:   вставьте строку целиком"
    echo "        Право 'Allow write access' НЕ нужно — достаточно чтения."
    echo
    sed 's/^/    /' "$KEY_PATH.pub"
    echo
    echo "Шаг 2. После добавления ключа повторите:"
    echo "        $0 $TARGET_DIR"
    echo
    exit 2
fi

echo "✅ GitHub принимает ключ"

if [ -d "$TARGET_DIR/.git" ]; then
    echo "Обновляю проект в $TARGET_DIR"
    cd "$TARGET_DIR" || exit 1
    git pull --ff-only || { echo "❌ не удалось обновить проект"; exit 1; }
else
    echo "Клонирую проект в $TARGET_DIR"
    GIT_SSH_COMMAND="ssh -i $KEY_PATH -o IdentitiesOnly=yes" \
        git clone "$REPO_URL" "$TARGET_DIR" || { echo "❌ клонирование не удалось"; exit 1; }
    cd "$TARGET_DIR" || exit 1
    # чтобы последующие git pull работали с тем же ключом
    git config core.sshCommand "ssh -i $KEY_PATH -o IdentitiesOnly=yes"
fi

if [ ! -f config.json ]; then
    cp config.example.json config.json
    chmod 600 config.json
    echo "✅ Создан config.json из шаблона (заполните токен и каналы)"
else
    echo "config.json уже есть — не трогаю"
fi

chmod +x deploy/*.sh 2>/dev/null
mkdir -p logs 2>/dev/null

echo
echo "Готово. Проект в: $TARGET_DIR"
echo "Дальше:"
echo "  cd $TARGET_DIR"
echo "  python3 tg_ref.py config set token <ТОКЕН>"
echo "  python3 tg_ref.py channels add @ваш_канал"
echo "  python3 tg_ref.py config set chat <ВАШ_CHAT_ID>"
echo "  ./deploy/check.sh"
echo "  ./deploy/botctl.sh start"
