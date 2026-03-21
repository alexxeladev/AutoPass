#!/bin/bash
# ============================================================
#  Пропускная система — Установка
#  Поддерживается: Ubuntu 20.04 / 22.04 / 24.04
# ============================================================

set -e  # остановиться при любой ошибке

# ── Цвета ───────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "${GREEN}✔${RESET}  $1"; }
info() { echo -e "${CYAN}→${RESET}  $1"; }
warn() { echo -e "${YELLOW}!${RESET}  $1"; }
err()  { echo -e "${RED}✘${RESET}  $1"; exit 1; }
h1()   { echo -e "\n${BOLD}${CYAN}$1${RESET}"; echo "$(printf '─%.0s' {1..50})"; }

# ── Папка проекта ────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo -e "\n${BOLD}🛡  Пропускная система — Установка${RESET}\n"

# ============================================================
h1 "1. Системные пакеты"
# ============================================================

info "Обновление пакетного менеджера..."
sudo apt-get update -q

info "Установка PostgreSQL..."
sudo apt-get install -y -q postgresql postgresql-contrib

info "Установка Python-инструментов..."
sudo apt-get install -y -q python3 python3-pip python3-venv

ok "Системные пакеты установлены"

# ============================================================
h1 "2. PostgreSQL"
# ============================================================

info "Запуск PostgreSQL..."
sudo systemctl enable postgresql --quiet
sudo systemctl start postgresql

# Генерируем безопасный пароль БД
DB_PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")
DB_NAME="propuska_db"
DB_USER="propuska"

info "Создание БД и пользователя..."
sudo -u postgres psql -q << SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
    CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}';
  ELSE
    ALTER USER ${DB_USER} WITH PASSWORD '${DB_PASS}';
  END IF;
END
\$\$;

SELECT 'CREATE DATABASE ${DB_NAME} OWNER ${DB_USER}'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${DB_NAME}')\gexec

GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};
SQL

info "Применение схемы БД..."
sudo -u postgres psql -d "$DB_NAME" -q << SQL
GRANT ALL ON SCHEMA public TO ${DB_USER};
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO ${DB_USER};
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO ${DB_USER};
SQL

# Применяем schema.sql через суперпользователя postgres (избегаем peer auth)
sudo -u postgres psql -d "$DB_NAME" -q -f /tmp/schema.sql

DATABASE_URL="postgresql://${DB_USER}:${DB_PASS}@127.0.0.1:5432/${DB_NAME}"

# ── Разрешаем подключение по паролю (md5) для нашего пользователя ──
# По умолчанию Ubuntu использует peer-аутентификацию для локальных
# соединений — это ломает подключение через Python. Добавляем правило
# md5 для пользователя propuska перед дефолтными строками.

PG_VERSION=$(sudo -u postgres psql -tAc "SHOW server_version_num;" | cut -c1-2)
PG_HBA=$(sudo -u postgres psql -tAc "SHOW hba_file;")

info "Настройка pg_hba.conf ($PG_HBA)..."

# Добавляем строку только если её ещё нет
if ! sudo grep -q "propuska" "$PG_HBA" 2>/dev/null; then
    # Вставляем в начало файла (до других правил), чтобы сработало первым
    sudo sed -i "1s|^|# propuska app — password auth\nhost    ${DB_NAME}    ${DB_USER}    127.0.0.1/32    md5\nhost    ${DB_NAME}    ${DB_USER}    ::1/128         md5\nlocal   ${DB_NAME}    ${DB_USER}                    md5\n\n|" "$PG_HBA"
fi

info "Перезагрузка конфигурации PostgreSQL..."
sudo systemctl reload postgresql

# Проверяем что подключение теперь работает
sleep 1
if PGPASSWORD="$DB_PASS" psql -U "$DB_USER" -d "$DB_NAME" -h 127.0.0.1 -c "SELECT 1;" -q > /dev/null 2>&1; then
    ok "Подключение к БД работает"
else
    warn "Не удалось проверить подключение к БД — проверьте вручную после установки"
fi

ok "PostgreSQL настроен. БД: $DB_NAME"

# ============================================================
h1 "3. Python-окружение"
# ============================================================

