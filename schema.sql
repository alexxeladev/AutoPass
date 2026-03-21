-- ================================================================
-- Схема базы данных — Пропускная система
-- ================================================================

-- Жильцы
CREATE TABLE IF NOT EXISTS residents (
    id          SERIAL PRIMARY KEY,
    house       TEXT NOT NULL,
    apartment   TEXT NOT NULL,
    full_name   TEXT NOT NULL,
    phone       TEXT UNIQUE NOT NULL,
    telegram_id BIGINT UNIQUE,
    verified    BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Автомобили жильцов
CREATE TABLE IF NOT EXISTS cars (
    id          SERIAL PRIMARY KEY,
    resident_id INT NOT NULL REFERENCES residents(id) ON DELETE CASCADE,
    car_number  TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (resident_id, car_number)
);

-- Коды верификации
CREATE TABLE IF NOT EXISTS verification_codes (
    phone       TEXT PRIMARY KEY,
    code        TEXT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Пропуска
CREATE TABLE IF NOT EXISTS passes (
    id             SERIAL PRIMARY KEY,
    resident_id    INT NOT NULL REFERENCES residents(id) ON DELETE CASCADE,
    guest_fullname TEXT NOT NULL,          -- ФИО гостя (чья машина)
    car_number     TEXT NOT NULL,
    date_from      DATE NOT NULL,
    date_to        DATE NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_passes_status     ON passes(status);
CREATE INDEX IF NOT EXISTS idx_passes_car        ON passes(UPPER(REPLACE(car_number, ' ', '')));
CREATE INDEX IF NOT EXISTS idx_passes_dates      ON passes(date_from, date_to);
CREATE INDEX IF NOT EXISTS idx_residents_phone   ON residents(phone);
CREATE INDEX IF NOT EXISTS idx_residents_tg      ON residents(telegram_id);
CREATE INDEX IF NOT EXISTS idx_cars_number       ON cars(UPPER(REPLACE(car_number, ' ', '')));

-- Пользователи панели (создаётся автоматически при старте web_app.py)
-- Дефолтный логин: admin / admin123  — СМЕНИТЕ ПАРОЛЬ ПОСЛЕ ПЕРВОГО ВХОДА!
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'guard',
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Охранники (доступ к боту охраны по Telegram ID)
CREATE TABLE IF NOT EXISTS guards (
    id          SERIAL PRIMARY KEY,
    full_name   TEXT NOT NULL,
    telegram_id BIGINT UNIQUE,
    phone       TEXT,
    active      BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_guards_telegram ON guards(telegram_id);
