"""
API маршруты модуля распознавания номеров.
Подключается к web_app.py через app.include_router(camera_router)
"""

import json
import os
from datetime import datetime
from pathlib import Path

import asyncpg
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / 'modules' / 'camera' / 'config.json'
templates = Jinja2Templates(directory=str(BASE_DIR / 'templates'))

camera_router = APIRouter()


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {'kpp': [], 'recognition': {}}


# ── Страница распознавания ───────────────────────────────────
@camera_router.get('/recognition', response_class=HTMLResponse)
async def recognition_page(request: Request):
    # Проверка авторизации (переиспользуем из web_app.py)
    from web_app import auth_redirect, db_pool
    redir = auth_redirect(request)
    if redir:
        return redir

    config = load_config()

    # Список камер для отображения
    cameras = []
    for kpp in config.get('kpp', []):
        for cam in kpp.get('cameras', []):
            cameras.append({
                'id': cam['id'],
                'name': cam['name'],
                'direction': cam['direction'],
                'direction_label': 'Въезд' if cam['direction'] == 'in'
                                   else 'Выезд' if cam['direction'] == 'out'
                                   else 'Въезд/Выезд',
                'active': cam.get('active', False),
                'kpp_name': kpp['name'],
            })

    # Последние 20 событий
    events = []
    async with db_pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT
                e.id, e.plate_normalized, e.confidence, e.match_type,
                e.barrier_action, e.created_at,
                c.name as camera_name, c.direction,
                r.full_name as resident_name, r.house, r.apartment,
                p.guest_fullname, p.date_from, p.date_to
            FROM camera_events e
            LEFT JOIN cameras c ON c.id = e.camera_id
            LEFT JOIN residents r ON r.id = e.resident_id
            LEFT JOIN passes p ON p.id = e.pass_id
            ORDER BY e.created_at DESC
            LIMIT 20
        ''')
        for row in rows:
            events.append(dict(row))

    return templates.TemplateResponse('recognition.html', {
        'request': request,
        'cameras': cameras,
        'events': events,
        'config': config,
    })


# ── API: последние события (для автообновления) ──────────────
@camera_router.get('/api/camera/events')
async def get_events(request: Request, limit: int = 20):
    from web_app import auth_redirect, db_pool
    redir = auth_redirect(request)
    if redir:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)

    async with db_pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT
                e.id, e.plate_normalized, e.confidence, e.match_type,
                e.barrier_action, e.created_at,
                c.name as camera_name, c.direction,
                r.full_name as resident_name, r.house, r.apartment,
                p.guest_fullname, p.date_from, p.date_to
            FROM camera_events e
            LEFT JOIN cameras c ON c.id = e.camera_id
            LEFT JOIN residents r ON r.id = e.resident_id
            LEFT JOIN passes p ON p.id = e.pass_id
            ORDER BY e.created_at DESC
            LIMIT $1
        ''', limit)

        events = []
        for row in rows:
            d = dict(row)
            d['created_at'] = d['created_at'].strftime('%d.%m.%Y %H:%M:%S')
            if d.get('date_from'):
                d['date_from'] = str(d['date_from'])
            if d.get('date_to'):
                d['date_to'] = str(d['date_to'])

            # Определяем цвет статуса
            if d['match_type'] == 'resident':
                d['status_color'] = 'green'
                d['status_label'] = '🏠 Житель'
            elif d['match_type'] == 'guest':
                d['status_color'] = 'blue'
                d['status_label'] = '👤 Гость'
            else:
                d['status_color'] = 'red'
                d['status_label'] = '❌ Неизвестен'

            events.append(d)

    return JSONResponse({'events': events})


# ── API: снимок вручную ──────────────────────────────────────
@camera_router.post('/api/camera/{camera_id}/capture')
async def manual_capture(request: Request, camera_id: int):
    from web_app import auth_redirect, db_pool
    redir = auth_redirect(request)
    if redir:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)

    config = load_config()

    try:
        from camera_service import capture_once, init_recognizer
        pipeline_obj, unzip_fn = init_recognizer()
        result = await capture_once(camera_id, config, pipeline_obj, unzip_fn, db_pool)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)


# ── API: статус камер ────────────────────────────────────────
@camera_router.get('/api/camera/status')
async def camera_status(request: Request):
    from web_app import auth_redirect
    redir = auth_redirect(request)
    if redir:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)

    config = load_config()
    cameras = []
    for kpp in config.get('kpp', []):
        for cam in kpp.get('cameras', []):
            cameras.append({
                'id': cam['id'],
                'name': cam['name'],
                'active': cam.get('active', False),
                'has_rtsp': bool(cam.get('rtsp_url', '')),
                'direction': cam['direction'],
                'kpp': kpp['name'],
            })

    return JSONResponse({'cameras': cameras})


# ── API: последнее событие (для виджета на dashboard) ────────
@camera_router.get('/api/camera/latest')
async def latest_event(request: Request):
    from web_app import auth_redirect, db_pool
    redir = auth_redirect(request)
    if redir:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT
                e.id, e.plate_normalized, e.match_type,
                e.created_at, c.name as camera_name,
                r.full_name as resident_name, r.house, r.apartment,
                p.guest_fullname
            FROM camera_events e
            LEFT JOIN cameras c ON c.id = e.camera_id
            LEFT JOIN residents r ON r.id = e.resident_id
            LEFT JOIN passes p ON p.id = e.pass_id
            ORDER BY e.created_at DESC
            LIMIT 1
        ''')

        if not row:
            return JSONResponse({'event': None})

        d = dict(row)
        d['created_at'] = d['created_at'].strftime('%d.%m.%Y %H:%M:%S')
        return JSONResponse({'event': d})


# ── API: шлагбаум (заглушка для будущего) ───────────────────
@camera_router.post('/api/camera/barrier/{camera_id}/open')
async def open_barrier(request: Request, camera_id: int):
    from web_app import auth_redirect
    redir = auth_redirect(request)
    if redir:
        return JSONResponse({'error': 'unauthorized'}, status_code=401)

    config = load_config()
    barrier_cfg = config.get('barrier', {})

    if not barrier_cfg.get('enabled', False):
        return JSONResponse({
            'success': False,
            'message': 'Управление шлагбаумом не настроено'
        })

    # TODO: реализовать управление шлагбаумом
    # barrier_type = barrier_cfg.get('type', 'relay')
    # if barrier_type == 'relay':
    #     await open_relay(camera_id)

    return JSONResponse({
        'success': True,
        'message': f'Шлагбаум камеры {camera_id} открыт'
    })
