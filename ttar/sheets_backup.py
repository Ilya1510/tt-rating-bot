"""Idempotent Google Sheets copy of committed history, outside the Telegram loop."""
import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

LOG = logging.getLogger('ttar.sheets_backup')
TITLE = 'История игр'
HEADERS = ['ID партии', 'Дата и время (МСК)', 'Фото ID', 'Партия на фото',
           'Игрок A', 'Игрок B', 'Очки A', 'Очки B', 'Победитель',
           'Elo A до', 'Elo A после', 'Изменение A', 'Elo B до', 'Elo B после',
           'Изменение B', 'Статус', 'Фото в Telegram', 'Игрок A ID', 'Игрок B ID', 'Время Unix']
TOKEN_URL = 'https://oauth2.googleapis.com/token'
SCOPE = 'https://www.googleapis.com/auth/spreadsheets'


class BackupError(Exception):
    """Only fixed messages or numeric HTTP codes; never response bodies or URLs."""


def snapshot(database, chat_id):
    # A single read transaction sees either side of a confirmation/recalculation.
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True, timeout=30) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        settings = dict(db.execute("SELECT key,value FROM settings WHERE key IN ('k','unit','elo_zero_baseline')"))
        rows = []
        for g in db.execute('''SELECT g.*,p.message_id,pa.name AS name_a,pb.name AS name_b
            FROM games g JOIN photos p ON p.id=g.photo_id
            JOIN players pa ON pa.id=g.a JOIN players pb ON pb.id=g.b
            WHERE p.chat_id=? AND p.status='confirmed'
            ORDER BY g.occurred_at,p.message_id,g.ordinal,g.id''', (chat_id,)):
            local = datetime.fromtimestamp(g['occurred_at'], ZoneInfo('Europe/Moscow'))
            serial = (local.replace(tzinfo=None) - datetime(1899, 12, 30)).total_seconds() / 86400
            delta_a = g['rating_a_after'] - g['rating_a_before'] if g['rating_a_after'] is not None else None
            delta_b = g['rating_b_after'] - g['rating_b_before'] if g['rating_b_after'] is not None else None
            link = f"https://t.me/c/{str(chat_id)[4:]}/{g['message_id']}" if str(chat_id).startswith('-100') else ''
            rows.append([g['id'], serial, g['photo_id'], g['ordinal'] + 1,
                         g['name_a'], g['name_b'], g['score_a'], g['score_b'],
                         g['name_a'] if g['score_a'] > g['score_b'] else g['name_b'],
                         g['rating_a_before'], g['rating_a_after'], delta_a,
                         g['rating_b_before'], g['rating_b_after'], delta_b,
                         'Учтена' if g['active'] else 'Отменена', link, g['a'], g['b'], g['occurred_at']])
    data = {'version': 1, 'headers': HEADERS, 'rows': rows, 'settings': settings}
    data['fingerprint'] = hashlib.sha256(json.dumps(data, ensure_ascii=False, allow_nan=False,
                                                   sort_keys=True).encode()).hexdigest()
    return data


