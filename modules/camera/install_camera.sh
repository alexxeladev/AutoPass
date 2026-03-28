#!/bin/bash
# ============================================================
#  Модуль распознавания номеров — Установка
#  Поддерживается: Ubuntu 20.04 / 22.04 / 24.04
#  Требует: основная система propuska уже установлена
# ============================================================

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "${GREEN}✔${RESET}  $1"; }
info() { echo -e "${CYAN}→${RESET}  $1"; }
warn() { echo -e "${YELLOW}!${RESET}  $1"; }
err()  { echo -e "${RED}✘${RESET}  $1"; exit 1; }
h1()   { echo -e "\n${BOLD}${CYAN}$1${RESET}"; echo "$(printf '─%.0s' {1..50})"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="/home/user"
VENV="$BASE_DIR/.venv"
ENV_FILE="$BASE_DIR/.env"

echo -e "\n${BOLD}📷  Модуль распознавания номеров — Установка${RESET}\n"

# ============================================================
h1 "1. Проверка основной системы"
# ============================================================

[ ! -f "$ENV_FILE" ] && err "Файл .env не найден. Сначала установите основную систему!"
[ ! -d "$VENV" ] && err "Виртуальное окружение не найдено. Сначала установите основную систему!"
[ ! -f "$BASE_DIR/web_app.py" ] && err "web_app.py не найден. Сначала установите основную систему!"

ok "Основная система найдена"

# ============================================================
h1 "2. Системные зависимости"
# ============================================================

info "Установка системных пакетов..."
sudo apt-get update -q

# OpenCV системные зависимости
sudo apt-get install -y -q \
    libglib2.0-0 \
    libgl1-mesa-glx \
    libglib2.0-dev \
    ffmpeg \
    libsm6 \
    libxext6

ok "Системные пакеты установлены"

# ============================================================
h1 "3. Python зависимости"
# ============================================================

info "Проверка и установка Python пакетов..."

packages=(
    "opencv-python-headless==4.9.0.80"
    "nomeroff-net==4.0.1"
    "torch==2.1.2"
    "torchvision==0.16.2"
    "Pillow==10.2.0"
    "numpy==1.26.4"
)

for pkg in "${packages[@]}"; do
    pkg_name=$(echo "$pkg" | cut -d= -f1)
    if "$VENV/bin/pip" show "$pkg_name" &>/dev/null; then
        ok "$pkg_name уже установлен"
    else
        info "Устанавливаю $pkg_name..."
        "$VENV/bin/pip" install -q "$pkg"
        ok "$pkg_name установлен"
    fi
done

# ============================================================
h1 "4. Миграция базы данных"
# ============================================================

info "Загрузка конфигурации БД..."
source "$ENV_FILE"

[ -z "$DATABASE_URL" ] && err "DATABASE_URL не найден в .env"

info "Создание таблиц модуля..."
PGPASSWORD=$(echo "$DATABASE_URL" | sed 's/.*:\(.*\)@.*/\1/') \
psql "$DATABASE_URL" << 'SQL'
-- Камеры / КПП
CREATE TABLE IF NOT EXISTS cameras (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    location    TEXT NOT NULL,
    direction   TEXT NOT NULL CHECK (direction IN ('in', 'out', 'both')),
    rtsp_url    TEXT,
    active      BOOLEAN DEFAULT TRUE,
    kpp_id      INT DEFAULT 1,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- События распознавания
CREATE TABLE IF NOT EXISTS camera_events (
    id                  SERIAL PRIMARY KEY,
    camera_id           INT REFERENCES cameras(id) ON DELETE SET NULL,
    plate_raw           TEXT,
    plate_normalized    TEXT,
    confidence          FLOAT,
    match_type          TEXT CHECK (match_type IN ('resident', 'guest', 'unknown')),
    resident_id         INT REFERENCES residents(id) ON DELETE SET NULL,
    pass_id             INT REFERENCES passes(id) ON DELETE SET NULL,
    barrier_action      TEXT DEFAULT 'none' CHECK (barrier_action IN ('open', 'none')),
    snapshot_path       TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_camera_events_plate ON camera_events(plate_normalized);
CREATE INDEX IF NOT EXISTS idx_camera_events_time  ON camera_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_camera_events_cam   ON camera_events(camera_id);

GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO propuska;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO propuska;
SQL

ok "Таблицы созданы"

# ============================================================
h1 "5. Конфигурация камер"
# ============================================================

CONFIG_FILE="$SCRIPT_DIR/config.json"

if [ ! -f "$CONFIG_FILE" ]; then
    info "Создаю config.json..."
    cat > "$CONFIG_FILE" << 'JSON'
{
    "kpp": [
        {
            "id": 1,
            "name": "КПП Петровское Парк",
            "cameras": [
                {
                    "id": 1,
                    "name": "Въезд",
                    "direction": "in",
                    "rtsp_url": "",
                    "active": false
                },
                {
                    "id": 2,
                    "name": "Выезд",
                    "direction": "out",
                    "rtsp_url": "",
                    "active": false
                }
            ]
        }
    ],
    "recognition": {
        "min_confidence": 0.7,
        "min_digits": 2,
        "min_letters": 1,
        "frame_interval": 0.5,
        "cooldown_seconds": 5
    },
    "snapshots_dir": "/home/user/snapshots",
    "barrier": {
        "enabled": false,
        "type": "relay"
    }
}
JSON
    ok "config.json создан — заполните RTSP URL камер"
else
    ok "config.json уже существует"
fi

mkdir -p /home/user/snapshots

# ============================================================
h1 "6. Копирование файлов модуля"
# ============================================================

info "Копирование шаблона recognition.html..."
cp "$SCRIPT_DIR/templates/recognition.html" "$BASE_DIR/templates/recognition.html"
ok "Шаблон скопирован"

info "Копирование camera_service.py..."
cp "$SCRIPT_DIR/camera_service.py" "$BASE_DIR/camera_service.py"
ok "camera_service.py скопирован"

info "Копирование camera_api.py..."
cp "$SCRIPT_DIR/camera_api.py" "$BASE_DIR/camera_api.py"
ok "camera_api.py скопирован"

# ============================================================
h1 "7. Systemd сервис"
# ============================================================

USER_NAME="$(stat -c '%U' $BASE_DIR/web_app.py)"

sudo tee /etc/systemd/system/propuska-camera.service > /dev/null << UNIT
[Unit]
Description=Propuska Camera Recognition Service
After=network.target postgresql.service propuska-web.service
Requires=postgresql.service

[Service]
User=${USER_NAME}
WorkingDirectory=${BASE_DIR}
EnvironmentFile=${BASE_DIR}/.env
ExecStart=${VENV}/bin/python ${BASE_DIR}/camera_service.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable propuska-camera --quiet
ok "Systemd сервис зарегистрирован"

# ============================================================
h1 "8. Патч web_app.py"
# ============================================================

if grep -q "camera_api" "$BASE_DIR/web_app.py"; then
    ok "web_app.py уже пропатчен"
else
    info "Добавляю маршруты камеры в web_app.py..."
    # Добавляем импорт в конец файла (после всех роутов)
    echo "" >> "$BASE_DIR/web_app.py"
    echo "# Camera module" >> "$BASE_DIR/web_app.py"
    echo "from camera_api import camera_router" >> "$BASE_DIR/web_app.py"
    echo "app.include_router(camera_router)" >> "$BASE_DIR/web_app.py"
    ok "web_app.py пропатчен"
fi

# ============================================================
h1 "Готово!"
# ============================================================

echo ""
echo -e "  ${BOLD}Следующие шаги:${RESET}"
echo ""
echo -e "  ${YELLOW}1.${RESET} Заполните RTSP URL камер:"
echo -e "     ${CYAN}nano $SCRIPT_DIR/config.json${RESET}"
echo ""
echo -e "  ${YELLOW}2.${RESET} Запустите сервис:"
echo -e "     ${CYAN}sudo systemctl start propuska-camera${RESET}"
echo ""
echo -e "  ${YELLOW}3.${RESET} Перезапустите веб-панель:"
echo -e "     ${CYAN}bash $BASE_DIR/start.sh restart${RESET}"
echo ""
echo -e "  ${YELLOW}4.${RESET} Страница распознавания:"
echo -e "     ${CYAN}http://IP:8000/recognition${RESET}"
echo ""
echo -e "  Логи камеры: ${CYAN}journalctl -u propuska-camera -f${RESET}"
echo ""
