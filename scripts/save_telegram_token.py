#!/usr/bin/env python3
"""Run interactively in a local terminal; never prints the entered token."""
import getpass
import json
import os
import re
from pathlib import Path

path = Path.home() / '.config' / 'ttar' / 'secrets.json'
path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
token = getpass.getpass('Токен НОВОГО бота из BotFather (ввод скрыт): ').strip()
if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}', token):
    raise SystemExit('Неверный формат токена; файл не изменён.')
data = json.loads(path.read_text()) if path.exists() else {}
data['telegram_token'] = token
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.fchmod(fd, 0o600)
with os.fdopen(fd, 'w') as out:
    json.dump(data, out)
print('Токен сохранён в защищённый локальный файл. Значение не выводится.')
