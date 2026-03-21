# Пропускная система 

Этот файл содержит полный контекст проекта для Claude. Читай его перед любыми изменениями.

---

## Обзор проекта

Система цифрового управления въездом гостей для жилого комплекса. Жильцы оформляют пропуска через Telegram-бот, охрана одобряет/отклоняет через свой бот или веб-панель, на КПП проверяют номер авто.

**Версия:** v1.3  
**Сервер:** Ubuntu 24, VPS, `/home/user/`  
**Веб-панель:** `https://176.222.52.214:8000`  

---

## Архитектура

```
bot.py          — Telegram-бот жильцов (aiogram 3, polling)
guard_bot.py    — Telegram-бот охраны  (aiogram 3, polling)
web_app.py      — Веб-панель FastAPI   (uvicorn, порт 8000, HTTPS)
schema.sql      — Схема PostgreSQL
```

Три отдельных процесса под управлением systemd. Все читают один `.env` файл.

**Поток уведомлений:**
- `bot.py` создаёт пропуск → отправляет уведомление охране через `guard_bot` (GUARD_BOT_TOKEN)
- Охрана одобряет/отклоняет в `guard_bot.py` → уведомление жильцу через `Bot(BOT_TOKEN)`
- Веб-панель одобряет/отклоняет → уведомление жильцу через `bot_instance` + охране через `guard_bot_instance`

---

## Переменные окружения (.env)

```env
BOT_TOKEN=             # токен бота жильцов
GUARD_BOT_TOKEN=       # токен бота охраны
SECURITY_CHAT_ID=      # Telegram ID охранника (число)
DATABASE_URL=postgresql://propuska:ПАРОЛЬ@127.0.0.1:5432/propuska_db
JWT_SECRET_KEY=        # случайная строка 32+ символа
DB_PASSWORD=           # пароль пользователя propuska в PostgreSQL
```

> `SECURITY_CHAT_ID` — личный Telegram ID охранника, не группа. Получить через @userinfobot.

---

## База данных

**Пользователь БД:** `propuska`  
**База:** `propuska_db`  
**Подключение:** всегда через `127.0.0.1`, не `localhost` (иначе peer auth)

### Таблицы

```sql
residents        — жильцы (house, apartment, full_name, phone UNIQUE, telegram_id UNIQUE, verified)
cars             — личные авто жильцов (resident_id FK, car_number) UNIQUE(resident_id, car_number)
verification_codes — SMS коды (phone PK, code, expires_at)
passes           — пропуска гостей (resident_id FK, car_number, date_from, date_to, status)
users            — аккаунты веб-панели (username UNIQUE, password_hash, role)
guards           — охранники (full_name, telegram_id BIGINT UNIQUE, phone, active BOOLEAN)
```

**Статусы пропуска:** `pending` → `approved` / `rejected`

**Важно:** после любого `ALTER TABLE` или добавления таблиц выполнить:
```sql
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO propuska;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO propuska;
```

---

## Сервисы systemd

```
propuska-web.service    — uvicorn web_app:app --host 0.0.0.0 --port 8000 --ssl-*
propuska-bot.service    — python bot.py
propuska-guard.service  — python guard_bot.py
```

**ExecStartPre** в сервисах ботов: сброс Telegram webhook перед стартом (предотвращает конфликт сессий при перезагрузке).

**Управление:**
```bash
bash start.sh           # запуск всего
bash start.sh restart   # перезапуск (со сбросом webhook)
bash start.sh status    # статус
bash start.sh logs bot  # логи бота жильцов
sudo journalctl -u propuska-web -n 30 --no-pager
```

---

## bot.py — Бот жильцов

### FSM состояния

```
AuthState:
  waiting_for_phone    — ожидание номера телефона (F.contact)
  waiting_for_code     — ожидание 6-значного кода подтверждения

PassOrder:
  waiting_for_car      — ввод номера авто гостя
  waiting_for_dates    — ввод произвольного диапазона дат
  waiting_for_confirm  — подтверждение перед отправкой

RejectReason:
  waiting_for_reason   — не используется в bot.py (только guard_bot.py)
```

