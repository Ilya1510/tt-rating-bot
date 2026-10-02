#!/usr/bin/env bash
set -euo pipefail
if [[ "$(hostname)" != ilya-grid-vm-* ]]; then
  echo 'Refusing to install on a host other than ilya-grid-vm' >&2
  exit 1
fi
getent group ttar >/dev/null || groupadd --system ttar
getent group ttar-ocr >/dev/null || groupadd --system ttar-ocr
id ttar >/dev/null 2>&1 || useradd --system --gid ttar --home-dir /var/lib/ttar --shell /usr/sbin/nologin ttar
id ttar-ocr >/dev/null 2>&1 || useradd --system --gid ttar-ocr --home-dir /var/lib/ttar-ocr --shell /usr/sbin/nologin ttar-ocr
usermod -aG ttar-ocr ttar
install -d -m 0755 /opt/ttar /opt/ttar/bin /etc/ttar
install -d -o ttar -g ttar -m 0700 /var/lib/ttar /var/backups/ttar
install -d -o ttar-ocr -g ttar-ocr -m 0700 /var/lib/ttar-ocr /var/lib/ttar-ocr/.codex
cp -r ttar deploy scripts tests requirements.txt requirements-dev.txt /opt/ttar/
chown -R root:root /opt/ttar
python3 -m venv --without-pip /opt/ttar/.venv
python3 - <<'PY'
import sysconfig, zipfile
from pathlib import Path
wheel = next(Path('/opt/ttar/deploy/wheels').glob('pip-*.whl'))
with zipfile.ZipFile(wheel) as archive:
    archive.extractall('/opt/ttar/.venv/lib/python3.12/site-packages')
PY
/opt/ttar/.venv/bin/python -m pip install -q --no-index --find-links=/opt/ttar/deploy/wheels -r /opt/ttar/requirements.txt
if [[ ! -x /opt/ttar/bin/codex ]]; then
  codex_source="$(readlink -f /home/ilya-grid/.local/bin/codex)"
  install -m 0755 "$codex_source" /opt/ttar/bin/codex
  install -m 0755 "$(dirname "$codex_source")/codex-code-mode-host" /opt/ttar/bin/codex-code-mode-host
fi
if [[ ! -e /etc/ttar/config.json ]]; then
  cat > /etc/ttar/config.json <<'JSON'
{"database":"/var/lib/ttar/history.sqlite3","queue_url":null,"allowed_chat_id":null,"recognizer_socket":"/run/ttar-ocr/ocr.sock"}
JSON
  chmod 0644 /etc/ttar/config.json
fi
cd /opt/ttar
if [[ ! -e /var/lib/ttar/history.sqlite3 ]]; then
  sudo -u ttar /opt/ttar/.venv/bin/python -m ttar.admin configure --unit game --k 32
fi
install -m 0644 deploy/ttar-*.service deploy/ttar-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ttar-ocr.service ttar-worker.service ttar-backup.timer
systemctl start ttar-backup.service
echo 'TTAR services installed; cloud and Telegram configuration may still be pending.'
