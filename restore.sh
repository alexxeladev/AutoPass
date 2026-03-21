#!/bin/bash
# ═══════════════════════════════════════════════════
#  Восстановление — КП Петровское Парк
# ═══════════════════════════════════════════════════

APP_DIR="/home/user"
BACKUP_DIR="/home/user/backups"

echo "═══════════════════════════════════════════════════"
echo "  ВОССТАНОВЛЕНИЕ ИЗ РЕЗЕРВНОЙ КОПИИ"
echo "═══════════════════════════════════════════════════"

# Список доступных бэкапов
BACKUPS=($(ls -t ${BACKUP_DIR}/propuska_backup_*.tar.gz 2>/dev/null))

if [ ${#BACKUPS[@]} -eq 0 ]; then
    echo "❌ Резервные копии не найдены в ${BACKUP_DIR}"
    exit 1
fi

echo ""
echo "Доступные резервные копии:"
for i in "${!BACKUPS[@]}"; do
    DATE_STR=$(basename "${BACKUPS[$i]}" | sed 's/propuska_backup_//;s/.tar.gz//' | sed 's/_/ /')
    SIZE=$(du -sh "${BACKUPS[$i]}" | cut -f1)
    echo "  $((i+1)). $(basename ${BACKUPS[$i]})  [${SIZE}]"
done

echo ""
read -p "Выберите номер бэкапа (1-${#BACKUPS[@]}): " CHOICE

if ! [[ "$CHOICE" =~ ^[0-9]+$ ]] || [ "$CHOICE" -lt 1 ] || [ "$CHOICE" -gt ${#BACKUPS[@]} ]; then
    echo "❌ Неверный выбор"
    exit 1
fi

SELECTED="${BACKUPS[$((CHOICE-1))]}"
BACKUP_NAME=$(basename "$SELECTED" .tar.gz)

echo ""
echo "⚠️  Будет восстановлено из: $(basename $SELECTED)"
echo "⚠️  Текущие данные будут ПЕРЕЗАПИСАНЫ!"
read -p "Продолжить? (yes/no): " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
    echo "Отменено."
    exit 0
fi

echo ""

# 1. Останавливаем сервисы
echo "→ Останавливаем сервисы..."
systemctl stop propuska-web propuska-bot propuska-guard 2>/dev/null
echo "✔ Сервисы остановлены"

# 2. Распаковываем архив
echo "→ Распаковка архива..."
TEMP_DIR="/tmp/propuska_restore_$$"
mkdir -p "$TEMP_DIR"
tar -xzf "$SELECTED" -C "$TEMP_DIR"
RESTORE_PATH="${TEMP_DIR}/${BACKUP_NAME}"
echo "✔ Архив распакован"

# 3. Восстанавливаем БД
echo "→ Восстановление базы данных..."
sudo -u postgres psql -q << SQL
DROP DATABASE IF EXISTS propuska_db;
CREATE DATABASE propuska_db OWNER propuska;
SQL
sudo -u postgres psql -d propuska_db -q -f "${RESTORE_PATH}/propuska_db.sql"
sudo -u postgres psql -d propuska_db -q -c "GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO propuska; GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO propuska;"
echo "✔ База данных восстановлена"

# 4. Восстанавливаем файлы приложения
echo "→ Восстановление файлов..."
cp ${RESTORE_PATH}/*.py      "${APP_DIR}/" 2>/dev/null
cp ${RESTORE_PATH}/.env      "${APP_DIR}/" 2>/dev/null
cp ${RESTORE_PATH}/ssl.key   "${APP_DIR}/" 2>/dev/null
cp ${RESTORE_PATH}/ssl.crt   "${APP_DIR}/" 2>/dev/null
cp -r ${RESTORE_PATH}/templates "${APP_DIR}/" 2>/dev/null
echo "✔ Файлы восстановлены"

# 5. Восстанавливаем сервисы
echo "→ Восстановление сервисов..."
cp ${RESTORE_PATH}/systemd/propuska-*.service /etc/systemd/system/ 2>/dev/null
systemctl daemon-reload
echo "✔ Сервисы восстановлены"

# 6. Запускаем
echo "→ Запуск сервисов..."
systemctl start propuska-web propuska-bot propuska-guard
sleep 3

# 7. Чистим временные файлы
rm -rf "$TEMP_DIR"

echo ""
bash ${APP_DIR}/start.sh status
echo ""
echo "✅ Восстановление завершено!"
echo "═══════════════════════════════════════════════════"
