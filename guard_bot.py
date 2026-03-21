"""
Бот охранника
Исправлено:
  - Сломанный SQL в статистике (date_from >= X AND date_to <= X — невозможно)
  - Статистика за сегодня теперь считает активные на сегодня пропуска
  - Статистика за 30 дней — все пропуска созданные за период
"""

import asyncio
import os
from datetime import date, timedelta

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
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

GUARD_BOT_TOKEN = os.getenv("GUARD_BOT_TOKEN")
DATABASE_URL    = os.getenv("DATABASE_URL", "postgresql://propuska:propuska123@localhost:5432/propuska_db")

if not GUARD_BOT_TOKEN:
    print("❌ GUARD_BOT_TOKEN не задан в .env"); exit(1)

bot = Bot(token=GUARD_BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())
router = Router()

db_pool = None


# ─────────────────────────────────────────────
# FSM
# ─────────────────────────────────────────────

class CheckState(StatesGroup):
    waiting_for_car = State()

class RejectReason(StatesGroup):
    waiting_for_reason = State()


# ─────────────────────────────────────────────
# БД
# ─────────────────────────────────────────────

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)


def format_car(number: str) -> str:
    if not number:
        return ""
    n = number.upper().replace(" ", "")
    return f"{n[0]}{n[1:4]}{n[4:6]} {n[6:]}" if len(n) >= 6 else n


# ─────────────────────────────────────────────
# Клавиатуры
# ─────────────────────────────────────────────

def main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Заявки"),    KeyboardButton(text="📅 Активные")],
            [KeyboardButton(text="🔍 Проверить"), KeyboardButton(text="📊 Статистика")],
        ],
        resize_keyboard=True,
    )

def inline_refresh_kb(action: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=action)],
    ])


# ─────────────────────────────────────────────
# Старт
# ─────────────────────────────────────────────

WELCOME = (
    "👮 КП Петровское Парк\n"
    "Добро пожаловать, охранник!\n\n"
    "Выберите действие:"
)

@router.message(Command("start"))
async def cmd_start(message: Message):
    async with db_pool.acquire() as conn:
        guard = await conn.fetchrow(
            "SELECT * FROM guards WHERE telegram_id=$1 AND active=TRUE",
            message.from_user.id
        )
    if not guard:
        await message.answer(
            "⛔️ Доступ запрещён.\n\n"
            "Ваш Telegram ID не найден в списке охранников.\n"
            "Обратитесь к администратору."
        )
        return
    await message.answer(
        f"👮 КП Петровское Парк\n"
        f"Добро пожаловать, {guard['full_name']}!\n\n"
        "Выберите действие:",
        reply_markup=main_kb()
    )


