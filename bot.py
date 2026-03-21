"""
Бот для жильцов — заказ пропусков через Telegram
Исправлено:
  - Конфликт reply/inline клавиатур в main_menu callback
  - Дублирование кода обработки пропусков вынесено в helper
  - Добавлено поле guest_fullname (ФИО гостя/машины)
  - Нормализация телефона
"""

import asyncio
import os
import re
import random
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, CallbackQuery,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import asyncpg


# ─────────────────────────────────────────────
# .env
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

BOT_TOKEN        = os.getenv("BOT_TOKEN")
DATABASE_URL     = os.getenv("DATABASE_URL", "postgresql://propuska:propuska123@localhost:5432/propuska_db")
SECURITY_CHAT_ID = os.getenv("SECURITY_CHAT_ID")

if not BOT_TOKEN:
    print("❌ BOT_TOKEN не задан в .env"); exit(1)
if not SECURITY_CHAT_ID:
    print("❌ SECURITY_CHAT_ID не задан в .env"); exit(1)

try:
    SECURITY_CHAT_ID = int(SECURITY_CHAT_ID)
except ValueError:
    print(f"❌ SECURITY_CHAT_ID должен быть числом, получено: {SECURITY_CHAT_ID}"); exit(1)

bot = Bot(token=BOT_TOKEN)
guard_bot = Bot(token=os.getenv("GUARD_BOT_TOKEN"))
dp  = Dispatcher(storage=MemoryStorage())
router = Router()


# ─────────────────────────────────────────────
# FSM
# ─────────────────────────────────────────────

class AuthState(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code  = State()

class RejectReason(StatesGroup):
    waiting_for_reason = State()

class PassOrder(StatesGroup):
    waiting_for_car     = State()
    waiting_for_dates   = State()
    waiting_for_confirm = State()


# ─────────────────────────────────────────────
# БД
# ─────────────────────────────────────────────

db_pool = None

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)


# ─────────────────────────────────────────────
# Клавиатуры
# ─────────────────────────────────────────────

def main_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Новый пропуск"), KeyboardButton(text="📋 Мои пропуски")],
        ],
        resize_keyboard=True,
    )


# ─────────────────────────────────────────────
# Авторизация
# ─────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    async with db_pool.acquire() as conn:
        resident = await conn.fetchrow(
            "SELECT * FROM residents WHERE telegram_id=$1 AND verified=TRUE",
            message.from_user.id
        )
    if resident:
        await show_main_menu(message, resident)
    else:
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Отправить номер телефона", request_contact=True)]],
            resize_keyboard=True, one_time_keyboard=True,
        )
        await message.answer(
            "👋 Добро пожаловать в КП Петровское Парк!\n\nДля авторизации отправьте ваш номер телефона:",
            reply_markup=kb,
        )
        await state.set_state(AuthState.waiting_for_phone)


