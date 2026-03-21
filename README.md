# 🛡 Пропускная система — КП Петровское Парк

Система цифрового управления въездом для жилого комплекса.

- Жильцы оформляют пропуска через Telegram-бот
- Охрана одобряет/отклоняет через свой бот или веб-панель
- На КПП проверяют номер авто

## Быстрая установка на новый сервер
```bash
curl -fsSL https://raw.githubusercontent.com/alexxeladev/AutoPass/main/deploy.sh | sudo bash
```

Скрипт сам:
- Скачает код с GitHub
- Спросит токены Telegram
- Установит PostgreSQL, Python, зависимости
- Создаст systemd-сервисы и запустит всё

## Что нужно перед установкой

1. Ubuntu 20.04 / 22.04 / 24.04
2. Два Telegram-бота — создать у @BotFather
3. Telegram ID охранника — узнать через @userinfobot

## Управление
```bash
bash start.sh status      # статус сервисов
bash start.sh restart     # перезапуск
bash start.sh logs web    # логи веб-панели
bash start.sh logs bot    # логи бота жильцов
bash start.sh logs guard  # логи бота охраны
```

## Структура
```
bot.py          — Telegram-бот жильцов
guard_bot.py    — Telegram-бот охраны
web_app.py      — Веб-панель (FastAPI, порт 8000)
schema.sql      — Схема БД
install.sh      — Установка системы
deploy.sh       — Развёртывание с GitHub одной командой
start.sh        — Управление сервисами
backup.sh       — Резервное копирование
restore.sh      — Восстановление из бэкапа
cleanup_db.sh   — Очистка тестовых данных
test.py         — Автотесты (30+ тестов)
```

## Веб-панель

После установки открыть: `http://IP_СЕРВЕРА:8000`
Логин: `admin` / Пароль: `admin123` — **сменить сразу после входа!**
