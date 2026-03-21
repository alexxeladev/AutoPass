"""
Панель охраны — веб-приложение
FastAPI + asyncpg + JWT (httponly cookies) + bcrypt
"""

import os
import re
import asyncpg
from datetime import datetime, timedelta, date
from typing import Optional

from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.security import HTTPBearer

from jose import JWTError, jwt
import bcrypt as _bcrypt_lib
from aiogram import Bot


# ─────────────────────────────────────────────
# Загрузка .env
# ─────────────────────────────────────────────

def load_env_file():
    env_file = ".env"
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ[key.strip()] = value.strip()

load_env_file()

# ─────────────────────────────────────────────
# Конфигурация
# ─────────────────────────────────────────────

DATABASE_URL  = os.getenv("DATABASE_URL", "postgresql://propuska:propuska123@localhost:5432/propuska_db")
BOT_TOKEN     = os.getenv("BOT_TOKEN")
SECURITY_CHAT_ID = os.getenv("SECURITY_CHAT_ID")

# JWT — ОБЯЗАТЕЛЬНО смените SECRET_KEY в продакшне!
SECRET_KEY    = os.getenv("JWT_SECRET_KEY", "CHANGE_ME_IN_PRODUCTION_USE_RANDOM_32_BYTES")
ALGORITHM     = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 8  # 8 часов

# ─────────────────────────────────────────────
# Утилиты безопасности
# ─────────────────────────────────────────────

def verify_password(plain: str, hashed: str) -> bool:
    return _bcrypt_lib.checkpw(plain.encode(), hashed.encode())


def hash_password(password: str) -> str:
    return _bcrypt_lib.hashpw(password.encode(), _bcrypt_lib.gensalt()).decode()


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


# ─────────────────────────────────────────────
# Приложение
# ─────────────────────────────────────────────

app = FastAPI(title="Панель охраны")
templates = Jinja2Templates(directory="templates")

try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except Exception:
    pass  # static папка может отсутствовать

# Telegram-бот для уведомлений
bot_instance = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
GUARD_BOT_TOKEN = os.getenv("GUARD_BOT_TOKEN")
SECURITY_CHAT_ID = int(os.getenv("SECURITY_CHAT_ID", "0"))
guard_bot_instance = Bot(token=GUARD_BOT_TOKEN) if GUARD_BOT_TOKEN else None

# ─────────────────────────────────────────────
# БД
# ─────────────────────────────────────────────

db_pool: Optional[asyncpg.Pool] = None


@app.on_event("startup")
async def startup():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    await _ensure_admin_user()


@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()


async def _ensure_admin_user():
    """Создаёт дефолтного admin если таблица users пуста."""
    async with db_pool.acquire() as conn:
        # Создаём таблицу users если не существует
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id          SERIAL PRIMARY KEY,
                username    TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role        TEXT NOT NULL DEFAULT 'guard',
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        count = await conn.fetchval("SELECT COUNT(*) FROM users")
        if count == 0:
            hashed = hash_password("admin123")
            await conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES ($1, $2, $3)",
                "admin", hashed, "administrator"
            )
            print("✅ Создан пользователь admin / admin123  — СМЕНИТЕ ПАРОЛЬ!")


# ─────────────────────────────────────────────
# Auth dependency
# ─────────────────────────────────────────────

async def get_current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get("access_token")
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    return {"username": payload.get("sub"), "role": payload.get("role", "guard")}


async def require_user(request: Request) -> dict:
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_302_FOUND,
                            headers={"Location": "/login"})
    return user


def auth_redirect(request: Request):
    """Вспомогательная — возвращает редирект если не авторизован, иначе None."""
    token = request.cookies.get("access_token")
    if not token or not decode_token(token):
        return RedirectResponse(url="/login", status_code=302)
    return None


# ─────────────────────────────────────────────
# Валидация
# ─────────────────────────────────────────────

def validate_phone(phone: str) -> bool:
    return bool(re.match(r"^\+7\d{10}$", phone))


def validate_car_number(number: str) -> bool:
    n = number.upper().replace(" ", "")
    return bool(re.match(r"^[АВЕКМНОРСТУХ]\d{3}[АВЕКМНОРСТУХ]{2}\d{2,3}$", n))


