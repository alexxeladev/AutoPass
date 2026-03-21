#!/bin/bash
# ============================================================
#  Пропускная система — Управление
#  Использование:
#    bash start.sh          # запустить всё
#    bash start.sh stop     # остановить всё
#    bash start.sh restart  # перезапустить всё
#    bash start.sh status   # статус сервисов
#    bash start.sh logs     # логи в реальном времени
# ============================================================

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SERVICES=("propuska-web" "propuska-bot" "propuska-guard")
SERVICE_LABELS=("Веб-панель    (порт 8000)" "Бот жильцов" "Бот охранника")

# ── Проверка .env ────────────────────────────────────────────
check_env() {
    if [ ! -f ".env" ]; then
        echo -e "${RED}✘  Файл .env не найден!${RESET}"
        echo -e "   Запустите сначала: ${CYAN}bash install.sh${RESET}"
        exit 1
    fi

    local missing=0
    for key in BOT_TOKEN SECURITY_CHAT_ID GUARD_BOT_TOKEN; do
        val=$(grep "^${key}=" .env | cut -d= -f2)
        if [[ "$val" == *"ВСТАВЬТЕ"* ]] || [ -z "$val" ]; then
            echo -e "${YELLOW}!  Не заполнен: ${BOLD}${key}${RESET}"
            missing=1
        fi
    done

    if [ $missing -eq 1 ]; then
        echo ""
        echo -e "Откройте ${BOLD}.env${RESET} и вставьте токены:"
        echo -e "  ${CYAN}nano .env${RESET}"
        echo ""
        echo -e "${YELLOW}Запустить только веб-панель без ботов? [y/N]${RESET} \c"
        read -r ans
        [ "$ans" != "y" ] && [ "$ans" != "Y" ] && exit 1
        SERVICES=("propuska-web")
        SERVICE_LABELS=("Веб-панель    (порт 8000)")
    fi
}

# ── Статус одного сервиса ────────────────────────────────────
svc_status() {
    local svc="$1"
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        echo -e "${GREEN}● работает${RESET}"
    elif systemctl is-failed --quiet "$svc" 2>/dev/null; then
        echo -e "${RED}✘ ошибка${RESET}"
    else
        echo -e "${YELLOW}○ остановлен${RESET}"
    fi
}

# ── Команды ─────────────────────────────────────────────────
CMD="${1:-start}"

case "$CMD" in

# ── START ────────────────────────────────────────────────────
start)
    echo -e "\n${BOLD}🛡  Пропускная система — Запуск${RESET}\n"
    check_env

    for i in "${!SERVICES[@]}"; do
        svc="${SERVICES[$i]}"
        label="${SERVICE_LABELS[$i]}"
        echo -e "${CYAN}→${RESET}  Запуск: ${label}..."
        sudo systemctl start "$svc"
        sleep 1
        status=$(svc_status "$svc")
        echo -e "   Статус: $status"
    done

    echo ""
    echo -e "${BOLD}Готово!${RESET}"
    LOCAL_IP=$(hostname -I | awk '{print $1}')
    PROTOCOL="http"
    [ -f "ssl.crt" ] && PROTOCOL="https"
    echo -e "Веб-панель: ${CYAN}${PROTOCOL}://${LOCAL_IP}:8000${RESET}"
    echo -e "Логин:      ${BOLD}admin${RESET} / ${BOLD}admin123${RESET} ${RED}(смените пароль!)${RESET}"
    echo ""
    echo -e "Управление:"
    echo -e "  ${CYAN}bash start.sh stop${RESET}        — остановить"
    echo -e "  ${CYAN}bash start.sh restart${RESET}     — перезапустить"
    echo -e "  ${CYAN}bash start.sh status${RESET}      — статус"
    echo -e "  ${CYAN}bash start.sh logs web${RESET}    — логи панели"
    echo -e "  ${CYAN}bash start.sh logs bot${RESET}    — логи бота жильцов"
    echo -e "  ${CYAN}bash start.sh logs guard${RESET}  — логи охраны"
    ;;