@router.callback_query(F.data == "menu")
async def menu(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(WELCOME, reply_markup=main_kb())


# ── Reply-кнопки ──────────────────────────────
@router.message(F.text == "📋 Заявки")
async def pending_reply(message: Message):
    await _send_pending(message)

@router.message(F.text == "📅 Активные")
async def active_reply(message: Message):
    await _send_active(message)

@router.message(F.text == "🔍 Проверить")
async def check_reply(message: Message, state: FSMContext):
    await message.answer("🔍 Введите номер автомобиля:\n\nПример: А123АА 777")
    await state.set_state(CheckState.waiting_for_car)

@router.message(F.text == "📊 Статистика")
async def stats_reply(message: Message):
    await _send_stats(message)


# ─────────────────────────────────────────────
# Нерассмотренные заявки
# ─────────────────────────────────────────────

async def _send_pending(target):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT p.*, r.house, r.apartment, r.full_name as resident_name
               FROM passes p JOIN residents r ON p.resident_id=r.id
               WHERE p.status='pending'
               ORDER BY p.created_at ASC LIMIT 15"""
        )
    if not rows:
        text = "📋 Нерассмотренных заявок нет."
    else:
        lines = [f"📋 Нерассмотренные заявки ({len(rows)}):\n"]
        for p in rows:
            lines.append(
                f"#{p['id']} 🚗 {p['car_number']}\n"
                f"   🏠 д.{p['house']}, кв.{p['apartment']} ({p['resident_name']})\n"
                f"   📅 {p['date_from'].strftime('%d.%m')} — {p['date_to'].strftime('%d.%m')}\n"
            )
        text = "\n".join(lines)
    await target.answer(text, reply_markup=inline_refresh_kb("pending"))

@router.callback_query(F.data.in_({"pending", "refresh_pending"}))
async def show_pending(callback: CallbackQuery):
    await _send_pending(callback.message)
    await callback.answer()


# ─────────────────────────────────────────────
# Активные пропуска
# ─────────────────────────────────────────────

async def _send_active(target):
    today = date.today()
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT p.*, r.house, r.apartment, r.full_name as resident_name
               FROM passes p JOIN residents r ON p.resident_id=r.id
               WHERE p.status='approved'
                 AND date(p.date_from) <= $1 AND date(p.date_to) >= $1
               ORDER BY p.date_to ASC LIMIT 15""",
            today
        )
    if not rows:
        text = f"📅 Активных пропусков на {today.strftime('%d.%m.%Y')} нет."
    else:
        lines = [f"📅 Активные пропуска на {today.strftime('%d.%m.%Y')} ({len(rows)}):\n"]
        for p in rows:
            lines.append(
                f"🚗 {format_car(p['car_number'])}\n"
                f"   🏠 д.{p['house']}, кв.{p['apartment']} ({p['resident_name']})\n"
                f"   📅 до {p['date_to'].strftime('%d.%m.%Y')}\n"
            )
        text = "\n".join(lines)
    await target.answer(text, reply_markup=inline_refresh_kb("active"))

@router.callback_query(F.data == "active")
async def show_active(callback: CallbackQuery):
    await _send_active(callback.message)
    await callback.answer()


# ─────────────────────────────────────────────
# Проверка пропуска
# ─────────────────────────────────────────────

