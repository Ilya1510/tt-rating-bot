import json
import os
import sqlite3
from pathlib import Path

from ttar import controller_guard


def test_failed_controller_boot_restores_code_and_reports_one_failure(tmp_path, monkeypatch):
    real_path = Path
    def mapped(value):
        value = str(value)
        return tmp_path/value.lstrip('/') if value.startswith(('/opt/', '/var/lib/', '/etc/')) else real_path(value)
    for name in ('/opt/ttar', '/opt/ttar-control'):
        root = mapped(name)
        (root/'ttar').mkdir(parents=True)
        (root/'ttar'/'version.py').write_text('new')
        (root/'ttar.previous').mkdir()
        (root/'ttar.previous'/'version.py').write_text('old')
    mapped('/var/lib/ttar-release').mkdir(parents=True)
    mapped('/etc/systemd/system').mkdir(parents=True)
    db_path = tmp_path/'db.sqlite3'
    with sqlite3.connect(db_path) as db:
        db.executescript('''CREATE TABLE operations(id,chat_id,status,result,finished_at);
        CREATE TABLE outbox(dedupe UNIQUE,method,payload);
        CREATE TABLE games(id); INSERT INTO games VALUES (112);''')
        db.execute('INSERT INTO operations VALUES (1,-123,?,?,NULL)',
                   ('awaiting_reload', json.dumps({'commit': 'new'})))
    state = tmp_path/'state.json'
    state.write_text(json.dumps({'commit': 'new', 'old_commit': 'old', 'old_pid': None,
        'old_policy': '{"regular_minutes":150}', 'old_units': {'ttar-maintenance.service': 'old unit'},
        'database': str(db_path)}))
    monkeypatch.setattr(controller_guard, 'Path', mapped)
    ticks = iter([0, 181])
    monkeypatch.setattr(controller_guard.time, 'monotonic', lambda: next(ticks))
    commands = []
    monkeypatch.setattr(controller_guard.subprocess, 'run', lambda argv, **kw: commands.append(argv))
    controller_guard.guard(str(state))
    assert mapped('/opt/ttar-control/ttar/version.py').read_text() == 'old'
    assert mapped('/opt/ttar/ttar/version.py').read_text() == 'old'
    assert mapped('/etc/systemd/system/ttar-maintenance.service').read_text() == 'old unit'
    with sqlite3.connect(db_path) as db:
        assert db.execute('SELECT status FROM operations').fetchone()[0] == 'failed'
        assert db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 1
        assert db.execute('SELECT id FROM games').fetchone()[0] == 112
    assert commands[-1] == ['systemctl', 'restart', 'ttar-worker', 'ttar-ocr', 'ttar-maintenance']
