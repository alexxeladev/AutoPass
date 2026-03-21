#!/bin/bash
# ============================================================
#  Пропускная система — Развёртывание с GitHub
#  Использование:
#    bash deploy.sh
# ============================================================

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "${GREEN}✔${RESET}  $1"; }
info() { echo -e "${CYAN}→${RESET}  $1"; }
warn() { echo -e "${YELLOW}!${RESET}  $1"; }
err()  { echo -e "${RED}✘${RESET}  $1"; exit 1; }
h1()   { echo -e "\n${BOLD}${CYAN}$1${RESET}"; echo "$(printf '─%.0s' {1..50})"; }

echo -e "\n${BOLD}🛡  Пропускная система — Установка${RESET}\n"

# ============================================================
h1 "1. Скачивание кода с GitHub"
# ============================================================

REPO_URL="https://github.com/alexxeladev/AutoPass.git"
INSTALL_DIR="/opt/autopass"

if [ -d "$INSTALL_DIR/.git" ]; then
    info "Репозиторий уже есть, обновляю..."
    cd "$INSTALL_DIR"
    git pull origin main
    ok "Код обновлён"
else
    info "Клонирую репозиторий..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    ok "Код скачан в $INSTALL_DIR"
fi

# ============================================================
h1 "2. Токены Telegram"
# ============================================================

echo -e "Сейчас введи токены. Они сохранятся в файл ${BOLD}.env${RESET} на этом сервере"
echo -e "и ${RED}никогда${RESET} не попадут в git.\n"

read -p "$(echo -e ${CYAN}BOT_TOKEN${RESET}' (бот жильцов, от @BotFather): ')" BOT_TOKEN </dev/tty
[ -z "$BOT_TOKEN" ] && err "BOT_TOKEN не может быть пустым"

read -p "$(echo -e ${CYAN}GUARD_BOT_TOKEN${RESET}' (бот охраны, от @BotFather): ')" GUARD_BOT_TOKEN </dev/tty
[ -z "$GUARD_BOT_TOKEN" ] && err "GUARD_BOT_TOKEN не может быть пустым"

read -p "$(echo -e ${CYAN}SECURITY_CHAT_ID${RESET}' (Telegram ID охранника, от @userinfobot): ')" SECURITY_CHAT_ID </dev/tty
[ -z "$SECURITY_CHAT_ID" ] && err "SECURITY_CHAT_ID не может быть пустым"

ok "Токены получены"

# ============================================================
h1 "3. Установка системы"
# ============================================================

info "Запускаю install.sh..."
    cp "$INSTALL_DIR/schema.sql" /tmp/schema.sql
    chmod 644 /tmp/schema.sql
    cd "$INSTALL_DIR"
    bash install.sh

# ============================================================
h1 "4. Сохранение токенов в .env"
# ============================================================

# Если install.sh создал .env.new — используем его, иначе .env
ENV_FILE=".env"
[ -f ".env.new" ] && ENV_FILE=".env.new" && mv .env.new .env

sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=${BOT_TOKEN}|" .env
sed -i "s|^GUARD_BOT_TOKEN=.*|GUARD_BOT_TOKEN=${GUARD_BOT_TOKEN}|" .env
sed -i "s|^SECURITY_CHAT_ID=.*|SECURITY_CHAT_ID=${SECURITY_CHAT_ID}|" .env

ok "Токены сохранены в .env"

# ============================================================
h1 "5. Запуск"
# ============================================================

info "Запускаю сервисы..."
bash start.sh

# ============================================================
h1 "Готово!"
# ============================================================

