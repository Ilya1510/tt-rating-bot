import json
import http.client
import ipaddress
import re
import socket
import time


class TelegramError(RuntimeError):
    def __init__(self, code, retry_after=0, not_modified=False):
        self.code = code
        self.retry_after = retry_after
        self.not_modified = not_modified
        super().__init__(f'Telegram request failed ({code})')


def redact(text):
    text = re.sub(r'https://api\.telegram\.org/(?:file/)?bot[^\s\"\']+', '[TELEGRAM_URL]', str(text))
    return re.sub(r'(?:\d{6,}:[A-Za-z0-9_-]{20,}|(?:y[01]_|t[01]_|AQAD-|sk-)[A-Za-z0-9_-]+)', '[SECRET]', text)


class Telegram:
    def __init__(self, token, ipv4_address=None):
        self.token = token
        self.ipv4_address = str(ipaddress.IPv4Address(ipv4_address)) if ipv4_address else None

    def _read(self, path, payload=None, limit=1000000, timeout=25):
        # TLS always verifies api.telegram.org, even when TCP uses a selected IPv4.
        connection = http.client.HTTPSConnection('api.telegram.org', timeout=timeout)
        if self.ipv4_address:
            def connect(target, wait, source_address=None, **kwargs):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(wait)
                try:
                    sock.connect((self.ipv4_address, 443))
                except Exception:
                    sock.close()
                    raise
                return sock
            connection._create_connection = connect
        else:
            def connect(target, wait, source_address=None, **kwargs):
                # Retry only TCP establishment: no HTTP bytes have been sent yet.
                for attempt in range(3):
                    try:
                        sock = socket.create_connection(target, min(wait, 5), source_address)
                        sock.settimeout(wait)
                        return sock
                    except OSError:
                        if attempt == 2:
                            raise
                        time.sleep(.2)
            connection._create_connection = connect
        try:
            connection.request('POST' if payload is not None else 'GET', path,
                               body=json.dumps(payload).encode() if payload is not None else None,
                               headers={'Content-Type': 'application/json'})
            response = connection.getresponse()
            body = response.read(limit + 1)
            if response.status >= 400:
                try:
                    error = json.loads(body)
                    retry = error.get('parameters', {}).get('retry_after', 0)
                    not_modified = 'message is not modified' in error.get('description', '').lower()
                except Exception:
                    retry = 0
                    not_modified = False
                raise TelegramError(response.status, retry, not_modified)
            if len(body) > limit:
                raise TelegramError('response_too_large')
            return body
        except TelegramError:
            raise
        except Exception:
            raise TelegramError('transport') from None
        finally:
            connection.close()

    def call(self, method, **payload):
        try:
            result = json.loads(self._read(f'/bot{self.token}/{method}', payload))
        except TelegramError:
            raise
        except Exception:
            raise TelegramError('transport') from None
        if not result.get('ok'):
            raise TelegramError(result.get('error_code', 'unknown'))
        return result['result']

    def download(self, file_id):
        path = self.call('getFile', file_id=file_id)['file_path']
        if not re.fullmatch(r'[A-Za-z0-9_./-]+', path) or '..' in path:
            raise TelegramError('invalid_file_path')
        return self._read(f'/file/bot{self.token}/{path}', limit=20 * 1024 * 1024, timeout=30)

    def admin(self, chat_id, user_id):
        return self.call('getChatMember', chat_id=chat_id, user_id=user_id).get('status') in ('creator', 'administrator')

    def member(self, chat_id, user_id):
        return human_member(self.call('getChatMember', chat_id=chat_id, user_id=user_id))


def human_member(result):
    return not result.get('user', {}).get('is_bot', False) and (
        result.get('status') in ('creator', 'administrator', 'member')
        or (result.get('status') == 'restricted' and result.get('is_member') is True))