def format_car_number(number: str) -> str:
    if not number:
        return ""
    n = number.upper().replace(" ", "")
    if len(n) >= 6:
        return f"{n[0]}{n[1:4]}{n[4:6]} {n[6:]}"
    return n


# ─────────────────────────────────────────────
# Уведомления в бот
# ─────────────────────────────────────────────

async def notify_resident(pass_id: int, action: str):
    if not bot_instance:
        return
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT p.*, r.telegram_id, r.full_name as resident_name
                   FROM passes p JOIN residents r ON p.resident_id = r.id
                   WHERE p.id = $1""",
                pass_id
            )
        if not row or not row["telegram_id"]:
            return
        if action == "approve":
            text = (
                f"✅ Пропуск #{pass_id} одобрен!\n"
                f"Авто: {row['car_number']}\n"
                f"Даты: {row['date_from'].strftime('%d.%m.%Y')} — {row['date_to'].strftime('%d.%m.%Y')}"
            )
        else:
            text = f"❌ Пропуск #{pass_id} отклонён."
        await bot_instance.send_message(chat_id=row["telegram_id"], text=text)

        # Уведомляем охрану что заявка обработана через веб
        if guard_bot_instance and SECURITY_CHAT_ID:
            if action == "approve":
                guard_text = (
                    f"✅ Заявка #{pass_id} одобрена через веб-панель\n"
                    f"🚗 {row['car_number']} — {row['resident_name']}\n"
                    f"📅 {row['date_from'].strftime('%d.%m.%Y')} — {row['date_to'].strftime('%d.%m.%Y')}"
                )
            else:
                guard_text = (
                    f"❌ Заявка #{pass_id} отклонена через веб-панель\n"
                    f"🚗 {row['car_number']} — {row['resident_name']}"
                )
            await guard_bot_instance.send_message(chat_id=SECURITY_CHAT_ID, text=guard_text)
    except Exception as e:
        print(f"Ошибка уведомления: {e}")


# ═══════════════════════════════════════════════════════════════
# МАРШРУТЫ
# ═══════════════════════════════════════════════════════════════

# ── Корень ──────────────────────────────────────────────────────

@app.get("/")
async def root_redirect():
    return RedirectResponse(url="/dashboard", status_code=302)


# ── Авторизация ──────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = await get_current_user(request)
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE username = $1", username)

    error = None
    if not row or not verify_password(password, row["password_hash"]):
        error = "Неверный логин или пароль"

    if error:
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": error}
        )

    token = create_access_token({"sub": row["username"], "role": row["role"]})
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(
        "access_token", token,
        httponly=True, max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60, samesite="lax"
    )
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("access_token")
    return response


# ── Дашборд ──────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    redir = auth_redirect(request)
    if redir:
        return redir

    today = date.today()

    async with db_pool.acquire() as conn:
        active_passes = await conn.fetch(
            """SELECT p.*, r.house, r.apartment, r.full_name as resident_name
               FROM passes p JOIN residents r ON p.resident_id = r.id
               WHERE date(p.date_from) <= $1 AND date(p.date_to) >= $1
                 AND p.status = 'approved'
               ORDER BY p.created_at DESC""",
            today
        )
        pending_requests = await conn.fetch(
            """SELECT p.*, r.house, r.apartment, r.full_name as resident_name
               FROM passes p JOIN residents r ON p.resident_id = r.id
               WHERE p.status = 'pending'
               ORDER BY p.created_at ASC"""
        )
        stats = await conn.fetchrow(
            """SELECT
                 COUNT(*) as total_today,
                 COUNT(*) FILTER (WHERE p.status = 'approved') as approved_today,
                 COUNT(*) FILTER (WHERE p.status = 'rejected') as rejected_today,
                 COUNT(*) FILTER (WHERE p.status = 'pending')  as pending_total
               FROM passes p
               WHERE date(p.date_from) <= $1 AND date(p.date_to) >= $1""",
            today
        )

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "active_passes": active_passes,
        "pending_requests": pending_requests,
        "stats": stats,
        "today": today.strftime("%d.%m.%Y"),
    })


# ── Заявки ───────────────────────────────────────────────────────

@app.get("/requests", response_class=HTMLResponse)
async def requests_page(request: Request):
    redir = auth_redirect(request)
    if redir:
        return redir

    async with db_pool.acquire() as conn:
        passes = await conn.fetch(
            """SELECT p.*, r.house, r.apartment, r.full_name as resident_name
               FROM passes p JOIN residents r ON p.resident_id = r.id
               WHERE p.status = 'pending'
               ORDER BY p.created_at ASC"""
        )

    return templates.TemplateResponse("requests.html", {
        "request": request, "passes": passes, "count": len(passes)
    })


# ── История ───────────────────────────────────────────────────────

@app.get("/history", response_class=HTMLResponse)
async def history_page(
    request: Request,
    search: str = "",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    redir = auth_redirect(request)
    if redir:
        return redir

    # Дефолтный период — последние 7 дней
    try:
        df = datetime.strptime(date_from, "%Y-%m-%d").date() if date_from else date.today() - timedelta(days=7)
        dt = datetime.strptime(date_to,   "%Y-%m-%d").date() if date_to   else date.today()
    except ValueError:
        df = date.today() - timedelta(days=7)
        dt = date.today()

    date_from = df.strftime("%Y-%m-%d")
    date_to   = dt.strftime("%Y-%m-%d")

    query = """
        SELECT p.*, r.house, r.apartment, r.full_name as resident_name
        FROM passes p JOIN residents r ON p.resident_id = r.id
        WHERE date(p.date_from) >= $1 AND date(p.date_to) <= $2
    """
    params: list = [df, dt]

    if search:
        query += " AND (UPPER(p.car_number) LIKE $3 OR UPPER(r.full_name) LIKE $3 OR r.house LIKE $3)"
        params.append(f"%{search.upper()}%")

    query += " ORDER BY p.created_at DESC"

    async with db_pool.acquire() as conn:
        passes = await conn.fetch(query, *params)

    return templates.TemplateResponse("history.html", {
        "request": request, "passes": passes,
        "date_from": date_from, "date_to": date_to, "search": search,
    })


# ── Проверка пропуска ─────────────────────────────────────────────

@app.get("/check", response_class=HTMLResponse)
async def check_page(request: Request):
    redir = auth_redirect(request)
    if redir:
        return redir
    return templates.TemplateResponse("check.html", {"request": request})


@app.post("/api/check-car")
async def api_check_car(car_number: str = Form(...)):
    normalized = car_number.upper().replace(" ", "")

    async with db_pool.acquire() as conn:
        # Ищем активный пропуск
        row = await conn.fetchrow(
            """SELECT p.*, r.house, r.apartment, r.full_name as resident_name
               FROM passes p JOIN residents r ON p.resident_id = r.id
               WHERE UPPER(REPLACE(p.car_number,' ','')) = $1
                 AND p.status = 'approved'
                 AND date(p.date_from) <= CURRENT_DATE
                 AND date(p.date_to)   >= CURRENT_DATE
               ORDER BY p.date_to DESC LIMIT 1""",
            normalized
        )
        if row:
            return {
                "found": True,
                "is_resident": False,
                "pass": {
                    "id": row["id"],
                    "car_number": format_car_number(row["car_number"]),
                    "guest_fullname": row["guest_fullname"],
                    "date_from": row["date_from"].strftime("%d.%m.%Y"),
                    "date_to":   row["date_to"].strftime("%d.%m.%Y"),
                    "resident_name": row["resident_name"],
                    "house": row["house"],
                    "apartment": row["apartment"],
                    "status": row["status"],
                },
            }

        # Проверяем, не жилец ли это
        try:
            resident = await conn.fetchrow(
                """SELECT r.full_name as resident_name, r.house, r.apartment
                   FROM residents r JOIN cars c ON r.id = c.resident_id
                   WHERE UPPER(REPLACE(c.car_number,' ','')) = $1""",
                normalized
            )
            if resident:
                return {
                    "found": True,
                    "is_resident": True,
                    "resident": {
                        "car_number": format_car_number(car_number),
                        "resident_name": resident["resident_name"],
                        "house": resident["house"],
                        "apartment": resident["apartment"],
                    },
                }
        except Exception:
            pass  # таблица cars может отсутствовать

    return {
        "found": False,
        "message": f"Нет активного пропуска для {format_car_number(car_number)}",
    }


# ── Одобрение / отклонение (HTMX) ────────────────────────────────

@app.post("/api/pass/{pass_id}/approve")
async def approve_pass(request: Request, pass_id: int):
    redir = auth_redirect(request)
    if redir:
        return HTMLResponse("<div class='alert'>Требуется авторизация</div>")

    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE passes SET status='approved' WHERE id=$1", pass_id)

    await notify_resident(pass_id, "approve")
    return HTMLResponse("")  # HTMX удалит строку (hx-swap="delete")


@app.post("/api/pass/{pass_id}/reject")
async def reject_pass(request: Request, pass_id: int):
    redir = auth_redirect(request)
    if redir:
        return HTMLResponse("<div class='alert'>Требуется авторизация</div>")

    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE passes SET status='rejected' WHERE id=$1", pass_id)

    await notify_resident(pass_id, "reject")
    return HTMLResponse("")


# ── API для счётчика ──────────────────────────────────────────────

@app.get("/api/pending-count")
async def pending_count(request: Request):
    redir = auth_redirect(request)
    if redir:
        return JSONResponse({"count": 0})

    async with db_pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM passes WHERE status='pending'")
    return JSONResponse({"count": count})


# ── Жильцы ───────────────────────────────────────────────────────

@app.get("/residents", response_class=HTMLResponse)
async def residents_page(request: Request, search: str = "", error: str = ""):
    redir = auth_redirect(request)
    if redir:
        return redir

    async with db_pool.acquire() as conn:
        # Проверяем таблицу cars
        cars_exists = await conn.fetchval(
            "SELECT EXISTS(SELECT FROM information_schema.tables WHERE table_name='cars')"
        )

        if cars_exists:
            base = """
                SELECT r.*, STRING_AGG(c.car_number, ', ') as car_numbers
                FROM residents r LEFT JOIN cars c ON r.id = c.resident_id
            """
            group = " GROUP BY r.id"
        else:
            base = "SELECT r.*, NULL as car_numbers FROM residents r"
            group = ""

        params = []
        where = ""
        if search:
            where = " WHERE r.full_name ILIKE $1 OR r.phone LIKE $1 OR r.house LIKE $1 OR r.apartment LIKE $1"
            params.append(f"%{search}%")

        order = " ORDER BY r.house, r.apartment"
        residents = await conn.fetch(base + where + group + order, *params)

    return templates.TemplateResponse("residents.html", {
        "request": request, "residents": residents, "search": search, "error": error,
    })


@app.post("/residents/add")
async def add_resident(
    request: Request,
    full_name: str = Form(...),
    phone: str = Form(...),
    house: str = Form(...),
    apartment: str = Form(...),
    car_numbers: list[str] = Form(default=[]),
):
    redir = auth_redirect(request)
    if redir:
        return redir

    if not validate_phone(phone):
        return RedirectResponse(url="/residents?error=phone", status_code=303)

    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT id FROM residents WHERE phone=$1", phone)
        if existing:
            return RedirectResponse(url="/residents?error=exists", status_code=303)
        resident_id = await conn.fetchval(
            "INSERT INTO residents (house, apartment, full_name, phone) VALUES ($1,$2,$3,$4) RETURNING id",
            house, apartment, full_name, phone
        )
        for car in car_numbers:
            car = car.upper().replace(" ", "")
            if car and validate_car_number(car):
                await conn.execute(
                    "INSERT INTO cars (resident_id, car_number) VALUES ($1,$2) ON CONFLICT DO NOTHING",
                    resident_id, car
                )

    return RedirectResponse(url="/residents", status_code=303)


@app.post("/residents/update/{resident_id}")
async def update_resident(
    request: Request,
    resident_id: int,
    full_name: str = Form(...),
    phone: str = Form(...),
    house: str = Form(...),
    apartment: str = Form(...),
):
    redir = auth_redirect(request)
    if redir:
        return redir

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE residents SET full_name=$1, phone=$2, house=$3, apartment=$4 WHERE id=$5",
            full_name, phone, house, apartment, resident_id
        )

    return RedirectResponse(url="/residents", status_code=303)


@app.post("/residents/delete/{resident_id}")
async def delete_resident(request: Request, resident_id: int):
    redir = auth_redirect(request)
    if redir:
        return redir

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM residents WHERE id=$1", resident_id)

    return RedirectResponse(url="/residents", status_code=303)


# ── Отчёты ───────────────────────────────────────────────────────

@app.get("/reports", response_class=HTMLResponse)
async def reports_page(
    request: Request,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    status:    Optional[str] = None,
):
    redir = auth_redirect(request)
    if redir:
        return redir

    try:
        df = datetime.strptime(date_from, "%Y-%m-%d").date() if date_from else date.today() - timedelta(days=30)
        dt = datetime.strptime(date_to,   "%Y-%m-%d").date() if date_to   else date.today()
    except ValueError:
        df = date.today() - timedelta(days=30)
        dt = date.today()

    date_from = df.strftime("%Y-%m-%d")
    date_to   = dt.strftime("%Y-%m-%d")

    query = """
        SELECT p.*, r.house, r.apartment, r.full_name as resident_name
        FROM passes p JOIN residents r ON p.resident_id = r.id
        WHERE date(p.date_from) >= $1 AND date(p.date_to) <= $2
    """
    params: list = [df, dt]

    if status:
        query += " AND p.status = $3"
        params.append(status)

    query += " ORDER BY p.created_at DESC"

    async with db_pool.acquire() as conn:
        passes = await conn.fetch(query, *params)
        stats  = await conn.fetchrow(
            """SELECT
                 COUNT(*) as total,
                 COUNT(*) FILTER (WHERE p.status='approved') as approved,
                 COUNT(*) FILTER (WHERE p.status='rejected') as rejected,
                 COUNT(*) FILTER (WHERE p.status='pending')  as pending
               FROM passes p
               WHERE date(p.date_from) >= $1 AND date(p.date_to) <= $2""",
            df, dt
        )

    return templates.TemplateResponse("reports.html", {
        "request": request,
        "passes": passes, "stats": stats,
        "date_from": date_from, "date_to": date_to, "status": status or "",
    })


# ── Пользователи / смена пароля ───────────────────────────────────

# ── Охранники ─────────────────────────────────────────────────

@app.get("/guards", response_class=HTMLResponse)
async def guards_page(request: Request):
    redir = auth_redirect(request)
    if redir: return redir
    async with db_pool.acquire() as conn:
        guards = await conn.fetch("SELECT * FROM guards ORDER BY created_at DESC")
    token = request.cookies.get("access_token")
    user = decode_token(token) if token else None
    return templates.TemplateResponse("guards.html", {"request": request, "guards": guards, "user": user})

@app.post("/guards/add")
async def guard_add(request: Request,
    full_name: str = Form(...),
    telegram_id: str = Form(default=""),
    phone: str = Form(default="")):
    redir = auth_redirect(request)
    if redir: return redir
    tg_id = int(telegram_id.strip()) if telegram_id.strip() else None
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO guards (full_name, telegram_id, phone) VALUES ($1, $2, $3)",
            full_name.strip(), tg_id, phone.strip() or None
        )
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/guards", status_code=303)

@app.post("/guards/{guard_id}/toggle")
async def guard_toggle(request: Request, guard_id: int):
    redir = auth_redirect(request)
    if redir: return redir
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE guards SET active = NOT active WHERE id=$1", guard_id)
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/guards", status_code=303)

@app.post("/guards/{guard_id}/delete")
async def guard_delete(request: Request, guard_id: int):
    redir = auth_redirect(request)
    if redir: return redir
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM guards WHERE id=$1", guard_id)
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/guards", status_code=303)


@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    redir = auth_redirect(request)
    if redir:
        return redir

    user = await get_current_user(request)
    return templates.TemplateResponse("users.html", {
        "request": request, "current_user": user
    })


@app.post("/users/change-password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password:     str = Form(...),
    confirm_password: str = Form(...),
):
    redir = auth_redirect(request)
    if redir:
        return redir

    user = await get_current_user(request)

    async def render_error(msg):
        return templates.TemplateResponse("users.html", {
            "request": request, "current_user": user, "error": msg
        })

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE username=$1", user["username"])

    if not row or not verify_password(current_password, row["password_hash"]):
        return await render_error("Неверный текущий пароль")

    if new_password != confirm_password:
        return await render_error("Новые пароли не совпадают")

    if len(new_password) < 8:
        return await render_error("Пароль должен быть не менее 8 символов")

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET password_hash=$1 WHERE username=$2",
            hash_password(new_password), user["username"]
        )

    return templates.TemplateResponse("users.html", {
        "request": request, "current_user": user,
        "success": "Пароль успешно изменён"
    })


@app.get("/api/passes/{status}")
async def get_passes_by_status(request: Request, status: str):
    redir = auth_redirect(request)
    if redir:
        return HTMLResponse("")

    if status not in ["approved", "rejected", "pending"]:
        return HTMLResponse("")

    from datetime import date as date_type
    today = date_type.today()
    async with db_pool.acquire() as conn:
        if status in ("approved", "rejected"):
            passes = await conn.fetch(
                """SELECT p.*, r.house, r.apartment, r.full_name as resident_name
                   FROM passes p JOIN residents r ON p.resident_id = r.id
                   WHERE p.status = $1
                     AND date(p.date_from) <= $2 AND date(p.date_to) >= $2
                   ORDER BY p.created_at DESC""",
                status, today
            )
        else:
            passes = await conn.fetch(
                """SELECT p.*, r.house, r.apartment, r.full_name as resident_name
                   FROM passes p JOIN residents r ON p.resident_id = r.id
                   WHERE p.status = $1
                   ORDER BY p.created_at DESC""",
                status
            )

    html = ""
    for p in passes:
        if status == "pending":
            action_td = f"""<td><div class="btn-group">
                <button class="btn btn-success btn-icon btn-sm"
                        hx-post="/api/pass/{p['id']}/approve"
                        hx-target="#row-{p['id']}" hx-swap="delete" title="Одобрить">
                    <i class="fas fa-check" style="font-size:11px"></i></button>
                <button class="btn btn-danger btn-icon btn-sm"
                        hx-post="/api/pass/{p['id']}/reject"
                        hx-target="#row-{p['id']}" hx-swap="delete" title="Отклонить">
                    <i class="fas fa-times" style="font-size:11px"></i></button>
            </div></td>"""
        elif status == "approved":
            action_td = f'<td><span class="badge badge-approved">Одобрен</span></td>'
        else:
            action_td = f'<td><span class="badge badge-rejected">Отклонён</span></td>'

        html += f"""
