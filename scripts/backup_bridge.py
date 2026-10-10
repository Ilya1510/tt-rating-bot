"""Consistent local bridge snapshots; run as the bridge service user."""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

root = Path('/var/backups/tg-vk-bridge')
target = root/(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%SZ') + '.sqlite3')
with sqlite3.connect('/var/lib/tg-vk-bridge/history.sqlite3') as source, sqlite3.connect(target) as dest:
    source.backup(dest)
os.chmod(target, 0o600)
for path in sorted(root.glob('*.sqlite3'))[:-30]:
    path.unlink()
print('Bridge database backup complete')
