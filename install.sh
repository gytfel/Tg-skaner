#!/usr/bin/env bash
#
# Tg-skaner — установка на VPS (Debian/Ubuntu) с автозапуском через systemd.
#
# Использование (на свежем сервере):
#   sudo apt update && sudo apt install -y git
#   git clone <URL-репозитория> tg-skaner
#   cd tg-skaner
#   sudo bash install.sh
#
set -euo pipefail

APP_NAME="tg-skaner"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
PYTHON="${PYTHON:-python3}"
VENV="$APP_DIR/.venv"

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
err() { printf '\033[1;31mОшибка:\033[0m %s\n' "$*" >&2; }

run_as_user() { sudo -u "$RUN_USER" --set-home "$@"; }

# 1. Нужны root-права (apt + systemd).
if [ "$(id -u)" -ne 0 ]; then
  err "Запустите через sudo: sudo bash install.sh"
  exit 1
fi

# 2. Системные пакеты.
say "Устанавливаю системные пакеты (python3, venv, pip)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip

# 3. Виртуальное окружение и зависимости (от имени пользователя, не root).
say "Создаю виртуальное окружение в $VENV ..."
run_as_user "$PYTHON" -m venv "$VENV"
run_as_user "$VENV/bin/pip" install --quiet --upgrade pip setuptools wheel
run_as_user "$VENV/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# 4. Файл настроек .env.
if [ ! -f "$APP_DIR/.env" ]; then
  say "Настройка .env"
  echo "   API_ID и API_HASH — с https://my.telegram.org (API development tools)"
  echo "   BOT_TOKEN — у @BotFather командой /newbot"
  read -rp "   API_ID: " API_ID
  read -rp "   API_HASH: " API_HASH
  read -rp "   BOT_TOKEN: " BOT_TOKEN
  umask 077
  cat > "$APP_DIR/.env" <<EOF
API_ID=$API_ID
API_HASH=$API_HASH
BOT_TOKEN=$BOT_TOKEN
SESSION_NAME=tg_skaner
SAVE_MEDIA=true
RETENTION_DAYS=7
IGNORE_OUTGOING=false
EOF
  chown "$RUN_USER":"$RUN_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
else
  say ".env уже существует — пропускаю."
fi

# 5. Одноразовый интерактивный вход (создаёт *.session).
if ! ls "$APP_DIR"/*.session >/dev/null 2>&1; then
  say "Первый вход в Telegram — введите номер телефона, код и (если есть) 2FA-пароль:"
  run_as_user bash -c "cd '$APP_DIR' && '$VENV/bin/python' login.py"
else
  say "Файл сессии уже есть — вход пропускаю."
fi

# 6. systemd-сервис.
say "Создаю и запускаю systemd-сервис '$APP_NAME'..."
cat > "/etc/systemd/system/$APP_NAME.service" <<EOF
[Unit]
Description=Tg-skaner (archiver of deleted Telegram messages)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$VENV/bin/python $APP_DIR/bot.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$APP_NAME.service"

say "Готово! Сервис запущен и добавлен в автозагрузку."
echo
echo "   Статус:  systemctl status $APP_NAME"
echo "   Логи:    journalctl -u $APP_NAME -f"
echo "   Рестарт: systemctl restart $APP_NAME"
echo "   Стоп:    systemctl stop $APP_NAME"
echo
echo "Откройте вашего бота в Telegram и нажмите Start, чтобы получать отчёты."