<tr id="row-{p['id']}">
    <td><span class="car-num">{p['car_number']}</span></td>
    <td style="font-size:12px; color:var(--text-2)">{p['guest_fullname']}<br>
        <span style="color:var(--text-3)">д.{p['house']}, кв.{p['apartment']}</span></td>
    <td class="mono" style="font-size:11.5px; color:var(--text-3)">
        {p['date_from'].strftime('%d.%m')}–{p['date_to'].strftime('%d.%m')}</td>
    {action_td}
</tr>"""

    if not html:
        html = '<tr><td colspan="4" style="text-align:center; padding:24px; color:var(--text-3)">Нет записей</td></tr>'

    return HTMLResponse(html)


@app.post("/residents/{resident_id}/cars/add")
async def add_car(request: Request, resident_id: int, car_number: str = Form(...)):
    redir = auth_redirect(request)
    if redir:
        return redir
    normalized = car_number.upper().replace(" ", "")
    if not validate_car_number(normalized):
        return RedirectResponse(url="/residents?error=car", status_code=303)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO cars (resident_id, car_number) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            resident_id, normalized
        )
    return RedirectResponse(url="/residents", status_code=303)


@app.post("/residents/{resident_id}/cars/delete")
async def delete_car(request: Request, resident_id: int, car_number: str = Form(...)):
    redir = auth_redirect(request)
    if redir:
        return redir
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM cars WHERE resident_id=$1 AND car_number=$2",
            resident_id, car_number.upper().replace(" ", "")
        )
    return RedirectResponse(url="/residents", status_code=303)
