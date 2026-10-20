#!/usr/bin/env bash
#
# Tg-skaner — установка на VPS (Debian/Ubuntu) с автозапуском через systemd.
#
# Использование (на свежем сервере):
#   apt update && apt install -y git
#   git clone -b claude/telegram-deleted-messages-bot-ovwzus --single-branch \
#     https://github.com/gytfel/Tg-skaner.git
#   cd Tg-skaner
#   bash install.sh      # не под root — добавьте sudo
#
set -euo pipefail

APP_NAME="tg-skaner"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
PYTHON="${PYTHON:-python3}"
VENV="$APP_DIR/.venv"

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
err() { printf '\033[1;31mОшибка:\033[0m %s\n' "$*" >&2; }

# Выполнить команду от имени RUN_USER. Если мы уже этот пользователь (напр. root
# запустил скрипт напрямую) — не вызываем sudo: на минимальном Debian его может
# не быть вовсе.
run_as_user() {
  if [ "$RUN_USER" = "$(id -un)" ]; then
    "$@"
  else
    sudo -u "$RUN_USER" --set-home "$@"
  fi
}

# 1. Нужны root-права (apt + systemd).
if [ "$(id -u)" -ne 0 ]; then
  err "Запустите через sudo: sudo bash install.sh"
  exit 1
fi

# 2. Нужен интерактивный терминал — для ввода номера/кода Telegram.
if [ ! -t 0 ]; then
  err "Запустите в интерактивном терминале (нужен ввод телефона и кода из Telegram)."
  exit 1
fi

# 3. Системные пакеты.
say "Устанавливаю системные пакеты (python3, venv, pip)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip

# 4. Виртуальное окружение и зависимости (от имени пользователя, не root).
say "Создаю виртуальное окружение в $VENV ..."
run_as_user "$PYTHON" -m venv "$VENV"
run_as_user "$VENV/bin/pip" install --quiet --upgrade pip setuptools wheel
run_as_user "$VENV/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# 5. Файл настроек env/.env.
ENV_FILE="$APP_DIR/env/.env"
if [ ! -f "$ENV_FILE" ]; then
  say "Настройка env/.env"
  echo "   API_ID и API_HASH — с https://my.telegram.org (API development tools)"
  echo "   BOT_TOKEN — у @BotFather командой /newbot"
  read -rp "   API_ID: " API_ID
  read -rsp "   API_HASH: " API_HASH; echo
  read -rsp "   BOT_TOKEN: " BOT_TOKEN; echo
  mkdir -p "$APP_DIR/env"
  umask 077
  cat > "$ENV_FILE" <<EOF
API_ID=$API_ID
API_HASH=$API_HASH
BOT_TOKEN=$BOT_TOKEN
SESSION_NAME=tg_skaner
SAVE_MEDIA=true
RETENTION_DAYS=7
IGNORE_OUTGOING=false
EOF
  chown "$RUN_USER":"$RUN_USER" "$APP_DIR/env" "$ENV_FILE"
  chmod 700 "$APP_DIR/env"
  chmod 600 "$ENV_FILE"
else
  say "env/.env уже существует — пропускаю."
fi

# 6. Вход в Telegram. login.py сам пропустит шаг, если сессия уже авторизована,
# и выполнит интерактивный вход, если нет (в т.ч. при пустом/битом .session).
say "Проверяю вход в Telegram (если нужно — введите номер, код и 2FA-пароль):"
run_as_user bash -c "cd '$APP_DIR' && '$VENV/bin/python' login.py"

# 7. systemd-сервис.
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
ExecStart="$VENV/bin/python" "$APP_DIR/bot.py"
Restart=always
RestartSec=10
RestartPreventExitStatus=69
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$APP_NAME.service"
systemctl restart "$APP_NAME.service"   # на случай повторного запуска установщика

say "Готово! Сервис запущен и добавлен в автозагрузку."
echo
echo "   Статус:  systemctl status $APP_NAME"
echo "   Логи:    journalctl -u $APP_NAME -f"
echo "   Рестарт: systemctl restart $APP_NAME"
echo "   Стоп:    systemctl stop $APP_NAME"
echo

# Подсказка про Start — только если доставка идёт через бота.
if grep -Eq '^BOT_TOKEN=.+' "$ENV_FILE"; then
  echo "Откройте вашего бота в Telegram и нажмите Start, чтобы получать отчёты."
fi
