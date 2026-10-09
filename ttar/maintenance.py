"""Installed as a fixed control-service snapshot, separate from editable bot code."""
import json
import fcntl
import logging
import os
import signal
import threading
import time
from datetime import datetime
from pathlib import Path

from .core import Store
from .operations import MOSCOW, OWNER_ID, ROOM, schedule
from .booking_policy import load_policy
from .work_context import tennis_snapshot
from .telegram import redact

STOP = threading.Event()
RELOAD = threading.Event()
LOG = logging.getLogger('ttar-maintenance')


def say(store, chat_id, key, text):
    text = redact(text)[:3800]
    with store.transaction():
        store.send(key, 'sendMessage', {'chat_id': chat_id, 'text': text})


def claim(store, work=False):
    with store.transaction():
        row = store.db.execute("SELECT * FROM operations WHERE status='pending' AND " +
            ("kind='work'" if work else "kind!='work'") + ' ORDER BY id LIMIT 1').fetchone()
        if row:
            store.db.execute("UPDATE operations SET status='running',started_at=? WHERE id=?", (time.time(), row['id']))
            return dict(row)


def finish(store, op, status, text, result=None, booking_id=None):
    with store.transaction():
        if op['kind'] == 'work':
            store.db.execute("UPDATE outbox SET status='superseded' WHERE status='pending' AND dedupe LIKE ?",
                             (f"work-progress:{op['id']}:%",))
        store.db.execute('UPDATE operations SET status=?,finished_at=?,result=? WHERE id=?',
                         (status, time.time(), json.dumps(result or {}, ensure_ascii=False), op['id']))
        payload = {'chat_id': op['chat_id'], 'text': redact(text)[:3800]}
        if booking_id is not None:
            store.db.execute('INSERT INTO booking_notices(booking_id,operation_id,chat_id,text) VALUES (?,?,?,?) '
                'ON CONFLICT(booking_id) DO UPDATE SET operation_id=excluded.operation_id,chat_id=excluded.chat_id,'
                'text=excluded.text,message_id=NULL,sent_text=NULL',
                (booking_id, op['id'], op['chat_id'], payload['text']))
            payload['_booking_notice'] = {'booking_id': booking_id, 'operation_id': op['id']}
        store.send(f"operation-result:{op['id']}", 'sendMessage', payload)


def recover(store):
    with store.transaction():
        for row in store.db.execute("SELECT * FROM operations WHERE status='running'").fetchall():
            if row['kind'] == 'work':
                store.db.execute("UPDATE operations SET status='uncertain',finished_at=? WHERE id=?", (time.time(), row['id']))
                store.send(f"operation-result:{row['id']}", 'sendMessage', {
                    'chat_id': row['chat_id'],
                    'text': f"Задача /work #{row['id']} прервалась при перезапуске. Автоматически повторять изменения не стал; результат нужно проверить по GitHub и версии сервиса."})
            else:
                store.db.execute("UPDATE operations SET status='pending' WHERE id=?", (row['id'],))


def booking_text(row, result):
    start = datetime.fromisoformat(row['start']).astimezone(MOSCOW)
    end = datetime.fromisoformat(row['end']).astimezone(MOSCOW)
    slot = f'{start:%d.%m.%Y %H:%M}–{end:%H:%M} МСК'
    status = result.status
    quiet_resource_status = result.event_id and status in ('accepted', 'pending', 'unverifiable')
    cancelling = row['pending_action'] == 'cancel'
    if quiet_resource_status and not cancelling:
        text = f'Встреча создана: {slot}.'
    elif status == 'cancelled':
        text = f'Бронь отменена: {slot}.'
    elif status in ('pending', 'unverifiable', 'uncertain'):
        text = f'Отмена встречи на {slot} выполняется.' if cancelling else f'Не удалось определить результат создания встречи на {slot}.'
    else:
        text = f'Не удалось забронировать зал на {slot}.'
    resource_notes = {
        'Календарь не показывает участие ресурса зала.',
        'Ожидается подтверждение зала.',
        'Неизвестный ответ зала.',
        'Зал принял приглашение; занятость подтверждена.',
        'Зал принял приглашение, но занятость этой встречей не подтверждена.',
    }
    if result.reason and not (quiet_resource_status and not cancelling and result.reason in resource_notes):
        text += '\n' + result.reason
    if result.url and status != 'cancelled':
        text += '\n' + result.url
    return text


def save_booking_result(store, row, result):
    pending = result.status in ('pending', 'unverifiable', 'uncertain')
    with store.transaction():
        store.db.execute('UPDATE bookings SET status=?,event_id=COALESCE(?,event_id),url=COALESCE(?,url),reason=?,next_check=? WHERE id=?',
            (result.status, result.event_id, result.url, redact(result.reason or '')[:1000],
             time.time() + 30 if pending else None, row['id']))


