#!/usr/bin/env python3
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '/opt/ttar')
from ttar.core import Store

config = json.loads(Path('/etc/ttar/config.json').read_text())
root = Path('/var/backups/ttar')
target = root/(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%SZ') + '.sqlite3')
Store(config['database']).backup(target)
os.chmod(target, 0o600)
for path in sorted(root.glob('*.sqlite3'))[:-30]:
    path.unlink()
print('Database backup complete')
