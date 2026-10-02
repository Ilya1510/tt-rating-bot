#!/usr/bin/env python3
"""Read-only probe. Does not poll updates or change webhook."""
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ttar.telegram import Telegram, TelegramError

path = Path.home()/'.config/ttar/secrets.json'
if not path.exists():
    raise SystemExit('Telegram token file is missing')
try:
    tg = Telegram(json.loads(path.read_text())['telegram_token'])
    me = tg.call('getMe')
    info = tg.call('getWebhookInfo')
    parsed = urlsplit(info.get('url', ''))
    print(json.dumps({'bot': {k: me.get(k) for k in ('id','username','first_name','can_read_all_group_messages')},
                      'webhook': {'configured': bool(info.get('url')), 'host': parsed.hostname,
                                  'pending_updates': info.get('pending_update_count'),
                                  'has_error': bool(info.get('last_error_date'))}}, ensure_ascii=False))
except TelegramError as error:
    raise SystemExit(str(error)) from None
