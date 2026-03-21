#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════
  АВТОТЕСТЫ — КП Петровское Парк
═══════════════════════════════════════════════════════
"""
import asyncio
import sys
import os
import httpx
import asyncpg
from datetime import date, timedelta

# ── Цвета ──────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BLUE   = "\033[94m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

# ── Конфиг ─────────────────────────────────────────
BASE_URL    = "https://127.0.0.1:8000"
ADMIN_USER  = "admin"
ADMIN_PASS  = os.getenv("ADMIN_PASSWORD", "admin123")

def load_env():
    if os.path.exists(".env"):
        with open(".env") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

load_env()
DATABASE_URL   = os.getenv("DATABASE_URL", "postgresql://propuska:propuska123@localhost:5432/propuska_db")
BOT_TOKEN      = os.getenv("BOT_TOKEN")
GUARD_TOKEN    = os.getenv("GUARD_BOT_TOKEN")

# ── Счётчики ────────────────────────────────────────
passed = 0
failed = 0
warnings = 0

def ok(name):
    global passed
    passed += 1
    print(f"  {GREEN}✔{RESET} {name}")

def fail(name, reason=""):
    global failed
    failed += 1
    r = f" — {reason}" if reason else ""
    print(f"  {RED}✘{RESET} {name}{RED}{r}{RESET}")

def warn(name, reason=""):
    global warnings
    warnings += 1
    r = f" — {reason}" if reason else ""
    print(f"  {YELLOW}⚠{RESET} {name}{YELLOW}{r}{RESET}")

def section(title):
    print(f"\n{BOLD}{BLUE}{'═'*50}{RESET}")
    print(f"{BOLD}{BLUE}  {title}{RESET}")
    print(f"{BOLD}{BLUE}{'═'*50}{RESET}")

# ═══════════════════════════════════════════════════
# 1. БАЗА ДАННЫХ
# ═══════════════════════════════════════════════════
async def test_database():
    section("1. БАЗА ДАННЫХ")
    try:
        conn = await asyncpg.connect(DATABASE_URL.replace("localhost", "127.0.0.1"))

        # Таблицы
        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        )
        table_names = [t["tablename"] for t in tables]
        required = ["residents", "cars", "passes", "users", "guards", "verification_codes"]
        for t in required:
            if t in table_names:
                ok(f"Таблица '{t}' существует")
            else:
                fail(f"Таблица '{t}' не найдена")

        # Индексы
        indexes = await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname='public'"
        )
        idx_names = [i["indexname"] for i in indexes]
        for idx in ["idx_passes_status", "idx_passes_car", "idx_residents_phone"]:
            if idx in idx_names:
                ok(f"Индекс '{idx}' существует")
            else:
                warn(f"Индекс '{idx}' не найден")

        # Права пользователя propuska
        for tbl in ["passes", "residents", "cars", "guards", "users"]:
            try:
                await conn.execute(f"SET ROLE propuska; SELECT 1 FROM {tbl} LIMIT 1; RESET ROLE;")
                ok(f"Права на таблицу '{tbl}'")
            except Exception as e:
                fail(f"Нет прав на таблицу '{tbl}'", str(e)[:60])

        # Дефолтный admin
        user = await conn.fetchrow("SELECT * FROM users WHERE username='admin'")
        if user:
            ok("Пользователь admin существует")
        else:
            warn("Пользователь admin не найден")

        await conn.close()

    except Exception as e:
        fail("Подключение к БД", str(e)[:80])


# ═══════════════════════════════════════════════════
# 2. ВЕБ-ПАНЕЛЬ
# ═══════════════════════════════════════════════════
async def test_web():
    section("2. ВЕБ-ПАНЕЛЬ")
    cookies = {}

    async with httpx.AsyncClient(verify=False, follow_redirects=True, timeout=10) as client:

        # Доступность
        try:
            r = await client.get(f"{BASE_URL}/login")
            if r.status_code == 200:
                ok("Страница /login доступна")
            else:
                fail("Страница /login", f"статус {r.status_code}")
        except Exception as e:
            fail("Сервер недоступен", str(e)[:60])
            return

        # Авторизация с неверным паролем
        r = await client.post(f"{BASE_URL}/login",
            data={"username": "admin", "password": "wrongpass"},
            follow_redirects=True)
        if "Неверный" in r.text or r.url.path == "/login":
            ok("Отклонение неверного пароля")
        else:
            warn("Проверка неверного пароля — неожиданный ответ")

        # Авторизация
        r = await client.post(f"{BASE_URL}/login",
            data={"username": ADMIN_USER, "password": ADMIN_PASS},
            follow_redirects=True)
        if r.status_code == 200 and "dashboard" in str(r.url):
            ok(f"Авторизация admin")
            cookies = dict(r.cookies)
        else:
            fail("Авторизация admin", f"url={r.url}, status={r.status_code}")
            return

        # Защищённые страницы
        pages = ["/dashboard", "/requests", "/history", "/check",
                 "/residents", "/guards", "/reports", "/users"]
        for page in pages:
            r = await client.get(f"{BASE_URL}{page}", cookies=cookies)
            if r.status_code == 200:
                ok(f"Страница {page}")
            else:
                fail(f"Страница {page}", f"статус {r.status_code}")

        # Редирект без авторизации — используем чистый клиент без куки
        async with httpx.AsyncClient(verify=False, follow_redirects=False, timeout=10) as clean:
            r = await clean.get(f"{BASE_URL}/dashboard")
            if r.status_code in (302, 307):
                ok("Редирект на /login без авторизации")
            else:
                fail("Редирект без авторизации", f"статус {r.status_code}")

        # API pending-count
        r = await client.get(f"{BASE_URL}/api/pending-count", cookies=cookies)
        if r.status_code == 200 and "count" in r.json():
            ok(f"API /api/pending-count → count={r.json()['count']}")
        else:
            fail("API /api/pending-count", f"статус {r.status_code}")

        # Добавление жильца
        r = await client.post(f"{BASE_URL}/residents/add", cookies=cookies, data={
            "full_name": "Тестов Тест Тестович",
            "phone": "+79990000001",
            "house": "99",
            "apartment": "99",
        }, follow_redirects=True)
        if r.status_code == 200 and "Тестов" in r.text:
            ok("Добавление жильца")
        else:
            warn("Добавление жильца", f"статус {r.status_code}")

        # Проверка дублирования жильца
        r = await client.post(f"{BASE_URL}/residents/add", cookies=cookies, data={
            "full_name": "Тестов Тест Тестович",
            "phone": "+79990000001",
            "house": "99",
            "apartment": "99",
        }, follow_redirects=True)
        if "exists" in str(r.url) or "exists" in r.text:
            ok("Защита от дублирования жильца")
        else:
            warn("Защита от дублирования", "дубль мог пройти")

        # Проверка авто через API
        r = await client.post(f"{BASE_URL}/api/check-car",
            cookies=cookies,
            data={"car_number": "ХХХXXX000"})
        if r.status_code == 200:
            ok("API /api/check-car работает")
        else:
            fail("API /api/check-car", f"статус {r.status_code}")

        # Passes API
        for status in ["pending", "approved", "rejected"]:
            r = await client.get(f"{BASE_URL}/api/passes/{status}", cookies=cookies)
            if r.status_code == 200:
                ok(f"API /api/passes/{status}")
            else:
                fail(f"API /api/passes/{status}", f"статус {r.status_code}")

        # Очистка тестового жильца
        conn = await asyncpg.connect(DATABASE_URL.replace("localhost", "127.0.0.1"))
        await conn.execute("DELETE FROM residents WHERE phone='+79990000001'")
        await conn.close()
        ok("Очистка тестовых данных")


# ═══════════════════════════════════════════════════
# 3. TELEGRAM БОТЫ
# ═══════════════════════════════════════════════════
async def test_bots():
    section("3. TELEGRAM БОТЫ")

    async with httpx.AsyncClient(timeout=10) as client:
        for name, token in [("Бот жильцов", BOT_TOKEN), ("Бот охраны", GUARD_TOKEN)]:
            if not token:
                warn(f"{name}", "токен не задан в .env")
                continue
            try:
                r = await client.get(f"https://api.telegram.org/bot{token}/getMe")
                data = r.json()
                if data.get("ok"):
                    bot_name = data["result"]["username"]
                    ok(f"{name} (@{bot_name}) — токен валидный")
                else:
                    fail(f"{name}", data.get("description", "неверный токен"))
            except Exception as e:
                fail(f"{name}", str(e)[:60])

        # Проверяем что нет конфликта (два экземпляра)
        if BOT_TOKEN:
            try:
                r = await client.get(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                    params={"timeout": 1}
                )
                data = r.json()
                if data.get("ok"):
                    ok("Бот жильцов — нет конфликта getUpdates")
                elif "Conflict" in str(data.get("description", "")):
                    fail("Бот жильцов — конфликт! Запущен второй экземпляр")
                else:
                    warn("Бот жильцов getUpdates", data.get("description",""))
            except Exception as e:
                warn("Проверка конфликта", str(e)[:60])

        if GUARD_TOKEN:
            r = await client.get(f"https://api.telegram.org/bot{GUARD_TOKEN}/getUpdates", params={"timeout":1})
            data = r.json()
            if data.get("ok"):
                ok("Бот охраны — работает (нет лишних экземпляров)")
            elif "Conflict" in str(data.get("description","")):
                ok("Бот охраны — работает (polling активен)")
            else:
                fail("Бот охраны", data.get("description",""))


# ═══════════════════════════════════════════════════
# 4. ЛОГИКА ПРОПУСКОВ
# ═══════════════════════════════════════════════════
async def test_passes_logic():
    section("4. ЛОГИКА ПРОПУСКОВ")
    try:
        conn = await asyncpg.connect(DATABASE_URL.replace("localhost", "127.0.0.1"))
        today = date.today()
        tomorrow = today + timedelta(days=1)
        yesterday = today - timedelta(days=1)

        # Создаём тестового жильца
        resident_id = await conn.fetchval(
            "INSERT INTO residents (house, apartment, full_name, phone) "
            "VALUES ('99','99','Тест Авто','+ 79990000099') RETURNING id"
        )

        # Создаём пропуска с разными статусами
        pass_active = await conn.fetchval(
            "INSERT INTO passes (resident_id, guest_fullname, car_number, date_from, date_to, status) "
            "VALUES ($1,'Гость','Т999ТТ99',$2,$3,'approved') RETURNING id",
            resident_id, today, tomorrow
        )
        pass_expired = await conn.fetchval(
            "INSERT INTO passes (resident_id, guest_fullname, car_number, date_from, date_to, status) "
            "VALUES ($1,'Гость','Т888ТТ99',$2,$3,'approved') RETURNING id",
            resident_id, yesterday, yesterday
        )
        pass_pending = await conn.fetchval(
            "INSERT INTO passes (resident_id, guest_fullname, car_number, date_from, date_to, status) "
            "VALUES ($1,'Гость','Т777ТТ99',$2,$3,'pending') RETURNING id",
            resident_id, today, tomorrow
        )

        # Проверяем активный пропуск
        row = await conn.fetchrow(
            "SELECT * FROM passes WHERE id=$1 "
            "AND status='approved' AND date_from<=$2 AND date_to>=$2",
            pass_active, today
        )
        if row: ok("Активный пропуск найден по дате")
        else: fail("Активный пропуск не найден")

        # Проверяем что истёкший пропуск не активен
        row = await conn.fetchrow(
            "SELECT * FROM passes WHERE id=$1 "
            "AND status='approved' AND date_from<=$2 AND date_to>=$2",
            pass_expired, today
        )
        if not row: ok("Истёкший пропуск не показывается как активный")
        else: fail("Истёкший пропуск показывается как активный")

        # Смена статуса
        await conn.execute("UPDATE passes SET status='approved' WHERE id=$1", pass_pending)
        row = await conn.fetchrow("SELECT status FROM passes WHERE id=$1", pass_pending)
        if row["status"] == "approved": ok("Смена статуса pending → approved")
        else: fail("Смена статуса не сработала")

        # Проверка нормализации номера авто
        normalized = "Т999ТТ99"
        row = await conn.fetchrow(
            "SELECT * FROM passes WHERE UPPER(REPLACE(car_number,' ',''))=$1 "
            "AND status='approved' AND date_from<=$2 AND date_to>=$2",
            normalized, today
        )
        if row: ok("Поиск авто с нормализацией номера")
        else: fail("Поиск авто с нормализацией не работает")

        # Каскадное удаление
        await conn.execute("DELETE FROM residents WHERE id=$1", resident_id)
        count = await conn.fetchval("SELECT COUNT(*) FROM passes WHERE resident_id=$1", resident_id)
        if count == 0: ok("Каскадное удаление пропусков при удалении жильца")
        else: fail("Каскадное удаление не работает", f"осталось {count} записей")

        await conn.close()

    except Exception as e:
        fail("Тест логики пропусков", str(e)[:80])


# ═══════════════════════════════════════════════════
# 5. ОХРАННИКИ
# ═══════════════════════════════════════════════════
async def test_guards():
    section("5. УПРАВЛЕНИЕ ОХРАННИКАМИ")
    try:
        conn = await asyncpg.connect(DATABASE_URL.replace("localhost", "127.0.0.1"))

        # Добавление охранника
        guard_id = await conn.fetchval(
            "INSERT INTO guards (full_name, telegram_id, phone, active) "
            "VALUES ('Тест Охранник', 999999999, '+79990000002', TRUE) RETURNING id"
        )
        ok("Добавление охранника")

        # Проверка уникальности telegram_id
        try:
            await conn.execute(
                "INSERT INTO guards (full_name, telegram_id) VALUES ('Дубль', 999999999)"
            )
            fail("Уникальность telegram_id не проверяется")
        except:
            ok("Уникальность telegram_id охранника")

        # Блокировка
        await conn.execute("UPDATE guards SET active=FALSE WHERE id=$1", guard_id)
        row = await conn.fetchrow("SELECT active FROM guards WHERE id=$1", guard_id)
        if not row["active"]: ok("Блокировка охранника")
        else: fail("Блокировка не сработала")

        # Проверка доступа заблокированного
        row = await conn.fetchrow(
            "SELECT * FROM guards WHERE telegram_id=999999999 AND active=TRUE"
        )
        if not row: ok("Заблокированный охранник не получает доступ")
        else: fail("Заблокированный охранник имеет доступ")

        # Удаление
        await conn.execute("DELETE FROM guards WHERE id=$1", guard_id)
        ok("Удаление охранника")

        await conn.close()

    except Exception as e:
        fail("Тест охранников", str(e)[:80])


# ═══════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════
async def main():
    import urllib3
    urllib3.disable_warnings()

    print(f"\n{BOLD}{'═'*50}")
    print(f"  АВТОТЕСТЫ — КП Петровское Парк")
    print(f"  {date.today().strftime('%d.%m.%Y')}")
    print(f"{'═'*50}{RESET}")

    await test_database()
    await test_web()
    await test_bots()
    await test_passes_logic()
    await test_guards()

    # Итог
    total = passed + failed + warnings
    print(f"\n{BOLD}{'═'*50}{RESET}")
    print(f"{BOLD}  ИТОГО: {total} тестов{RESET}")
    print(f"  {GREEN}✔ Пройдено:  {passed}{RESET}")
    print(f"  {RED}✘ Провалено: {failed}{RESET}")
    print(f"  {YELLOW}⚠ Внимание:  {warnings}{RESET}")
    print(f"{BOLD}{'═'*50}{RESET}\n")

    if failed > 0:
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
