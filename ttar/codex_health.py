"""Persistent six-hour probes of the two independent Codex logins."""
import fcntl
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

from .recognizer import RecognitionError, health_probe, remote_health

INTERVAL = 6 * 60 * 60
LABELS = {'ocr': 'распознавания фото', 'work': '/work'}
LOG = logging.getLogger('ttar-health')


class Busy(Exception):
    pass


def probe_work(config):
    work = config['work']
    release = Path(work.get('release_root', '/var/lib/ttar-release'))
    with (release/'work.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy() from None
        home = work.get('code_home', '/var/lib/ttar-code')
        argv = ['/usr/sbin/runuser', '-u', work.get('code_user', 'ttar-code'), '--',
                '/usr/bin/env', '-i', 'PATH=/usr/local/bin:/usr/bin:/bin',
                'HOME=' + home, 'CODEX_HOME=' + home + '/.codex',
                'PYTHONPATH=/opt/ttar-control', '/opt/ttar/.venv/bin/python',
                '-m', 'ttar.codex_health', '--probe', work.get('codex', '/opt/ttar/bin/codex')]
        if work.get('model'):
            argv.append(work['model'])
        try:
            result = subprocess.run(argv, cwd='/tmp', capture_output=True, timeout=90,
                                    env={'PATH': '/usr/bin:/bin'})
        except subprocess.TimeoutExpired:
            raise RecognitionError('timeout') from None
        # The fixed child prints only an enum, never arbitrary provider output.
        code = result.stdout.decode(errors='replace').strip()
        if result.returncode or code != 'ok':
            raise RecognitionError(code)


def alert_text(target, code):
    label = LABELS[target]
    if code == 'auth':
        return f'Слетела авторизация Codex для {label}. Илье нужно повторно войти в ChatGPT на сервере для этого сервиса.'
    if code == 'limit':
        return f'Проверка Codex для {label}: достигнут лимит использования. Подключение сейчас недоступно.'
    return f'Проверка Codex для {label} дважды не получила ответ. Возможен сбой сети или сервиса; слёт авторизации не подтверждён.'


def check_due(store, config, *, probes=None, clock=time.time):
    store.db.execute('''CREATE TABLE IF NOT EXISTS codex_health (
        target TEXT PRIMARY KEY, next_check REAL NOT NULL, checked_at REAL,
        last_success REAL, status TEXT, failures INTEGER NOT NULL DEFAULT 0,
        alert_code TEXT, incident INTEGER NOT NULL DEFAULT 0)''')
    probes = probes or {'ocr': remote_health, 'work': lambda: probe_work(config)}
    for target, probe in probes.items():
        state = store.db.execute('SELECT * FROM codex_health WHERE target=?', (target,)).fetchone()
        if state and state['next_check'] > clock():
            continue
        try:
            probe()
            code = 'ok'
        except Busy:
            continue
        except RecognitionError as error:
            code = error.code
        except Exception:
            code = 'provider'
        now = clock()
        failures = 0 if code == 'ok' else (state['failures'] if state else 0) + 1
        previous_alert = state['alert_code'] if state else None
        incident = state['incident'] if state else 0
        notify = code != 'ok' and (code == 'auth' or failures >= 2) and previous_alert != code
        alert = None if code == 'ok' else code if notify else previous_alert
        delay = 60 if code not in ('ok', 'auth') and failures == 1 else INTERVAL
        last_success = now if code == 'ok' else state['last_success'] if state else None
        with store.transaction():
            if notify:
                incident += 1
                store.send(f'codex-health:{target}:{incident}', 'sendMessage', {
                    'chat_id': config['allowed_chat_id'], 'text': alert_text(target, code)})
            store.db.execute('''INSERT INTO codex_health VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(target) DO UPDATE SET next_check=excluded.next_check,
                checked_at=excluded.checked_at,last_success=excluded.last_success,
                status=excluded.status,failures=excluded.failures,
                alert_code=excluded.alert_code,incident=excluded.incident''',
                (target, now + delay, now, last_success, code, failures, alert, incident))
        LOG.info('Codex health target=%s status=%s', target, code)


if __name__ == '__main__':
    if len(sys.argv) not in (3, 4) or sys.argv[1] != '--probe':
        raise SystemExit(2)
    try:
        health_probe(codex=sys.argv[2], model=sys.argv[3] if len(sys.argv) == 4 else None)
        print('ok')
    except RecognitionError as error:
        print(error.code)
    except Exception:
        print('provider')