### Ключевые обработчики

| Хэндлер | Триггер | Действие |
|---------|---------|----------|
| `cmd_start` | /start | Проверяет авторизацию, показывает меню или просит телефон |
| `process_phone` | F.contact | Генерирует 6-значный код, сохраняет в verification_codes |
| `process_code` | AuthState.waiting_for_code | Проверяет код, привязывает telegram_id |
| `start_new_pass` | «➕ Новый пропуск» | Запрашивает номер авто |
| `process_car` | PassOrder.waiting_for_car | Нормализует номер, показывает выбор периода |
| `process_period` | period_today / period_tomorrow | Устанавливает даты, показывает подтверждение |
| `confirm_pass_cb` | confirm_pass | Создаёт пропуск в БД, уведомляет охрану |
| `fix_car_cb` | fix_car | Возвращает к вводу номера |
| `cancel_my_pass_N` | cancel_my_pass_{id} | Отменяет пропуск в статусе pending |

### Уведомление охраны

При создании пропуска `bot.py` отправляет сообщение через `guard_bot` (GUARD_BOT_TOKEN) на SECURITY_CHAT_ID с inline-кнопками `approve_{id}` / `reject_{id}`.

---

## guard_bot.py — Бот охраны

### Авторизация

При `/start` проверяет `telegram_id` пользователя в таблице `guards` где `active=TRUE`. Если не найден — отказывает.

### Reply-keyboard (постоянное меню)

```
📋 Заявки    📅 Активные
🔍 Проверить  📊 Статистика
```

### FSM состояния

```
CheckState:
  waiting_for_car     — ввод номера авто для проверки

RejectReason:
  waiting_for_reason  — охранник вводит текст причины отклонения
```

### Ключевые обработчики

| Хэндлер | Триггер | Действие |
|---------|---------|----------|
| `cmd_start` | /start | Проверяет guards, показывает меню |
| `pending_reply` | «📋 Заявки» | Список pending пропусков |
| `active_reply` | «📅 Активные» | Активные пропуска на сегодня |
| `check_reply` | «🔍 Проверить» | Запрашивает номер авто |
| `do_check` | CheckState.waiting_for_car | Поиск в passes + cars |
| `stats_reply` | «📊 Статистика» | Сводка за сегодня и 30 дней |
| `approve_pass` | approve_{id} | Одобряет, уведомляет жильца |
| `reject_pass` | reject_{id} | Запрашивает причину |
| `process_reject_reason` | RejectReason.waiting_for_reason | Отклоняет с причиной |
| `undo_decision` | undo_{id} | Возвращает пропуск в pending |

### Проверка авто (do_check)

Нормализация: `UPPER(REPLACE(number, ' ', ''))`. Три варианта ответа:
1. Активный пропуск (status=approved, date_from≤today≤date_to) → «✅ ВЪЕЗД РАЗРЕШЁН»
2. Авто из таблицы `cars` (личный автомобиль жильца) → «🏠 Автомобиль жильца — ВЪЕЗД РАЗРЕШЁН»
3. Ничего не найдено → «❌ Пропуск не найден»

---

## web_app.py — Веб-панель

### Аутентификация

JWT в httponly cookie `access_token`. Срок: 8 часов.  
`auth_redirect(request)` — возвращает RedirectResponse на /login если нет валидного токена.  
Дефолтный аккаунт: `admin` / `admin123` — создаётся при старте если нет ни одного пользователя.

### Маршруты

