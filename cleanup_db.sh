#!/bin/bash
# ============================================================
#  Очистка тестовых данных из БД
# ============================================================

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

cd "$(dirname "${BASH_SOURCE[0]}")"

# Загружаем DATABASE_URL из .env
export $(grep -v '^#' .env | grep DATABASE_URL)

echo -e "\n${BOLD}🗑  Очистка тестовых данных${RESET}\n"

# Показываем что есть
echo -e "${CYAN}Текущее состояние БД:${RESET}"
PGPASSWORD=$(echo $DATABASE_URL | sed 's/.*:\(.*\)@.*/\1/') \
psql "$DATABASE_URL" -t << SQL
SELECT '  Пропусков:   ' || COUNT(*) FROM passes;
SELECT '  Жильцов:     ' || COUNT(*) FROM residents;
SELECT '  Автомобилей: ' || COUNT(*) FROM cars;
SELECT '  Кодов верификации: ' || COUNT(*) FROM verification_codes;
SQL

echo ""
echo -e "${YELLOW}Что очистить?${RESET}"
echo "  1) Только пропуска (заявки)"
echo "  2) Пропуска + жильцы + автомобили (всё кроме пользователей панели)"
echo "  3) Всё полностью (включая пользователей панели)"
echo "  0) Отмена"
echo ""
read -rp "Введите номер: " choice

case $choice in
1)
    echo -e "\n${YELLOW}Удалить все пропуска? [y/N]${RESET} \c"
    read -r confirm
    [ "$confirm" != "y" ] && [ "$confirm" != "Y" ] && echo "Отменено." && exit 0
    psql "$DATABASE_URL" -c "DELETE FROM passes;"
    echo -e "${GREEN}✔ Пропуска удалены${RESET}"
    ;;
2)
    echo -e "\n${RED}Удалить пропуска, жильцов и автомобили? [y/N]${RESET} \c"
    read -r confirm
    [ "$confirm" != "y" ] && [ "$confirm" != "Y" ] && echo "Отменено." && exit 0
    psql "$DATABASE_URL" << SQL
DELETE FROM passes;
DELETE FROM verification_codes;
DELETE FROM cars;
DELETE FROM residents;
SQL
    echo -e "${GREEN}✔ Пропуска, жильцы и автомобили удалены${RESET}"
    ;;
3)
    echo -e "\n${RED}⚠  Удалить ВСЕ данные включая пользователей панели? [y/N]${RESET} \c"
    read -r confirm
    [ "$confirm" != "y" ] && [ "$confirm" != "Y" ] && echo "Отменено." && exit 0
    psql "$DATABASE_URL" << SQL
DELETE FROM passes;
DELETE FROM verification_codes;
DELETE FROM cars;
DELETE FROM residents;
DELETE FROM users;
SQL
    echo -e "${GREEN}✔ Все данные удалены${RESET}"
    echo -e "${YELLOW}!  Пользователь admin пересоздастся при следующем запуске веб-панели${RESET}"
    ;;
0|*)
    echo "Отменено."
    exit 0
    ;;
esac

echo ""
echo -e "${CYAN}Состояние после очистки:${RESET}"
psql "$DATABASE_URL" -t << SQL
SELECT '  Пропусков:   ' || COUNT(*) FROM passes;
SELECT '  Жильцов:     ' || COUNT(*) FROM residents;
SQL
echo ""
