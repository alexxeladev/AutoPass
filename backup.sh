#!/bin/bash
# ═══════════════════════════════════════════════════
#  Резервное копирование — КП Петровское Парк
# ═══════════════════════════════════════════════════

APP_DIR="/home/user"
BACKUP_DIR="/home/user/backups"
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_NAME="propuska_backup_${DATE}"
BACKUP_PATH="${BACKUP_DIR}/${BACKUP_NAME}"

mkdir -p "$BACKUP_PATH"

echo "═══════════════════════════════════════════════════"
echo "  РЕЗЕРВНОЕ КОПИРОВАНИЕ — $(date '+%d.%m.%Y %H:%M')"
echo "═══════════════════════════════════════════════════"

# 1. База данных
echo "→ Дамп базы данных..."
sudo -u postgres pg_dump propuska_db > "${BACKUP_PATH}/propuska_db.sql"
echo "✔ БД сохранена ($(du -sh ${BACKUP_PATH}/propuska_db.sql | cut -f1))"

# 2. Файлы приложения
echo "→ Файлы приложения..."
cp ${APP_DIR}/*.py     "${BACKUP_PATH}/" 2>/dev/null
cp ${APP_DIR}/*.sh     "${BACKUP_PATH}/" 2>/dev/null
cp ${APP_DIR}/*.sql    "${BACKUP_PATH}/" 2>/dev/null
cp ${APP_DIR}/*.txt    "${BACKUP_PATH}/" 2>/dev/null
cp ${APP_DIR}/.env     "${BACKUP_PATH}/" 2>/dev/null
cp ${APP_DIR}/ssl.key  "${BACKUP_PATH}/" 2>/dev/null
cp ${APP_DIR}/ssl.crt  "${BACKUP_PATH}/" 2>/dev/null
cp -r ${APP_DIR}/templates "${BACKUP_PATH}/" 2>/dev/null
echo "✔ Файлы приложения сохранены"

# 3. Systemd сервисы
echo "→ Конфигурация сервисов..."
mkdir -p "${BACKUP_PATH}/systemd"
cp /etc/systemd/system/propuska-*.service "${BACKUP_PATH}/systemd/" 2>/dev/null
echo "✔ Сервисы сохранены"

# 4. Упаковка
echo "→ Упаковка архива..."
cd "${BACKUP_DIR}"
tar -czf "${BACKUP_NAME}.tar.gz" "${BACKUP_NAME}/"
rm -rf "${BACKUP_PATH}"
echo "✔ Архив создан: ${BACKUP_DIR}/${BACKUP_NAME}.tar.gz ($(du -sh ${BACKUP_DIR}/${BACKUP_NAME}.tar.gz | cut -f1))"

# 5. Чистим старые бэкапы (оставляем 7 последних)
echo "→ Очистка старых бэкапов..."
ls -t ${BACKUP_DIR}/propuska_backup_*.tar.gz 2>/dev/null | tail -n +8 | xargs rm -f
TOTAL=$(ls ${BACKUP_DIR}/propuska_backup_*.tar.gz 2>/dev/null | wc -l)
echo "✔ Хранится бэкапов: ${TOTAL}"

echo ""
# Запускаем тесты после бэкапа
echo ""
echo "→ Запуск автотестов..."
cd /home/user && .venv/bin/python test.py
echo ""
echo "✅ Резервная копия готова:"
echo "   ${BACKUP_DIR}/${BACKUP_NAME}.tar.gz"
echo "═══════════════════════════════════════════════════"
