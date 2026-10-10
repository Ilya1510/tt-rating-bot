"""Durable local inbox and reply mapping; no object storage dependency."""
import contextlib
import hashlib
import json
import os
import sqlite3
import time


@contextlib.contextmanager
def database():
    db = sqlite3.connect(os.environ['BRIDGE_DATABASE'], timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    try:
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS inbox(id INTEGER PRIMARY KEY, source TEXT NOT NULL, '
                       'identity TEXT NOT NULL, body TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, '
                       'attempts INTEGER NOT NULL DEFAULT 0, ready REAL NOT NULL DEFAULT 0, UNIQUE(source,identity))')
            yield db
    finally:
        db.close()


def get_state(key, default=None):
    with database() as db:
        row = db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default


def set_state(key, value):
    with database() as db:
        db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, json.dumps(value)))


def load_mapping():
    return get_state('mapping', {'tg_to_vk': {}, 'vk_to_tg': {}, 'processed_updates': []})


def save_mapping(mapping):
    # Preserve the historical bridge's reply window.
    for key in ('tg_to_vk', 'vk_to_tg'):
        mapping[key] = dict(list(mapping[key].items())[-100:])
    mapping['processed_updates'] = mapping.get('processed_updates', [])[-200:]
    set_state('mapping', mapping)


def persist_batch(source, updates, cursor):
    # Commit all events and the next cursor together, before acknowledging upstream.
    with database() as db:
        for update in updates:
            body = json.dumps(update, sort_keys=True, ensure_ascii=False)
            identity = str(update['update_id']) if source == 'tg' else str(
                update.get('event_id') or hashlib.sha256(body.encode()).hexdigest())
            db.execute('INSERT OR IGNORE INTO inbox(source,identity,body) VALUES (?,?,?)',
                       (source, identity, body))
        db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (source + '_cursor', json.dumps(cursor)))


def next_job():
    with database() as db:
        row = db.execute('SELECT * FROM inbox WHERE done=0 AND ready<=? ORDER BY id LIMIT 1',
                         (time.time(),)).fetchone()
        return dict(row) if row else None


def finish(job_id):
    with database() as db:
        db.execute('UPDATE inbox SET done=1 WHERE id=?', (job_id,))


def retry(job_id):
    with database() as db:
        db.execute('UPDATE inbox SET attempts=attempts+1,ready=? WHERE id=?', (time.time() + 30, job_id))