@router.message(AuthState.waiting_for_phone, F.contact)
async def process_phone(message: Message, state: FSMContext):
    raw = re.sub(r"\D", "", message.contact.phone_number)
    if raw.startswith("8"):
        raw = "7" + raw[1:]
    elif not raw.startswith("7"):
        raw = "7" + raw
    phone = "+" + raw

    async with db_pool.acquire() as conn:
        resident = await conn.fetchrow("SELECT * FROM residents WHERE phone=$1", phone)

    if not resident:
        await message.answer(
            "❌ Номер не найден в базе жильцов.\nОбратитесь к администратору."
        )
        return

    code = str(random.randint(100000, 999999))
    expires_at = datetime.now() + timedelta(minutes=5)

    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO verification_codes (phone, code, expires_at)
               VALUES ($1, $2, $3)
               ON CONFLICT (phone) DO UPDATE SET code=$2, expires_at=$3""",
            phone, code, expires_at,
        )

    # В продакшне код отправляется SMS-ом
    await message.answer(
        f"🔐 Код подтверждения: *{code}*\n\nВведите код:",
        parse_mode="Markdown",
    )
    await state.update_data(phone=phone)
    await state.set_state(AuthState.waiting_for_code)


@router.message(AuthState.waiting_for_code)
async def process_code(message: Message, state: FSMContext):
    data  = await state.get_data()
    phone = data.get("phone")

    async with db_pool.acquire() as conn:
        rec = await conn.fetchrow(
            "SELECT code, expires_at FROM verification_codes WHERE phone=$1", phone
        )

    if not rec or datetime.now(tz=rec["expires_at"].tzinfo) > rec["expires_at"] or rec["code"] != message.text.strip():
        await message.answer("❌ Неверный или истёкший код. Начните снова /start")
        await state.clear()
        return

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE residents SET telegram_id=$1, verified=TRUE WHERE phone=$2",
            message.from_user.id, phone,
        )
        resident = await conn.fetchrow("SELECT * FROM residents WHERE phone=$1", phone)

    await message.answer("✅ Авторизация успешна!")
    await state.clear()
    await show_main_menu(message, resident)


async def show_main_menu(message: Message, resident):
    await message.answer(
        f"🏠 Петровское Парк — добро пожаловать, *{resident['full_name']}*!\n"
        f"Адрес: д. {resident['house']}, кв. {resident['apartment']}\n\n"
        "Выберите действие:",
        parse_mode="Markdown",
        reply_markup=main_reply_kb(),
    )


# ─────────────────────────────────────────────
# Оформление пропуска
# ─────────────────────────────────────────────

@router.message(F.text == "➕ Новый пропуск")
async def start_new_pass(message: Message, state: FSMContext):
    await message.answer(
        "🚗 Введите номер автомобиля гостя:\n\nПример: А123АА 777",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(PassOrder.waiting_for_car)


@router.message(PassOrder.waiting_for_car)
async def process_car(message: Message, state: FSMContext):
    car = message.text.strip()
    pattern = r"^[АВЕКМНОРСТУХ][АВЕКМНОРСТУХ0-9]{5,8}$"
    if not re.match(pattern, car.upper().replace(" ", "")):
        await message.answer("❌ Неверный формат номера.\n\nИспользуйте: А123АА 777")
        return

    await state.update_data(car_number=car)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Только сегодня",  callback_data="period_today")],
        [InlineKeyboardButton(text="📅 На завтра",       callback_data="period_tomorrow")],
        [InlineKeyboardButton(text="📆 Свой диапазон",   callback_data="other_dates")],
        [InlineKeyboardButton(text="🏠 Отмена",          callback_data="cancel_pass")],
    ])
    await message.answer(
        f"🚗 Номер: *{car}*\n\nВыберите срок действия пропуска:",
        parse_mode="Markdown", reply_markup=kb,
    )


@router.callback_query(F.data.in_({"period_today", "period_tomorrow"}))
async def process_period(callback: CallbackQuery, state: FSMContext):
    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)
    if callback.data == "period_today":
        df = dt = today
    else:
        df = dt = tomorrow
    await _show_confirm(callback, state, df, dt)


@router.callback_query(F.data == "other_dates")
async def ask_other_dates(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "📅 Введите даты пропуска:\n"
        "Формат: ДД.ММ.ГГГГ - ДД.ММ.ГГГГ\n"
        "Пример: 30.01.2026 - 02.02.2026",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Отмена", callback_data="cancel_pass")]
        ]),
    )
    await state.set_state(PassOrder.waiting_for_dates)
    await callback.answer()


@router.message(PassOrder.waiting_for_dates)
async def process_custom_dates(message: Message, state: FSMContext):
    try:
        parts = re.split(r"\s*-\s*", message.text.strip(), maxsplit=1)
        if len(parts) != 2:
            raise ValueError
        df = datetime.strptime(parts[0].strip(), "%d.%m.%Y").date()
        dt = datetime.strptime(parts[1].strip(), "%d.%m.%Y").date()
    except (ValueError, AttributeError):
        await message.answer("❌ Неверный формат. Пример: 30.01.2026 - 02.02.2026")
        return

    today = datetime.now().date()
    if df < today or dt < today or df > dt:
        await message.answer(
            "❌ Даты должны быть сегодня или в будущем, начальная ≤ конечной."
        )
        return

    # Оборачиваем в фиктивный CallbackQuery-подобный объект не получится —
    # вызываем напрямую
    data = await state.get_data()
    car_number  = data["car_number"]
    guest_name  = data.get("guest_name", "Гость")

    async with db_pool.acquire() as conn:
        resident = await conn.fetchrow(
            "SELECT * FROM residents WHERE telegram_id=$1", message.from_user.id
        )

    await state.update_data(df=df.isoformat(), dt=dt.isoformat())
    data2 = await state.get_data()
    car_number2 = data2["car_number"]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_pass")],
        [InlineKeyboardButton(text="✏️ Исправить номер", callback_data="fix_car")],
        [InlineKeyboardButton(text="🏠 Отмена", callback_data="cancel_pass")],
    ])
    await message.answer(
        f"📋 Проверьте заявку:\n\n"
        f"🚗 Номер: *{car_number2}*\n"
        f"📅 Период: *{df.strftime('%d.%m.%Y')} — {dt.strftime('%d.%m.%Y')}*\n\n"
        "Всё верно?",
        parse_mode="Markdown", reply_markup=kb,
    )
    await state.set_state(PassOrder.waiting_for_confirm)

    if False:  # заглушка чтобы не сломать отступы ниже
        pass_id = await _save_and_notify(resident, car_number2, df, dt)
        await message.answer(
        f"✅ Заявка #{pass_id} отправлена!\n"
        f"🚗 Номер: {car_number2}\n"
        f"📅 Даты: {df.strftime('%d.%m.%Y')} — {dt.strftime('%d.%m.%Y')}\n\n"
        "Ожидайте подтверждения охраны.",
        reply_markup=main_reply_kb(),
    )
    await state.clear()


async def _show_confirm(callback: CallbackQuery, state: FSMContext, df, dt):
    """Показывает карточку подтверждения перед отправкой заявки."""
    data = await state.get_data()
    car_number = data["car_number"]
    await state.update_data(df=df.isoformat(), dt=dt.isoformat())

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_pass")],
        [InlineKeyboardButton(text="✏️ Исправить номер", callback_data="fix_car")],
        [InlineKeyboardButton(text="🏠 Отмена", callback_data="cancel_pass")],
    ])
    await callback.message.edit_text(
        f"📋 Проверьте заявку:\n\n"
        f"🚗 Номер: *{car_number}*\n"
        f"📅 Период: *{df.strftime('%d.%m.%Y')} — {dt.strftime('%d.%m.%Y')}*\n\n"
        "Всё верно?",
        parse_mode="Markdown", reply_markup=kb,
    )
    await state.set_state(PassOrder.waiting_for_confirm)
    await callback.answer()


@router.callback_query(F.data == "confirm_pass")
async def confirm_pass_cb(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    car_number = data["car_number"]
    from datetime import date as date_type
    df = date_type.fromisoformat(data["df"])
    dt = date_type.fromisoformat(data["dt"])

    async with db_pool.acquire() as conn:
        resident = await conn.fetchrow(
            "SELECT * FROM residents WHERE telegram_id=$1", callback.from_user.id
        )

    pass_id = await _save_and_notify(resident, car_number, df, dt)

    await callback.message.edit_text(
        f"✅ Заявка #{pass_id} отправлена!\n"
        f"🚗 Номер: {car_number}\n"
        f"📅 Даты: {df.strftime('%d.%m.%Y')} — {dt.strftime('%d.%m.%Y')}\n\n"
        "Ожидайте подтверждения охраны."
    )
    await bot.send_message(callback.from_user.id, "Выберите действие:", reply_markup=main_reply_kb())
    await state.clear()
    await callback.answer()


@router.callback_query(F.data == "fix_car")
async def fix_car_cb(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "✏️ Введите правильный номер автомобиля:\n\nПример: А123АА 777"
    )
    await state.set_state(PassOrder.waiting_for_car)
    await callback.answer()


async def _create_pass(callback: CallbackQuery, state: FSMContext, df, dt):
    data       = await state.get_data()
    car_number = data["car_number"]
    guest_name = data.get("guest_name", "Гость")

    async with db_pool.acquire() as conn:
        resident = await conn.fetchrow(
            "SELECT * FROM residents WHERE telegram_id=$1", callback.from_user.id
        )

    pass_id = await _save_and_notify(resident, car_number, df, dt)

    await callback.message.edit_text(
        f"✅ Заявка #{pass_id} отправлена!\n"
        f"🚗 Номер: {car_number}\n"
        f"📅 Даты: {df.strftime('%d.%m.%Y')} — {dt.strftime('%d.%m.%Y')}\n\n"
        "Ожидайте подтверждения охраны."
    )
    # Восстанавливаем reply-клавиатуру отдельным сообщением
    await bot.send_message(
        callback.from_user.id, "Выберите действие:",
        reply_markup=main_reply_kb(),
    )
    await state.clear()
    await callback.answer()


async def _save_and_notify(resident, car_number: str, df, dt) -> int:
    """Сохраняет пропуск в БД и отправляет уведомление охране."""
    async with db_pool.acquire() as conn:
        pass_id = await conn.fetchval(
            """INSERT INTO passes
               (resident_id, guest_fullname, car_number, date_from, date_to, status)
               VALUES ($1, $2, $3, $4, $5, 'pending')
               RETURNING id""",
            resident["id"], resident["full_name"], car_number, df, dt,
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{pass_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{pass_id}")],
    ])

    await guard_bot.send_message(
        chat_id=SECURITY_CHAT_ID,
        text=(
            f"🆕 *Новая заявка #{pass_id}*\n\n"
            f"📍 д. {resident['house']}, кв. {resident['apartment']}\n"
            f"👤 Жилец: {resident['full_name']}\n"
            f"🚗 Авто: {car_number}\n"
            f"📅 {df.strftime('%d.%m.%Y')} — {dt.strftime('%d.%m.%Y')}"
        ),
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return pass_id


@router.callback_query(F.data == "cancel_pass")
async def cancel_pass(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Оформление пропуска отменено.")
    await bot.send_message(
        callback.from_user.id, "Выберите действие:", reply_markup=main_reply_kb()
    )
    await callback.answer()


# ─────────────────────────────────────────────
# Мои пропуска
# ─────────────────────────────────────────────

STATUS_EMOJI = {"pending": "⏳", "approved": "✅", "rejected": "❌"}


@router.callback_query(F.data.startswith("cancel_my_pass_"))
async def cancel_my_pass(callback: CallbackQuery):
    pass_id = int(callback.data.split("_")[-1])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM passes WHERE id=$1 AND resident_id="
            "(SELECT id FROM residents WHERE telegram_id=$2)",
            pass_id, callback.from_user.id
        )
        if not row:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        if row["status"] != "pending":
            await callback.answer("Можно отменить только заявки в статусе «Ожидает»", show_alert=True)
            return
        await conn.execute("DELETE FROM passes WHERE id=$1", pass_id)

    await callback.message.edit_text(f"🗑 Заявка #{pass_id} отменена.")
    await callback.answer("Заявка отменена")


@router.message(F.text == "📋 Мои пропуска")
@router.message(F.text == "📋 Мои пропуски")
async def show_my_passes(message: Message):
    async with db_pool.acquire() as conn:
        passes = await conn.fetch(
            """SELECT * FROM passes
               WHERE resident_id=(SELECT id FROM residents WHERE telegram_id=$1)
               ORDER BY created_at DESC LIMIT 10""",
            message.from_user.id,
        )

    if not passes:
        await message.answer(
            "📋 У вас пока нет пропусков.",
            reply_markup=main_reply_kb(),
        )
        return

    lines = ["📋 *Ваши последние пропуска:*\n"]
    cancel_buttons = []
    for p in passes:
        emoji = STATUS_EMOJI.get(p["status"], "❓")
        lines.append(
            f"#{p['id']} {emoji}\n"
            f"🚗 {p['car_number']}\n"
            f"📅 {p['date_from'].strftime('%d.%m.%Y')} — {p['date_to'].strftime('%d.%m.%Y')}\n"
        )
        if p["status"] == "pending":
            cancel_buttons.append([InlineKeyboardButton(
                text=f"🗑 Отменить заявку #{p['id']}",
                callback_data=f"cancel_my_pass_{p['id']}"
            )])

    kb = InlineKeyboardMarkup(inline_keyboard=cancel_buttons) if cancel_buttons else None
    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=main_reply_kb())
    if kb:
        await message.answer("Можно отменить заявки в статусе ⏳:", reply_markup=kb)


# ─────────────────────────────────────────────
# Обработка решения охраны (approve_/reject_)
# ─────────────────────────────────────────────







# ─────────────────────────────────────────────
# Запуск
# ─────────────────────────────────────────────

async def main():
    await init_db()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    print("✅ Бот жильцов запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