| Метод | URL | Описание |
|-------|-----|----------|
| GET | `/` | Редирект на /dashboard |
| GET/POST | `/login` | Авторизация |
| GET | `/logout` | Выход |
| GET | `/dashboard` | Главная: pending заявки + статистика |
| GET | `/requests` | Все заявки с фильтрацией |
| GET | `/history` | История пропусков |
| GET | `/check` | Форма проверки авто |
| POST | `/api/check-car` | API проверки авто |
| POST | `/api/pass/{id}/approve` | Одобрить пропуск |
| POST | `/api/pass/{id}/reject` | Отклонить пропуск |
| GET | `/api/pending-count` | Количество pending (для автообновления) |
| GET | `/api/passes/{status}` | HTML строки таблицы (pending/approved/rejected) |
| GET | `/residents` | Список жильцов |
| POST | `/residents/add` | Добавить жильца |
| POST | `/residents/update/{id}` | Обновить жильца |
| POST | `/residents/delete/{id}` | Удалить жильца |
| POST | `/residents/{id}/cars/add` | Добавить авто жильцу |
| POST | `/residents/{id}/cars/delete` | Удалить авто жильца |
| GET | `/residents/{id}/cars/list` | JSON список авто жильца |
| GET | `/reports` | Отчёты со статистикой |
| GET | `/guards` | Управление охранниками |
| POST | `/guards/add` | Добавить охранника |
| POST | `/guards/{id}/toggle` | Заблокировать/разблокировать |
| POST | `/guards/{id}/delete` | Удалить охранника |
| GET | `/users` | Управление аккаунтами |
| POST | `/users/change-password` | Смена пароля |

### Dashboard автообновление

`/api/pending-count` опрашивается каждые 3 секунды через JS. При изменении счётчика — обновляется таблица через `/api/passes/pending`. Модальное окно пропусков также обновляется каждые 5 секунд.

### Уведомление при решении через веб

`notify_resident()` в web_app.py: отправляет уведомление жильцу через `bot_instance` (BOT_TOKEN) + охране через `guard_bot_instance` (GUARD_BOT_TOKEN).

---

## Известные баги (backlog)

| # | Описание | Приоритет |
|---|----------|-----------|
| 1 | Авто не сохраняются при добавлении нового жильца через модал — скрытые поля `car_numbers` не попадают в POST. Через кнопку 🚗 у существующего жильца работает. | High |

---

## Запланировано на v1.4

- 🔴 Исправить баг с сохранением авто при создании жильца
- 📱 Адаптивный дизайн под мобильные устройства
- 🐳 Docker-контейнеризация (docker-compose + deploy.sh)

---

## Автоматизация

```bash
# Автотесты — каждый час
crontab: 0 * * * * cd /home/user && .venv/bin/python test.py >> backups/test.log 2>&1

# Бэкап — каждый день в 3:00
crontab: 0 3 * * * /home/user/backup.sh >> backups/backup.log 2>&1

# Запуск тестов вручную
cd /home/user && .venv/bin/python test.py

# Создать бэкап вручную
bash backup.sh

# Восстановить из бэкапа
bash restore.sh
```

Бэкапы хранятся в `/home/user/backups/`, последние 7 копий.

---

## SSL

Самоподписанный сертификат на IP `176.222.52.214`:
```
/home/user/ssl.key
/home/user/ssl.crt
```

При переезде на домен — заменить на Let's Encrypt:
```bash
certbot certonly --standalone -d домен.ru
# Обновить пути в /etc/systemd/system/propuska-web.service
```

---

## Частые проблемы

| Проблема | Причина | Решение |
|----------|---------|---------|
| `permission denied for table X` | Права пользователя propuska | `GRANT ALL ON ALL TABLES IN SCHEMA public TO propuska;` |
| `peer authentication failed` | localhost вместо 127.0.0.1 в DATABASE_URL | Использовать `127.0.0.1` |
| Бот не отвечает | Конфликт — два экземпляра запущены | `deleteWebhook` + рестарт сервиса |
| Веб недоступен снаружи | SSL сертификат не на тот CN | Перевыпустить с правильным IP/доменом |
| Сервис не стартует после reboot | ExecStartPre не прописан | Проверить `grep ExecStartPre /etc/systemd/system/propuska-*.service` |

---

## Важные соглашения