info "Создание виртуального окружения (.venv)..."
python3 -m venv .venv

info "Установка зависимостей..."
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

ok "Python-окружение готово"

# ============================================================
h1 "4. Файл настроек (.env)"
# ============================================================

# Генерируем JWT-секрет
JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

if [ -f ".env" ]; then
    warn ".env уже существует, создаю .env.new (ваши токены сохранены)"
    ENV_FILE=".env.new"
else
    ENV_FILE=".env"
fi

cat > "$ENV_FILE" << EOF
# ── База данных ────────────────────────────────────────────
DATABASE_URL=${DATABASE_URL}

# ── JWT (не меняйте после первого запуска!) ────────────────
JWT_SECRET_KEY=${JWT_SECRET}

# ── Бот жильцов ───────────────────────────────────────────
# Получите токен у @BotFather в Telegram
BOT_TOKEN=ВСТАВЬТЕ_ТОКЕН_БОТА_ЖИЛЬЦОВ

# ── Чат охраны ────────────────────────────────────────────
# ID группы/канала куда бот шлёт новые заявки
# Как узнать: добавьте @userinfobot в группу и отправьте /start
SECURITY_CHAT_ID=ВСТАВЬТЕ_ID_ЧАТА_ОХРАНЫ

# ── Бот охранника ─────────────────────────────────────────
# Отдельный токен от @BotFather
GUARD_BOT_TOKEN=ВСТАВЬТЕ_ТОКЕН_БОТА_ОХРАНЫ
EOF

ok "Файл $ENV_FILE создан"

# ============================================================
h1 "5. Systemd-сервисы (автозапуск)"
# ============================================================

APP_DIR="$SCRIPT_DIR"
VENV="$APP_DIR/.venv"
USER_NAME="$(whoami)"

# Веб-панель
sudo tee /etc/systemd/system/propuska-web.service > /dev/null << EOF
[Unit]
Description=Propuska Web Panel
After=network.target postgresql.service
Requires=postgresql.service

[Service]
User=${USER_NAME}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${VENV}/bin/uvicorn web_app:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Бот жильцов
sudo tee /etc/systemd/system/propuska-bot.service > /dev/null << EOF
[Unit]
Description=Propuska Residents Bot
After=network.target postgresql.service
Requires=postgresql.service

[Service]
User=${USER_NAME}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${VENV}/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Бот охранника
sudo tee /etc/systemd/system/propuska-guard.service > /dev/null << EOF
[Unit]
Description=Propuska Guard Bot
After=network.target postgresql.service
Requires=postgresql.service

[Service]
User=${USER_NAME}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${VENV}/bin/python guard_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable propuska-web propuska-bot propuska-guard --quiet

ok "Systemd-сервисы зарегистрированы"

# ============================================================
h1 "Установка завершена!"
# ============================================================

echo ""
echo -e "${BOLD}Что дальше:${RESET}"
echo ""
echo -e "  ${YELLOW}1.${RESET} Откройте файл ${BOLD}.env${RESET} и вставьте токены:"
echo -e "     ${CYAN}nano .env${RESET}"
echo ""
echo -e "  ${YELLOW}2.${RESET} После заполнения .env запустите сервисы:"
echo -e "     ${CYAN}bash start.sh${RESET}"
echo ""
echo -e "  ${YELLOW}3.${RESET} Откройте панель охраны в браузере:"
echo -e "     ${CYAN}http://localhost:8000${RESET}"
echo -e "     Логин: ${BOLD}admin${RESET}  Пароль: ${BOLD}admin123${RESET}"
echo -e "     ${RED}Сразу смените пароль после входа!${RESET}"
echo ""
echo -e "${BOLD}Где взять токены Telegram:${RESET}"
echo "  • Откройте @BotFather → /newbot → скопируйте токен"
echo "  • Для SECURITY_CHAT_ID: добавьте бота в группу охраны,"
echo "    напишите что-нибудь, откройте:"
echo "    https://api.telegram.org/bot<TOKEN>/getUpdates"
echo "    → найдите \"chat\":{\"id\": <ЧИСЛО>}"
echo ""