def sheet_rows(data, exported_at):
    return [
        ['История настольного тенниса'],
        ['Обновлено (МСК)', datetime.fromtimestamp(exported_at, ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M:%S')],
        ['Подтверждённые партии; отменённые сохранены с отметкой. Начальный Elo: ' +
         ('0' if data['settings'].get('elo_zero_baseline') == 'true' else '1000') +
         '; K=' + data['settings'].get('k', '?')],
        data['headers'], *data['rows'],
    ]


def cell(value):
    if value is None:
        return {}
    # Explicit stringValue prevents names beginning with '=' becoming formulas.
    return {'userEnteredValue': {'numberValue' if isinstance(value, (int, float)) else 'stringValue': value}}


def update_requests(sheet_id, data, exported_at, grid_rows, grid_columns):
    values = sheet_rows(data, exported_at)
    size = max(len(values), grid_rows)
    grid = {'sheetId': sheet_id, 'startRowIndex': 0, 'endRowIndex': size,
            'startColumnIndex': 0, 'endColumnIndex': len(HEADERS)}
    return [
        {'updateSheetProperties': {'properties': {'sheetId': sheet_id, 'gridProperties': {
            'rowCount': size, 'columnCount': max(len(HEADERS), grid_columns), 'frozenRowCount': 4}},
            'fields': 'gridProperties.rowCount,gridProperties.columnCount,gridProperties.frozenRowCount'}},
        {'updateCells': {'range': grid, 'rows': [{'values': [cell(v) for v in row]} for row in values],
                         'fields': 'userEnteredValue'}},
        {'repeatCell': {'range': dict(grid, startRowIndex=4), 'cell': {'userEnteredFormat': {
            'textFormat': {'fontFamily': 'Arial', 'fontSize': 10}, 'verticalAlignment': 'MIDDLE'}},
            'fields': 'userEnteredFormat.textFormat,userEnteredFormat.verticalAlignment'}},
        {'repeatCell': {'range': dict(grid, startRowIndex=4, startColumnIndex=1, endColumnIndex=2),
                        'cell': {'userEnteredFormat': {'numberFormat': {'type': 'DATE_TIME', 'pattern': 'dd.mm.yyyy hh:mm'}}},
                        'fields': 'userEnteredFormat.numberFormat'}},
        {'repeatCell': {'range': dict(grid, startRowIndex=4, startColumnIndex=9, endColumnIndex=15),
                        'cell': {'userEnteredFormat': {'numberFormat': {'type': 'NUMBER', 'pattern': '0.00'}}},
                        'fields': 'userEnteredFormat.numberFormat'}},
        {'setBasicFilter': {'filter': {'range': dict(grid, startRowIndex=3, endRowIndex=len(values))}}},
    ]


def request_json(url, *, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise BackupError('Google HTTP ' + str(error.code)) from None
    except Exception:
        raise BackupError('Google connection or response failed') from None


class SheetsClient:
    def __init__(self, credentials):
        if credentials.get('type') != 'service_account':
            raise BackupError('Service account credential required')
        self.credentials = credentials
        self.token = None
        self.expires = 0

    def access_token(self):
        if self.token and time.time() < self.expires:
            return self.token
        def encode(value):
            return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).rstrip(b'=')
        now = int(time.time())
        signing = encode({'alg': 'RS256', 'typ': 'JWT'}) + b'.' + encode({
            'iss': self.credentials['client_email'], 'scope': SCOPE, 'aud': TOKEN_URL,
            'iat': now, 'exp': now + 3600})
        key = serialization.load_pem_private_key(self.credentials['private_key'].encode(), password=None)
        signature = key.sign(signing, padding.PKCS1v15(), hashes.SHA256())
        assertion = (signing + b'.' + base64.urlsafe_b64encode(signature).rstrip(b'=')).decode()
        result = request_json(TOKEN_URL, data=urllib.parse.urlencode({
            'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer', 'assertion': assertion}).encode(),
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        self.token = result['access_token']
        self.expires = now + int(result['expires_in']) - 120
        return self.token

    def call(self, spreadsheet_id, suffix='', body=None):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', spreadsheet_id):
            raise BackupError('Invalid spreadsheet ID')
        try:
            return request_json('https://sheets.googleapis.com/v4/spreadsheets/' + spreadsheet_id + suffix,
                data=None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode(),
                headers={'Authorization': 'Bearer ' + self.access_token(), 'Content-Type': 'application/json'})
        except BackupError as error:
            if str(error) == 'Google HTTP 401':
                self.token = None
            raise

    def write(self, config, data, exported_at):
        meta = self.call(config['spreadsheet_id'], '?fields=sheets.properties')
        sheet = next((s['properties'] for s in meta['sheets']
                      if s['properties']['sheetId'] == config['sheet_id']), None)
        if sheet is None or sheet['title'] != TITLE:
            raise BackupError('Backup sheet is missing or renamed')
        props = sheet['gridProperties']
        requests = update_requests(config['sheet_id'], data, exported_at, props['rowCount'], props['columnCount'])
        # One atomic, retry-safe overwrite; never clear separately or append.
        self.call(config['spreadsheet_id'], ':batchUpdate', {'requests': requests})


def sync_once(database, chat_id, config, client, state_path, now=None):
    now = time.time() if now is None else now
    data = snapshot(database, chat_id)
    path = Path(state_path)
    state = json.loads(path.read_text()) if path.exists() else {}
    target = [config['spreadsheet_id'], config['sheet_id']]
    ids = [row[0] for row in data['rows']]
    if state.get('target') == target:
        if set(state.get('game_ids', [])) - set(ids):
            raise BackupError('History lost game IDs; refusing to overwrite backup')
        if state.get('fingerprint') == data['fingerprint']:
            return False
    if not ids:
        return False
    client.write(config, data, now)
    state = {'target': target, 'fingerprint': data['fingerprint'], 'game_ids': ids,
             'last_success': now, 'game_count': len(ids)}
    staged = path.with_suffix('.next')
    with staged.open('w') as stream:
        os.chmod(staged, 0o600)
        json.dump(state, stream)
        stream.flush()
        os.fsync(stream.fileno())
    staged.replace(path)
    LOG.info('Google backup synchronized: games=%s', len(ids))
    return True


def run(config, credential_path, stop):
    failures = 0
    client = None
    state = Path(config['database']).parent/'google-backup-state.json'
    while not stop.is_set():
        try:
            if client is None:
                client = SheetsClient(json.loads(Path(credential_path).read_text()))
            sync_once(config['database'], config['allowed_chat_id'], config['google_backup'], client, state)
            failures = 0
            delay = 10
        except Exception as error:
            failures += 1
            # Do not leak credentials, HTTP bodies, names or game data into logs.
            detail = str(error) if isinstance(error, BackupError) else type(error).__name__
            LOG.warning('Google backup failed: %s; retry=%s', detail, failures)
            delay = min(300, 10 * 2 ** min(failures, 5))
        stop.wait(delay)