def run_booking(store, client, op):
    request = json.loads(op['request'])
    if op['kind'] == 'cancel':
        params = [ROOM]
        query = "SELECT * FROM bookings WHERE room=? AND status NOT IN ('cancelled','not_found','conflict','rejected')"
        if request.get('date'):
            query += ' AND substr(start,1,10)=?'; params.append(request['date'])
        else:
            query += ' AND start>?'; params.append(datetime.now(MOSCOW).isoformat())
        if request.get('time'):
            query += ' AND substr(start,12,5)=?'; params.append(request['time'])
        rows = store.db.execute(query + ' ORDER BY start', params).fetchall()
        if not request.get('date'):
            rows = rows[:1]
        if len(rows) != 1:
            finish(store, op, 'failed', 'Не нашёл одну нашу бронь для отмены. Укажи дату и время: /cancel_booking ГГГГ-ММ-ДД ЧЧ:ММ.')
            return
        row = rows[0]
        with store.transaction():
            store.db.execute("UPDATE bookings SET pending_action='cancel',checks=0 WHERE id=?", (row['id'],))
        if not row['event_id']:
            result = client.reconcile_booking(row['start'], row['end'], row['booking_key'])
            save_booking_result(store, row, result)
            if not result.event_id:
                finish(store, op, 'failed', 'Не удалось определить созданную встречу для отмены. Новую встречу не создавал.')
                return
            row = store.db.execute('SELECT * FROM bookings WHERE id=?', (row['id'],)).fetchone()
        result = client.cancel_booking(row['event_id'], row['booking_key'], row['start'], row['end'])
    else:
        start, end = request['start'], request['end']
        key = 'ttar:' + start + '/' + end
        with store.transaction():
            store.db.execute('INSERT OR IGNORE INTO bookings(booking_key,room,start,end) VALUES (?,?,?,?)', (key, ROOM, start, end))
        row = store.db.execute('SELECT * FROM bookings WHERE booking_key=?', (key,)).fetchone()
        if row['status'] == 'cancelled':
            if op['actor'] == 0:
                finish(store, op, 'done', 'Эта бронь уже отменена. Автоматически создавать её повторно не буду.')
                return
            with store.transaction():
                store.db.execute("UPDATE bookings SET status='requested',event_id=NULL,url=NULL,create_attempted=0,pending_action='book',checks=0 WHERE id=?", (row['id'],))
            row = store.db.execute('SELECT * FROM bookings WHERE id=?', (row['id'],)).fetchone()
        if row['pending_action'] == 'cancel':
            finish(store, op, 'pending', 'Сначала нужно получить подтверждение предыдущей отмены; новую встречу пока не создавал.')
            return
        if row['event_id']:
            result = client.verify_booking(row['event_id'], start, end)
        elif row['create_attempted']:
            result = client.reconcile_booking(start, end, key)
        else:
            with store.transaction():
                store.db.execute("UPDATE bookings SET status='creating',create_attempted=1,checks=0 WHERE id=?", (row['id'],))
            def saved(event_id):
                with store.transaction():
                    store.db.execute('UPDATE bookings SET event_id=? WHERE id=?', (event_id, row['id']))
            result = client.create_booking(start, end, key, saved)
            if result.status in ('conflict', 'rejected', 'unverifiable') and not result.event_id:
                with store.transaction():
                    store.db.execute('UPDATE bookings SET create_attempted=0 WHERE id=?', (row['id'],))
    save_booking_result(store, row, result)
    row = store.db.execute('SELECT * FROM bookings WHERE id=?', (row['id'],)).fetchone()
    finish(store, op, 'done' if result.status in ('accepted', 'cancelled') else result.status,
           booking_text(row, result), {'booking_id': row['id'], 'status': result.status, 'event_id': result.event_id},
           booking_id=row['id'])


def check_pending(store, client, chat_id):
    row = store.db.execute('SELECT * FROM bookings WHERE next_check<=? AND checks<10 ORDER BY next_check LIMIT 1', (time.time(),)).fetchone()
    if row is None:
        return
    with store.transaction():
        store.db.execute('UPDATE bookings SET checks=checks+1,next_check=? WHERE id=?', (time.time() + 30, row['id']))
    if row['event_id'] and row['pending_action'] == 'cancel':
        result = client.cancel_booking(row['event_id'], row['booking_key'], row['start'], row['end'])
    elif row['event_id']:
        result = client.verify_booking(row['event_id'], row['start'], row['end'])
    else:
        result = client.reconcile_booking(row['start'], row['end'], row['booking_key'])
    save_booking_result(store, row, result)
    if result.status in ('accepted', 'rejected', 'conflict', 'cancelled') or row['checks'] >= 9:
        with store.transaction():
            store.db.execute('UPDATE bookings SET next_check=NULL WHERE id=?', (row['id'],))
        # Rechecks change the original notice only when its visible meaning
        # changes. Missing resource metadata never produces another message.
        text = redact(booking_text(row, result))[:3800]
        with store.transaction():
            notice = store.db.execute('SELECT * FROM booking_notices WHERE booking_id=?', (row['id'],)).fetchone()
            if notice and notice['text'] != text:
                store.db.execute('UPDATE booking_notices SET text=? WHERE booking_id=?', (text, row['id']))
                store.send(f"booking-check:{row['id']}:{notice['operation_id']}:{row['checks'] + 1}",
                           'updateBookingNotice', {'booking_id': row['id'], 'operation_id': notice['operation_id']})