- Номера авто нормализуются: `UPPER(REPLACE(number, ' ', ''))` — всегда при записи и поиске
- Телефоны хранятся в формате `+7XXXXXXXXXX`
- Даты пропусков — тип `DATE` (не datetime), сравнение через `date_from <= today <= date_to`
- `DELETE FROM table` — не `TRUNCATE` (нарушает FK)
- Все три сервиса должны работать одновременно — они взаимозависимы через БД
- bcrypt импортируется напрямую: `import bcrypt as _bcrypt_lib` (не через passlib — баг на Python 3.12)

---

## Структура файлов проекта

```
/home/user/
├── bot.py                  — бот жильцов
├── guard_bot.py            — бот охраны
├── web_app.py              — веб-панель FastAPI
├── schema.sql              — схема БД
├── requirements.txt        — зависимости Python
├── install.sh              — установка системы
├── start.sh                — управление сервисами
├── backup.sh               — резервное копирование
├── restore.sh              — восстановление из бэкапа
├── cleanup_db.sh           — очистка тестовых данных
├── test.py                 — автотесты
├── .env                    — секреты и конфигурация
├── .env.example            — шаблон для .env
├── ssl.key / ssl.crt       — SSL сертификат
├── backups/                — директория бэкапов
│   ├── propuska_backup_YYYYMMDD_HHMMSS.tar.gz
│   ├── backup.log
│   └── test.log
└── templates/
    ├── base.html
    ├── login.html
    ├── dashboard.html
    ├── requests.html
    ├── history.html
    ├── check.html
    ├── residents.html
    ├── guards.html
    ├── reports.html
    └── users.html
```

---

### bot.py (539 строк)

Telegram-бот для жильцов. aiogram 3, FSM через MemoryStorage.

**Что делает:**
- Авторизует жильцов по номеру телефона + SMS-код (6 цифр, TTL 5 минут)
- Принимает заявки на пропуск: номер авто → выбор периода → подтверждение → создание
- Показывает историю заявок жильца (последние 10)
- Позволяет отменить заявку в статусе `pending`
- Уведомляет жильца при одобрении/отклонении (получает callback от guard_bot через общую БД)
- Отправляет уведомления охране при создании пропуска через `guard_bot` (GUARD_BOT_TOKEN)

**Ключевые объекты:**
- `guard_bot = Bot(token=GUARD_BOT_TOKEN)` — создаётся в bot.py для отправки уведомлений охране
- `bot_instance = Bot(token=BOT_TOKEN)` — используется в web_app.py для уведомлений жильцам

---

### guard_bot.py (490 строк)

Telegram-бот для охраны. aiogram 3, FSM через MemoryStorage.

**Что делает:**
- Авторизует охранников по `telegram_id` (проверка в таблице `guards`, `active=TRUE`)
- Показывает pending заявки с кнопками одобрить/отклонить
- Принимает причину отклонения текстом
- Показывает активные пропуска на сегодня
- Проверяет номер авто на въезде (поиск в passes + cars)
- Показывает статистику за сегодня и 30 дней
- Обрабатывает callback `approve_` / `reject_` / `undo_` — пришедшие из сообщений которые сам же отправил
- Уведомляет жильца о решении через `Bot(token=BOT_TOKEN)`

**Важно:** `approve_`/`reject_` callbacks должны обрабатываться именно в guard_bot.py — это тот же бот что отправлял сообщение с кнопками. Telegram не позволяет обрабатывать callbacks другим ботом.

---

### web_app.py (869 строк)

FastAPI веб-приложение. Jinja2 шаблоны, asyncpg для БД, JWT в httponly cookies.

**Что делает:**
- Авторизация через логин/пароль → JWT cookie (8 часов)
- Дашборд с pending заявками, статистикой дня, автообновлением каждые 3 сек
- Управление жильцами и их личными автомобилями
- Управление охранниками (добавление, блокировка, удаление)
- История пропусков с фильтрацией
- Отчёты со статистикой
- API для одобрения/отклонения пропусков (с уведомлениями через оба бота)
- API проверки авто на КПП
- Смена пароля администратора

