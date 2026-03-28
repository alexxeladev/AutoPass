#!/usr/bin/env python3
"""
Сервис распознавания номерных знаков.
Читает RTSP потоки, распознаёт номера через nomeroff-net,
сохраняет события в БД, уведомляет веб-панель через WebSocket.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

import asyncpg
import cv2
import numpy as np

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('camera_service')

# Пути
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / 'modules' / 'camera' / 'config.json'
SNAPSHOTS_DIR = Path('/home/user/snapshots')
SNAPSHOTS_DIR.mkdir(exist_ok=True)

# Переменные окружения
DATABASE_URL = os.environ.get('DATABASE_URL', '')


# ── Загрузка конфига ─────────────────────────────────────────
def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ── Нормализация номера ──────────────────────────────────────
def normalize_plate(plate: str) -> str:
    return plate.upper().replace(' ', '').strip()


# ── Проверка качества распознавания ─────────────────────────
def is_valid_recognition(plate: str, min_digits: int, min_letters: int) -> bool:
    """
    Номер считается распознанным если совпало
    хотя бы min_digits цифр и min_letters букв.
    """
    digits = len(re.findall(r'\d', plate))
    letters = len(re.findall(r'[A-ZА-Я]', plate.upper()))
    return digits >= min_digits and letters >= min_letters


# ── Инициализация nomeroff-net ────────────────────────────────
def init_recognizer():
    try:
        from nomeroff_net import pipeline
        from nomeroff_net.tools import unzip
        log.info("Загрузка nomeroff-net pipeline...")
        pipeline_obj = pipeline(
            "number_plate_short_detection_and_reading",
            image_loader="opencv"
        )
        log.info("nomeroff-net загружен успешно")
        return pipeline_obj, unzip
    except ImportError:
        log.error("nomeroff-net не установлен! Запустите install_camera.sh")
        raise


# ── Распознавание номера на кадре ───────────────────────────
def recognize_frame(pipeline_obj, unzip_fn, frame: np.ndarray) -> list[dict]:
    """
    Возвращает список распознанных номеров с confidence.
    """
    results = []
    try:
        (images, images_bboxs,
         zones, texts) = unzip_fn(pipeline_obj([frame]))

        for plate_variants in texts:
            for plate in plate_variants:
                if plate:
                    results.append({
                        'plate': str(plate),
                        'confidence': 0.85  # nomeroff-net не возвращает confidence напрямую
                    })
    except Exception as e:
        log.warning(f"Ошибка распознавания: {e}")
    return results


# ── Поиск номера в БД ────────────────────────────────────────
async def lookup_plate(conn: asyncpg.Connection, plate_normalized: str) -> dict:
    """
    Ищет номер в passes (гостевые) и cars (жильцы).
    Возвращает тип совпадения и данные.
    """
    # Проверяем активный пропуск
    pass_row = await conn.fetchrow('''
        SELECT p.id, p.date_from, p.date_to, p.guest_fullname,
               r.full_name, r.house, r.apartment, r.phone
        FROM passes p
        JOIN residents r ON r.id = p.resident_id
        WHERE UPPER(REPLACE(p.car_number, ' ', '')) = $1
          AND p.status = 'approved'
          AND p.date_from <= CURRENT_DATE
          AND p.date_to >= CURRENT_DATE
    ''', plate_normalized)

    if pass_row:
        return {
            'match_type': 'guest',
            'resident_name': pass_row['full_name'],
            'house': pass_row['house'],
            'apartment': pass_row['apartment'],
            'phone': pass_row['phone'],
            'guest_name': pass_row['guest_fullname'],
            'date_from': str(pass_row['date_from']),
            'date_to': str(pass_row['date_to']),
            'pass_id': pass_row['id'],
            'resident_id': None,
        }

    # Проверяем личное авто жильца
    car_row = await conn.fetchrow('''
        SELECT c.id, r.id as resident_id,
               r.full_name, r.house, r.apartment, r.phone
        FROM cars c
        JOIN residents r ON r.id = c.resident_id
        WHERE UPPER(REPLACE(c.car_number, ' ', '')) = $1
    ''', plate_normalized)

    if car_row:
        return {
            'match_type': 'resident',
            'resident_name': car_row['full_name'],
            'house': car_row['house'],
            'apartment': car_row['apartment'],
            'phone': car_row['phone'],
            'guest_name': None,
            'date_from': None,
            'date_to': None,
            'pass_id': None,
            'resident_id': car_row['resident_id'],
        }

    return {
        'match_type': 'unknown',
        'resident_name': None,
        'house': None,
        'apartment': None,
        'phone': None,
        'guest_name': None,
        'date_from': None,
        'date_to': None,
        'pass_id': None,
        'resident_id': None,
    }


# ── Сохранение события в БД ──────────────────────────────────
async def save_event(conn: asyncpg.Connection, camera_id: int,
                     plate_raw: str, plate_normalized: str,
                     confidence: float, lookup: dict,
                     snapshot_path: str | None) -> int:
    row = await conn.fetchrow('''
        INSERT INTO camera_events
            (camera_id, plate_raw, plate_normalized, confidence,
             match_type, resident_id, pass_id, barrier_action, snapshot_path)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING id
    ''',
        camera_id,
        plate_raw,
        plate_normalized,
        confidence,
        lookup['match_type'],
        lookup['resident_id'],
        lookup['pass_id'],
        'open' if lookup['match_type'] in ('resident', 'guest') else 'none',
        snapshot_path
    )
    return row['id']


# ── Сохранение снимка ────────────────────────────────────────
def save_snapshot(frame: np.ndarray, plate: str) -> str | None:
    try:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_plate = re.sub(r'[^A-ZА-Я0-9]', '', plate.upper())
        filename = f"{ts}_{safe_plate}.jpg"
        path = SNAPSHOTS_DIR / filename
        cv2.imwrite(str(path), frame)
        return str(path)
    except Exception as e:
        log.warning(f"Не удалось сохранить снимок: {e}")
        return None


# ── Обработка одной камеры ───────────────────────────────────
async def process_camera(camera_cfg: dict, config: dict,
                          pipeline_obj, unzip_fn,
                          db_pool: asyncpg.Pool,
                          last_seen: dict):
    """
    Основной цикл обработки одной камеры.
    Читает кадры, распознаёт номера, сохраняет события.
    """
    cam_id = camera_cfg['id']
    cam_name = camera_cfg['name']
    rtsp_url = camera_cfg.get('rtsp_url', '')
    rec_cfg = config['recognition']

    cooldown = rec_cfg.get('cooldown_seconds', 5)
    frame_interval = rec_cfg.get('frame_interval', 0.5)
    min_digits = rec_cfg.get('min_digits', 2)
    min_letters = rec_cfg.get('min_letters', 1)
    min_confidence = rec_cfg.get('min_confidence', 0.7)

    if not rtsp_url:
        log.warning(f"[{cam_name}] RTSP URL не задан, камера пропущена")
        return

    log.info(f"[{cam_name}] Подключение к {rtsp_url}")

    while True:
        cap = cv2.VideoCapture(rtsp_url)
        if not cap.isOpened():
            log.error(f"[{cam_name}] Не удалось подключиться к камере, повтор через 10 сек")
            await asyncio.sleep(10)
            continue

        log.info(f"[{cam_name}] Камера подключена")

        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    log.warning(f"[{cam_name}] Потерян поток, переподключение...")
                    break

                # Распознаём номера на кадре
                recognitions = await asyncio.get_event_loop().run_in_executor(
                    None, recognize_frame, pipeline_obj, unzip_fn, frame
                )

                for rec in recognitions:
                    plate_raw = rec['plate']
                    confidence = rec['confidence']

                    if confidence < min_confidence:
                        continue

                    if not is_valid_recognition(plate_raw, min_digits, min_letters):
                        continue

                    plate_norm = normalize_plate(plate_raw)

                    # Cooldown — не обрабатываем один номер чаще чем раз в N секунд
                    last_time = last_seen.get(f"{cam_id}:{plate_norm}", 0)
                    if time.time() - last_time < cooldown:
                        continue

                    last_seen[f"{cam_id}:{plate_norm}"] = time.time()

                    log.info(f"[{cam_name}] Распознан: {plate_norm} (confidence={confidence:.2f})")

                    # Снимок
                    snapshot = await asyncio.get_event_loop().run_in_executor(
                        None, save_snapshot, frame, plate_norm
                    )

                    # Поиск в БД и сохранение события
                    async with db_pool.acquire() as conn:
                        lookup = await lookup_plate(conn, plate_norm)
                        event_id = await save_event(
                            conn, cam_id, plate_raw, plate_norm,
                            confidence, lookup, snapshot
                        )

                    log.info(
                        f"[{cam_name}] Событие #{event_id}: "
                        f"{plate_norm} → {lookup['match_type']}"
                    )

                await asyncio.sleep(frame_interval)

        finally:
            cap.release()

        await asyncio.sleep(3)


# ── Режим одиночного снимка (по кнопке) ─────────────────────
async def capture_once(camera_id: int, config: dict,
                        pipeline_obj, unzip_fn,
                        db_pool: asyncpg.Pool) -> dict:
    """
    Делает один снимок с камеры и возвращает результат.
    Используется из camera_api.py для кнопки "Снять вручную".
    """
    # Найти камеру по id
    cam_cfg = None
    for kpp in config['kpp']:
        for cam in kpp['cameras']:
            if cam['id'] == camera_id:
                cam_cfg = cam
                break

    if not cam_cfg:
        return {'error': f'Камера {camera_id} не найдена'}

    rtsp_url = cam_cfg.get('rtsp_url', '')
    if not rtsp_url:
        return {'error': 'RTSP URL не задан'}

    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        return {'error': 'Не удалось подключиться к камере'}

    try:
        ret, frame = cap.read()
        if not ret:
            return {'error': 'Не удалось получить кадр'}

        rec_cfg = config['recognition']
        recognitions = recognize_frame(pipeline_obj, unzip_fn, frame)

        results = []
        for rec in recognitions:
            plate_raw = rec['plate']
            plate_norm = normalize_plate(plate_raw)

            if not is_valid_recognition(
                plate_raw,
                rec_cfg.get('min_digits', 2),
                rec_cfg.get('min_letters', 1)
            ):
                continue

            snapshot = save_snapshot(frame, plate_norm)

            async with db_pool.acquire() as conn:
                lookup = await lookup_plate(conn, plate_norm)
                event_id = await save_event(
                    conn, camera_id, plate_raw, plate_norm,
                    rec['confidence'], lookup, snapshot
                )

            results.append({
                'event_id': event_id,
                'plate': plate_norm,
                'confidence': rec['confidence'],
                **lookup
            })

        return {'results': results, 'frame_saved': True}

    finally:
        cap.release()


# ── Главный цикл ─────────────────────────────────────────────
async def main():
    log.info("Запуск сервиса распознавания номеров")

    config = load_config()
    pipeline_obj, unzip_fn = init_recognizer()

    log.info("Подключение к БД...")
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)
    log.info("БД подключена")

    # Словарь для cooldown по каждой камере
    last_seen: dict = {}

    # Собираем все активные камеры
    tasks = []
    for kpp in config['kpp']:
        for cam in kpp['cameras']:
            if cam.get('active', False):
                tasks.append(
                    process_camera(cam, config, pipeline_obj, unzip_fn, db_pool, last_seen)
                )

    if not tasks:
        log.warning("Нет активных камер в config.json. Сервис ожидает...")
        # Держим процесс живым — камеры могут быть активированы позже
        while True:
            await asyncio.sleep(60)
            # Перечитываем конфиг
            config = load_config()
            for kpp in config['kpp']:
                for cam in kpp['cameras']:
                    if cam.get('active', False):
                        log.info(f"Обнаружена новая активная камера: {cam['name']}")
                        tasks.append(
                            process_camera(cam, config, pipeline_obj, unzip_fn, db_pool, last_seen)
                        )
            if tasks:
                break

    await asyncio.gather(*tasks)


if __name__ == '__main__':
    asyncio.run(main())
