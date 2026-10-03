"""Standalone guard copied before installing owner-authorized controller code."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


def guard(state_file):
    state = json.loads(Path(state_file).read_text())
    ready = Path('/var/lib/ttar-release/controller-ready.json')
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            status = json.loads(ready.read_text())
            if status.get('commit') == state['commit'] and Path(f"/proc/{status['pid']}").exists():
                return
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(2)
    # Do not interrupt an old controller that is still finishing its task.
    if state.get('old_pid') and Path(f"/proc/{state['old_pid']}").exists():
        return
    for target in ('/opt/ttar-control', '/opt/ttar'):
        root = Path(target)
        if (root/'ttar.previous').exists():
            shutil.rmtree(root/'ttar')
            (root/'ttar.previous').rename(root/'ttar')
    control = Path('/opt/ttar-control')
    (control/'commit').write_text(state['old_commit'])
    Path('/opt/ttar/booking-policy.json').write_text(state['old_policy'])
    for name, text in state['old_units'].items():
        path = Path('/etc/systemd/system')/name
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(text)
            path.chmod(0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True, capture_output=True)
    subprocess.run(['systemctl', 'restart', 'ttar-worker', 'ttar-ocr', 'ttar-maintenance'], check=True, capture_output=True)
    with sqlite3.connect(state['database'], timeout=30) as db:
        for op_id, chat_id, raw in db.execute("SELECT id,chat_id,result FROM operations WHERE status='awaiting_reload'").fetchall():
            result = json.loads(raw)
            if result.get('commit') != state['commit']:
                continue
            text = f'/work #{op_id}: новая версия контроллера не запустилась. Предыдущая версия восстановлена; данные игр сохранены.'
            result.update(status='failed', summary=text, rollback_status='restored')
            db.execute("UPDATE operations SET status='failed',finished_at=?,result=? WHERE id=?", (time.time(), json.dumps(result), op_id))
            db.execute('INSERT OR IGNORE INTO outbox(dedupe,method,payload) VALUES (?,?,?)',
                       (f'operation-result:{op_id}', 'sendMessage', json.dumps({'chat_id': chat_id, 'text': text})))
    Path('/var/lib/ttar-release/deployed.json').write_text(json.dumps({'commit': state['old_commit'], 'at': time.time()}))


if __name__ == '__main__':
    try:
        guard(sys.argv[1])
    except Exception:
        # No subprocess/provider output with possible credentials in journal.
        raise SystemExit('Controller rollback guard failed; operator check required') from None