@router.callback_query(F.data == "check")
async def start_check(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("🔍 Введите номер автомобиля:\n\nПример: А123АА 777")
    await state.set_state(CheckState.waiting_for_car)
    await callback.answer()


@router.message(CheckState.waiting_for_car)
async def do_check(message: Message, state: FSMContext):
    await state.clear()
    normalized = message.text.strip().upper().replace(" ", "")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT p.*, r.house, r.apartment, r.full_name as resident_name
               FROM passes p JOIN residents r ON p.resident_id=r.id
               WHERE UPPER(REPLACE(p.car_number,' ','')) = $1
                 AND p.status='approved'
                 AND date(p.date_from) <= CURRENT_DATE
                 AND date(p.date_to)   >= CURRENT_DATE
               ORDER BY p.date_to DESC LIMIT 1""",
            normalized,
        )

        if row:
            text = (
                f"✅ Активный пропуск!\n\n"
                f"🚗 {format_car(row['car_number'])}\n"
                f"👤 Гость: {row['guest_fullname']}\n"
                f"🏠 Жилец: {row['resident_name']}\n"
                f"📍 д.{row['house']}, кв.{row['apartment']}\n"
                f"📅 {row['date_from'].strftime('%d.%m.%Y')} — {row['date_to'].strftime('%d.%m.%Y')}"
            )
        else:
            # Проверяем, не жилец ли это
            try:
                res = await conn.fetchrow(
                    """SELECT r.full_name, r.house, r.apartment
                       FROM residents r JOIN cars c ON r.id=c.resident_id
                       WHERE UPPER(REPLACE(c.car_number,' ','')) = $1""",
                    normalized,
                )
                if res:
                    text = (
                        f"🏠 Автомобиль жильца\n\n"
                        f"🚗 {format_car(message.text.strip())}\n"
                        f"👤 {res['full_name']}\n"
                        f"📍 д.{res['house']}, кв.{res['apartment']}"
                    )
                else:
                    text = f"❌ Нет активного пропуска для {format_car(message.text.strip())}"
            except Exception:
                text = f"❌ Нет активного пропуска для {format_car(message.text.strip())}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Проверить ещё", callback_data="check")],
        [InlineKeyboardButton(text="🏠 Главное меню",  callback_data="menu")],
    ])
    await message.answer(text, reply_markup=kb)


# ─────────────────────────────────────────────
# Статистика (ИСПРАВЛЕНО)
# ─────────────────────────────────────────────

async def _send_stats(target):
    today     = date.today()
    month_ago = today - timedelta(days=30)
    async with db_pool.acquire() as conn:
        today_stats = await conn.fetchrow(
            """SELECT COUNT(*) as total,
                 COUNT(*) FILTER (WHERE status='approved') as approved,
                 COUNT(*) FILTER (WHERE status='rejected') as rejected,
                 COUNT(*) FILTER (WHERE status='pending')  as pending
               FROM passes WHERE date(date_from) <= $1 AND date(date_to) >= $1""", today)
        month_stats = await conn.fetchrow(
            """SELECT COUNT(*) as total,
                 COUNT(*) FILTER (WHERE status='approved') as approved,
                 COUNT(*) FILTER (WHERE status='rejected') as rejected,
                 COUNT(*) FILTER (WHERE status='pending')  as pending
               FROM passes WHERE date(created_at) >= $1""", month_ago)
    text = (
        f"📊 Статистика на {today.strftime('%d.%m.%Y')}:\n\n"
        f"🗓 Активны сегодня:\n"
        f"  ✅ Одобрено: {today_stats['approved']}\n"
        f"  ❌ Отклонено: {today_stats['rejected']}\n"
        f"  ⏳ В ожидании: {today_stats['pending']}\n"
        f"  📦 Всего: {today_stats['total']}\n\n"
        f"📅 За 30 дней:\n"
        f"  ✅ Одобрено: {month_stats['approved']}\n"
        f"  ❌ Отклонено: {month_stats['rejected']}\n"
        f"  ⏳ В ожидании: {month_stats['pending']}\n"
        f"  📦 Всего: {month_stats['total']}"
    )
    await target.answer(text, reply_markup=inline_refresh_kb("stats"))

@router.callback_query(F.data == "stats")
async def show_stats(callback: CallbackQuery):
    await _send_stats(callback.message)
    await callback.answer()


# ─────────────────────────────────────────────
# Запуск
# ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("approve_"))
async def approve_pass(callback: CallbackQuery):
    pass_id = int(callback.data.split("_")[1])
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE passes SET status='approved' WHERE id=$1", pass_id)
        row = await conn.fetchrow(
            "SELECT p.*, r.telegram_id FROM passes p JOIN residents r ON p.resident_id=r.id WHERE p.id=$1",
            pass_id,
        )
    await callback.message.edit_text(
        callback.message.text + "\n\n✅ *ОДОБРЕНО*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Отменить решение", callback_data=f"undo_approve_{pass_id}")]
        ])
    )
    if row and row["telegram_id"]:
        try:
            resident_bot = Bot(token=os.getenv("BOT_TOKEN"))
            await resident_bot.send_message(
                row["telegram_id"],
                f"✅ Пропуск #{pass_id} одобрен!\n"
                f"🚗 {row['car_number']}\n"
                f"📅 {row['date_from'].strftime('%d.%m.%Y')} — {row['date_to'].strftime('%d.%m.%Y')}",
            )
            await resident_bot.session.close()
        except Exception:
            pass
    await callback.answer("Пропуск одобрен")


@router.callback_query(F.data.startswith("undo_approve_"))
async def undo_approve(callback: CallbackQuery):
    pass_id = int(callback.data.split("_")[-1])
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE passes SET status='pending' WHERE id=$1", pass_id)
        row = await conn.fetchrow(
            "SELECT p.*, r.telegram_id FROM passes p JOIN residents r ON p.resident_id=r.id WHERE p.id=$1",
            pass_id,
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{pass_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{pass_id}")],
    ])
    text = callback.message.text.split("\n\n✅")[0]
    await callback.message.edit_text(
        text + "\n\n↩️ *Решение отменено — заявка снова на рассмотрении*",
        parse_mode="Markdown", reply_markup=kb
    )
    if row and row["telegram_id"]:
        try:
            resident_bot = Bot(token=os.getenv("BOT_TOKEN"))
            await resident_bot.send_message(
                row["telegram_id"],
                f"🔄 Заявка #{pass_id} снова на рассмотрении.\nОжидайте решения охраны."
            )
            await resident_bot.session.close()
        except Exception:
            pass
    await callback.answer("Решение отменено")


@router.callback_query(F.data.startswith("reject_"))
async def reject_pass(callback: CallbackQuery, state: FSMContext):
    pass_id = int(callback.data.split("_")[1])
    await state.update_data(
        reject_pass_id=pass_id,
        reject_msg_id=callback.message.message_id,
        reject_chat_id=callback.message.chat.id,
        reject_original_text=callback.message.text
    )
    await callback.message.edit_text(
        callback.message.text + "\n\n✍️ Укажите причину отклонения:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"undo_reject_{pass_id}")]
        ])
    )
    await state.set_state(RejectReason.waiting_for_reason)
    await callback.answer()


@router.message(RejectReason.waiting_for_reason)
async def process_reject_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    pass_id = data["reject_pass_id"]
    reason = message.text.strip()
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE passes SET status='rejected' WHERE id=$1", pass_id)
        row = await conn.fetchrow(
            "SELECT p.*, r.telegram_id FROM passes p JOIN residents r ON p.resident_id=r.id WHERE p.id=$1",
            pass_id,
        )
    try:
        await bot.edit_message_text(
            chat_id=data["reject_chat_id"],
            message_id=data["reject_msg_id"],
            text=data["reject_original_text"] + f"\n\n❌ *ОТКЛОНЕНО*\nПричина: {reason}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Отменить решение", callback_data=f"undo_reject_{pass_id}")]
            ])
        )
    except Exception:
        pass
    if row and row["telegram_id"]:
        try:
            resident_bot = Bot(token=os.getenv("BOT_TOKEN"))
            await resident_bot.send_message(
                row["telegram_id"],
                f"❌ Заявка #{pass_id} отклонена.\n\n"
                f"Причина: {reason}\n\n"
                "Вы можете подать новую заявку через «➕ Новый пропуск»."
            )
            await resident_bot.session.close()
        except Exception:
            pass
    await message.delete()
    await state.clear()


@router.callback_query(F.data.startswith("undo_reject_"))
async def undo_reject(callback: CallbackQuery):
    pass_id = int(callback.data.split("_")[-1])
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE passes SET status='pending' WHERE id=$1", pass_id)
        row = await conn.fetchrow(
            "SELECT p.*, r.telegram_id FROM passes p JOIN residents r ON p.resident_id=r.id WHERE p.id=$1",
            pass_id,
        )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{pass_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{pass_id}")],
    ])
    text = callback.message.text.split("\n\n❌")[0].split("\n\n✍️")[0]
    await callback.message.edit_text(
        text + "\n\n↩️ *Решение отменено — заявка снова на рассмотрении*",
        parse_mode="Markdown", reply_markup=kb
    )
    if row and row["telegram_id"]:
        try:
            resident_bot = Bot(token=os.getenv("BOT_TOKEN"))
            await resident_bot.send_message(
                row["telegram_id"],
                f"🔄 Заявка #{pass_id} снова на рассмотрении.\nОжидайте решения охраны."
            )
            await resident_bot.session.close()
        except Exception:
            pass
    await callback.answer("Решение отменено")


async def main():
    await init_db()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    print("✅ Бот охранника запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