**Зависимости:** `python-jose` (JWT), `bcrypt` (хэширование), `aiogram.Bot` (отправка уведомлений)

---

### schema.sql (3038 байт)

SQL схема базы данных. Применяется при первой установке через `install.sh`.

**Содержит:** CREATE TABLE IF NOT EXISTS для всех 6 таблиц + индексы.  
**Не содержит:** таблицу `guards` в старых версиях — была добавлена через `ALTER` в процессе разработки. В v1.3 уже включена.

---

### requirements.txt

```
fastapi==0.110.0
uvicorn[standard]==0.27.1
jinja2==3.1.3
python-multipart==0.0.9
asyncpg==0.29.0
python-jose[cryptography]==3.3.0
passlib[bcrypt]==1.7.4      # установлен, но bcrypt используется напрямую
aiogram==3.4.1
```

> `passlib` установлен для совместимости, но bcrypt вызывается напрямую (`import bcrypt as _bcrypt_lib`) из-за бага `ValueError` в Python 3.12.

---

### install.sh (10 227 байт)

Полная автоматическая установка на чистый Ubuntu 20.04/22.04/24.04.

**Шаги:**
1. Системные пакеты (postgresql, python3-venv, python3-pip)
2. PostgreSQL — создание пользователя `propuska`, БД `propuska_db`, применение schema.sql
3. Python-окружение — создание `.venv`, установка requirements.txt
4. Настройка pg_hba.conf — добавление md5 правила для пользователя propuska
5. Systemd сервисы — создание propuska-web/bot/guard.service, enable + start
6. Проверка — тест подключения к БД, проверка что все сервисы запущены

**Важная правка для VPS:** перед запуском скопировать schema.sql в /tmp:
```bash
cp schema.sql /tmp/schema.sql && chmod 644 /tmp/schema.sql
sed -i 's|-f schema.sql|-f /tmp/schema.sql|' install.sh
```

---

### start.sh (7 708 байт)

Управление сервисами с цветным выводом.

**Команды:**
```bash
bash start.sh              # запуск (start)
bash start.sh stop         # остановка
bash start.sh restart      # перезапуск
bash start.sh status       # статус всех сервисов
bash start.sh logs         # все логи
bash start.sh logs web     # логи веб-панели
bash start.sh logs bot     # логи бота жильцов
bash start.sh logs guard   # логи бота охраны
```

**При restart и start:** сначала вызывает `deleteWebhook` для обоих ботов (сброс Telegram сессий), затем запускает сервисы. Токены читаются из `.env`.

---

### backup.sh (3 127 байт)

Резервное копирование всего проекта.

**Что сохраняет:** дамп БД (`pg_dump`), все `.py` файлы, `.env`, SSL сертификаты, templates/, systemd сервисы.  
**Формат:** `propuska_backup_YYYYMMDD_HHMMSS.tar.gz` в `/home/user/backups/`  
**Ротация:** хранит последние 7 копий, старые удаляет.  
**После бэкапа:** запускает `test.py`.  
**Cron:** запускается автоматически каждый день в 3:00.

---

### restore.sh (4 256 байт)

Интерактивное восстановление из бэкапа.

**Процесс:**
1. Показывает список доступных бэкапов с датами и размерами
2. Запрашивает подтверждение (требует ввести `yes`)
3. Останавливает сервисы
4. Удаляет и пересоздаёт БД из дампа
5. Восстанавливает файлы приложения
6. Восстанавливает systemd сервисы
7. Запускает всё и показывает статус

---

### cleanup_db.sh (3 121 байт)

Очистка тестовых данных из БД. Использовать при тестировании.

**Удаляет:** все записи из `passes`, `residents`, `cars`, `verification_codes`, `guards`.  
**Не трогает:** таблицу `users` (аккаунты веб-панели).  
**Требует подтверждения** перед выполнением.

---

### test.py (441 строка, 30+ тестов)

Автотесты системы. Запускается каждый час через cron.

**5 секций тестов:**

