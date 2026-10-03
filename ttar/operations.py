"""Durable owner commands; execution happens outside the Telegram worker."""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo('Europe/Moscow')
ROOM = 'conf_mm_5_15@yandex-team.ru'
OWNER_ID = 220427487
COMMANDS = {'/work', '/create_booking', '/cancel_booking'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS operations(
 id INTEGER PRIMARY KEY, dedupe TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
 actor INTEGER NOT NULL, chat_id INTEGER NOT NULL, request TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL,
 started_at REAL, finished_at REAL, result TEXT);
CREATE TABLE IF NOT EXISTS bookings(
 id INTEGER PRIMARY KEY, booking_key TEXT UNIQUE NOT NULL, room TEXT NOT NULL,
 start TEXT NOT NULL, end TEXT NOT NULL, event_id TEXT, url TEXT,
 status TEXT NOT NULL DEFAULT 'requested', reason TEXT NOT NULL DEFAULT '',
 next_check REAL, checks INTEGER NOT NULL DEFAULT 0,
 create_attempted INTEGER NOT NULL DEFAULT 0, pending_action TEXT NOT NULL DEFAULT 'book',
 UNIQUE(room,start,end));
'''


def next_slot(now):
    day = now.astimezone(MOSCOW).replace(hour=19, minute=0, second=0, microsecond=0)
    for offset in range(8):
        candidate = day + timedelta(days=offset)
        if candidate.weekday() in (0, 3) and candidate > now:
            return candidate


def booking_request(args, now):
    parts = args.split()
    try:
        if not parts:
            start, duration = next_slot(now), 60
        else:
            if len(parts) > 3:
                raise ValueError()
            date = datetime.strptime(parts[0], '%Y-%m-%d')
            hour = datetime.strptime(parts[1] if len(parts) > 1 else '19:00', '%H:%M')
            start = date.replace(hour=hour.hour, minute=hour.minute, tzinfo=MOSCOW)
            duration = int(parts[2]) if len(parts) > 2 else 60
        if not 1 <= duration <= 90:
            raise ValueError('Слот — от 1 до 90 минут.')
        if start <= now:
            raise ValueError('Укажи будущее время по Москве.')
        if start - now >= timedelta(days=3):
            raise ValueError('Зал открывается для брони менее чем за 3 дня до начала.')
        return {'start': start.isoformat(), 'end': (start + timedelta(minutes=duration)).isoformat()}
    except (TypeError, OverflowError):
        raise ValueError('Формат: /create_booking ГГГГ-ММ-ДД ЧЧ:ММ [минуты]. Время московское.') from None
    except ValueError as error:
        if str(error) in ('Слот — от 1 до 90 минут.', 'Укажи будущее время по Москве.',
                          'Зал открывается для брони менее чем за 3 дня до начала.'):
            raise
        raise ValueError('Формат: /create_booking ГГГГ-ММ-ДД ЧЧ:ММ [минуты]. Время московское.') from None


def enqueue(store, command, text, actor, chat_id, dedupe, now=None):
    if actor != OWNER_ID:
        raise ValueError('Эта команда доступна только Илье.')
    now = now or datetime.now(MOSCOW)
    args = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ''
    if command == '/work':
        if not args or len(args) > 12000:
            raise ValueError('Напиши задачу: /work что изменить в боте.')
        kind, request = 'work', {'text': args}
    elif command == '/create_booking':
        kind, request = 'book', booking_request(args, now)
    else:
        parts = args.split()
        try:
            if len(parts) > 2:
                raise ValueError()
            if parts:
                datetime.strptime(parts[0], '%Y-%m-%d')
            if len(parts) == 2:
                datetime.strptime(parts[1], '%H:%M')
        except ValueError:
            raise ValueError('Формат: /cancel_booking ГГГГ-ММ-ДД [ЧЧ:ММ].') from None
        kind, request = 'cancel', {'date': parts[0] if parts else None, 'time': parts[1] if len(parts) == 2 else None}
    store.db.execute('INSERT OR IGNORE INTO operations(dedupe,kind,actor,chat_id,request,created_at) VALUES (?,?,?,?,?,?)',
        (dedupe, kind, actor, chat_id, json.dumps(request, ensure_ascii=False), now.timestamp()))
    op = store.db.execute('SELECT id FROM operations WHERE dedupe=?', (dedupe,)).fetchone()[0]
    if command == '/work':
        return None, None
    return f'Задача #{op} принята. Результат напишу в этот чат.', None


def schedule(store, chat_id, now, duration=60):
    now = now.astimezone(MOSCOW)
    # No retrospective booking after a missed day; same-day recovery is safe.
    if now.weekday() not in (0, 4) or (now.hour, now.minute) < (19, 1):
        return False
    start = (now + timedelta(days=3)).replace(hour=19, minute=0, second=0, microsecond=0)
    if type(duration) is not int or not 1 <= duration <= 150:
        raise ValueError('Invalid scheduled duration')
    request = {'start': start.isoformat(), 'end': (start + timedelta(minutes=duration)).isoformat()}
    key = 'scheduled:' + start.date().isoformat()
    with store.transaction():
        cursor = store.db.execute('INSERT OR IGNORE INTO operations(dedupe,kind,actor,chat_id,request,created_at) VALUES (?,?,?,?,?,?)',
            (key, 'book', 0, chat_id, json.dumps(request), now.timestamp()))
    return bool(cursor.rowcount)