# ── STOP ─────────────────────────────────────────────────────
stop)
    echo -e "\n${BOLD}Остановка сервисов...${RESET}"
    for svc in "${SERVICES[@]}"; do
        sudo systemctl stop "$svc" 2>/dev/null || true
        echo -e "${YELLOW}○${RESET}  $svc остановлен"
    done
    ;;

# ── RESTART ──────────────────────────────────────────────────
restart)
    echo -e "\n${BOLD}Перезапуск сервисов...${RESET}"
    check_env
        # Сбрасываем Telegram сессии перед перезапуском ботов
    echo -e "${CYAN}→${RESET}  Сброс Telegram сессий..."
    BOT_TOKEN=$(grep BOT_TOKEN .env | grep -v GUARD | cut -d= -f2 | tr -d " \r")
    GUARD_BOT_TOKEN=$(grep GUARD_BOT_TOKEN .env | cut -d= -f2 | tr -d " \r")
    if [ -n "$BOT_TOKEN" ]; then
        curl -s "https://api.telegram.org/bot${BOT_TOKEN}/deleteWebhook?drop_pending_updates=true" > /dev/null
    fi
    if [ -n "$GUARD_BOT_TOKEN" ]; then
        curl -s "https://api.telegram.org/bot${GUARD_BOT_TOKEN}/deleteWebhook?drop_pending_updates=true" > /dev/null
    fi
    sleep 3
    for i in "${!SERVICES[@]}"; do
        svc="${SERVICES[$i]}"
        label="${SERVICE_LABELS[$i]}"
        sudo systemctl restart "$svc"
        sleep 1
        status=$(svc_status "$svc")
        echo -e "${CYAN}↺${RESET}  ${label}: $status"
    done
    ;;

# ── STATUS ───────────────────────────────────────────────────
status)
    echo -e "\n${BOLD}Статус сервисов:${RESET}\n"
    for i in "${!SERVICES[@]}"; do
        svc="${SERVICES[$i]}"
        label="${SERVICE_LABELS[$i]}"
        status=$(svc_status "$svc")
        printf "  %-30s %s\n" "$label" "$status"
    done

    echo ""
    # PostgreSQL
    if systemctl is-active --quiet postgresql; then
        echo -e "  PostgreSQL                     ${GREEN}● работает${RESET}"
    else
        echo -e "  PostgreSQL                     ${RED}✘ не запущен${RESET}"
    fi
    echo ""

    # Порт 8000
    if ss -tlnp 2>/dev/null | grep -q ':8000'; then
        echo -e "  Порт 8000 (веб)                ${GREEN}● слушает${RESET}"
    else
        echo -e "  Порт 8000 (веб)                ${YELLOW}○ не занят${RESET}"
    fi
    echo ""
    ;;

# ── LOGS ─────────────────────────────────────────────────────
logs)
    TARGET="${2:-web}"
    case "$TARGET" in
        web)   SVC="propuska-web"   ;;
        bot)   SVC="propuska-bot"   ;;
        guard) SVC="propuska-guard" ;;
        *)
            echo "Использование: bash start.sh logs [web|bot|guard]"
            exit 1
            ;;
    esac
    echo -e "${CYAN}Логи $SVC (Ctrl+C для выхода):${RESET}\n"
    sudo journalctl -u "$SVC" -f --no-pager
    ;;

# ── HELP ─────────────────────────────────────────────────────
*)
    echo ""
    echo -e "${BOLD}Использование:${RESET}"
    echo "  bash start.sh              — запустить всё"
    echo "  bash start.sh stop         — остановить всё"
    echo "  bash start.sh restart      — перезапустить"
    echo "  bash start.sh status       — статус сервисов"
    echo "  bash start.sh logs web     — логи веб-панели"
    echo "  bash start.sh logs bot     — логи бота жильцов"
    echo "  bash start.sh logs guard   — логи бота охраны"
    echo ""
    ;;
esac