def work_loop(config):
    from .work_runner import run_work
    store = Store(config['database'])
    try:
        while not STOP.is_set():
            op = claim(store, work=True)
            if not op:
                STOP.wait(1)
                continue
            if op['actor'] <= 0 or op['chat_id'] != config['allowed_chat_id']:
                finish(store, op, 'failed', 'Нет доступа к /work.')
                continue
            try:
                def progress(message):
                    LOG.info('Work %s: %s', op['id'], message)
                work_config = dict(config['work'], actor_id=op['actor'], tennis_context=tennis_snapshot(store, op['chat_id']))
                result = run_work(json.loads(op['request'])['text'], op['id'], work_config, progress)
                text = f"/work #{op['id']}: " + result['summary']
                if result['status'] == 'done' and result.get('commit'):
                    text += '\nИзменения применены, проверки пройдены.'
                if result.get('controller_reload') and result['status'] == 'done':
                    result['final_text'] = text
                    with store.transaction():
                        store.db.execute("UPDATE operations SET status='awaiting_reload',result=? WHERE id=?",
                                         (json.dumps(result, ensure_ascii=False), op['id']))
                    RELOAD.set()
                    STOP.set()
                else:
                    finish(store, op, result['status'], text, result)
            except Exception as error:
                LOG.warning('Work operation %s failed: %s', op['id'], type(error).__name__)
                finish(store, op, 'failed', f"/work #{op['id']}: не удалось завершить задачу. Сбой исполнителя ({type(error).__name__}); детали сохранены на сервере.")
    finally:
        store.db.close()


def main():
    from .calendar_api import CalendarClient
    logging.basicConfig(level=logging.INFO)
    config = json.loads(Path(os.environ.get('TTAR_MAINTENANCE_CONFIG', '/etc/ttar/maintenance.json')).read_text())
    lock = open('/var/lib/ttar-release/maintenance.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    credentials = json.loads((Path(os.environ['CREDENTIALS_DIRECTORY'])/'maintenance.json').read_text())
    store = Store(config['database'])
    client = CalendarClient(credentials['calendar_token'], ownership_lookup=lambda event_id, key:
        store.db.execute('SELECT 1 FROM bookings WHERE event_id=? AND booking_key=?',
                         (event_id, key)).fetchone() is not None)
    recover(store)
    for row in store.db.execute("SELECT * FROM operations WHERE status='awaiting_reload'").fetchall():
        result = json.loads(row['result'])
        marker = Path('/opt/ttar-control/commit')
        if marker.exists() and marker.read_text().strip() == result.get('commit'):
            finish(store, row, 'done', result['final_text'], result)
    Path('/var/lib/ttar-release/controller-ready.json').write_text(json.dumps({
        'pid': os.getpid(), 'commit': Path('/opt/ttar-control/commit').read_text().strip()
        if Path('/opt/ttar-control/commit').exists() else None}))
    thread = threading.Thread(target=work_loop, args=(config,))
    thread.start()
    try:
        while not STOP.is_set():
            try:
                if config.get('booking_schedule_enabled', True):
                    policy = load_policy('/opt/ttar/booking-policy.json')
                    schedule(store, config['allowed_chat_id'], datetime.now(MOSCOW), policy['regular_minutes'])
                op = claim(store)
                if op:
                    automatic = op['actor'] == 0 and op['kind'] == 'book' and op['dedupe'].startswith('scheduled:')
                    if op['chat_id'] != config['allowed_chat_id'] or (op['actor'] != OWNER_ID and not automatic):
                        finish(store, op, 'failed', 'Нет доступа к управлению бронями.')
                    else:
                        try:
                            run_booking(store, client, op)
                        except Exception as error:
                            LOG.warning('Booking operation %s failed: %s', op['id'], type(error).__name__)
                            finish(store, op, 'uncertain', f"Не удалось проверить бронь: ошибка API ({type(error).__name__}). Успех не подтверждён; новую встречу повторно не создавал.")
                check_pending(store, client, config['allowed_chat_id'])
            except Exception as error:
                LOG.warning('Maintenance tick failed: %s', type(error).__name__)
            STOP.wait(1)
    finally:
        thread.join()
        store.db.close()
        lock.close()


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    main()
    if RELOAD.is_set():
        raise SystemExit(75)