| Секция | Что проверяет |
|--------|---------------|
| 1. База данных | Наличие всех таблиц, индексов, прав пользователя propuska, аккаунта admin |
| 2. Веб-панель | Доступность страниц, авторизация, редирект без токена, все API эндпоинты |
| 3. Telegram боты | Валидность токенов через `getMe`, проверка polling (конфликт = бот работает) |
| 4. Логика пропусков | Создание пропусков, фильтрация по дате, нормализация номеров, каскадное удаление |
| 5. Охранники | Добавление, уникальность telegram_id, блокировка, проверка доступа |

**Зависимости:** `httpx`, `asyncpg`, `urllib3` (установить в .venv).  
**Результат:** цветной вывод ✔/✘/⚠ + итоговая сводка. Exit code 1 при провале.

---

### templates/base.html (543 строки)

Базовый шаблон. Все страницы наследуются через `{% extends "base.html" %}`.

**Включает:**
- Sidebar навигация (Dashboard, Заявки, История, Проверить, Жильцы, Охранники, Отчёты, Аккаунт)
- CSS переменные темы (тёмная тема: `--bg`, `--surface`, `--text`, `--border`, `--green`, `--red`)
- Шрифты: Manrope (основной), DM Mono (номера авто)
- Bootstrap 5.3, Font Awesome 6.4, HTMX 1.9.6
- Счётчик pending заявок в sidebar (обновляется каждые 3 сек через `/api/pending-count`)

**Блоки для переопределения:** `{% block title %}`, `{% block content %}`

---

### templates/login.html (207 строк)

Страница входа. Брендинг «Петровское Парк / Система пропусков». Показывает сообщения об ошибках из query параметров.

---

### templates/dashboard.html (192 строки)

Главная страница охраны.

**Содержит:**
- 4 карточки статистики: Всего сегодня / Одобрено / Отклонено / Ожидает
- Таблица нерассмотренных заявок (pending) с кнопками одобрить/отклонить через HTMX
- Кнопка «Обновить» + автообновление таблицы каждые 3 сек
- Модальное окно для просмотра одобренных/отклонённых (загружает строки из `/api/passes/{status}`)
- Модал автообновляется каждые 5 сек пока открыт

---

### templates/check.html (187 строк)

Проверка авто на КПП.

**Содержит:**
- Форма ввода номера авто (POST на `/api/check-car`)
- Результат: зелёная карточка (въезд разрешён) / красная (не найден) / синяя (авто жильца)
- Заголовок «🏠 Автомобиль жильца — ВЪЕЗД РАЗРЕШЁН» — большой, зелёный, жирный
- Цвета текста в карточке: `color:var(--text)` (исправлен баг тёмного текста на тёмном фоне)

---

### templates/residents.html (357 строк)

Управление жильцами. Самый сложный шаблон.

**Содержит:**
- Таблица жильцов с поиском
- Модал добавления жильца (ФИО, телефон, адрес, авто — tag-input через JS массив `addCars[]`)
- Модал редактирования жильца
- Модал управления авто 🚗 (добавление через fetch + `/residents/{id}/cars/list`, удаление через form POST)
- **Известный баг:** при добавлении нового жильца авто из `addCars[]` не попадают в POST — скрытые поля `car_numbers` не отправляются

---

### templates/guards.html (108 строк)

Управление охранниками.

**Содержит:**
- Таблица охранников (ФИО, Telegram ID, телефон, статус активности)
- Кнопки: заблокировать 🚫 / активировать ✓ / удалить 🗑
- Модал добавления охранника
- Инструкция как получить Telegram ID через @userinfobot

---

### templates/history.html (98 строк)

История всех пропусков с фильтрацией по дате и статусу.

---

### templates/requests.html (88 строк)

Список всех заявок с фильтрацией. Отличается от dashboard — показывает все статусы, без кнопок одобрения.

---

### templates/reports.html (112 строк)

Отчёты и статистика. Сводка по периодам, фильтрация по статусу.

---

### templates/users.html (85 строк)

Управление аккаунтами веб-панели. Смена пароля (минимум 8 символов).
