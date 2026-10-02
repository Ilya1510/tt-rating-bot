#!/usr/bin/env python3
"""Root-only stdin credential installation; no secrets in argv/stdout."""
import json
import os
import subprocess
import sys
from pathlib import Path

if os.geteuid() != 0:
    raise SystemExit('Run as root')
data = json.load(sys.stdin)
if 'telegram_token' in data['credentials'] or 'cloud_function_key' not in data['credentials']:
    raise SystemExit('VM must use the private cloud transport without a Telegram token')
config_path = Path('/etc/ttar/config.json')
config = json.loads(config_path.read_text())
config.update(data['config'])
config_path.write_text(json.dumps(config))
config_path.chmod(0o644)
directory = Path('/etc/credstore.encrypted')
directory.mkdir(mode=0o700, exist_ok=True)
encoded = json.dumps(data['credentials']).encode()
target = directory/'ttar.json'
run = subprocess.run(['systemd-creds', 'encrypt', '--with-key=host', '--name=credentials.json', '-', str(target)],
                     input=encoded, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
if run.returncode:
    raise SystemExit('Could not encrypt systemd credentials; nothing printed')
target.chmod(0o600)
drop = Path('/etc/systemd/system/ttar-worker.service.d')
drop.mkdir(exist_ok=True)
(drop/'credentials.conf').write_text('[Service]\nLoadCredentialEncrypted=credentials.json:/etc/credstore.encrypted/ttar.json\n')
subprocess.run(['systemctl', 'daemon-reload'], check=True)
subprocess.run(['systemctl', 'restart', 'ttar-worker.service'], check=True)
print('VM configuration installed; worker restarted')
