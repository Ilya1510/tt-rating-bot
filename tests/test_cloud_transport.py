import base64
import io
import json
import time
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ttar.cloud_telegram import CloudTelegram
from ttar.telegram import Telegram, TelegramError
from ttar.webhook import relay_action


def test_cloud_relay_restricts_methods_chats_and_preserves_rate_limit():
    class Fake:
        def call(self, method, **payload):
            raise TelegramError(429, 42)
        def download(self, file_id):
            return b'photo bytes'
    fake = Fake()
    assert relay_action({'telegram_action': 'getUpdates'}, fake, -123)['code'] == 'forbidden_method'
    assert relay_action({'telegram_action': 'sendMessage', 'payload': {'chat_id': -999}}, fake, -123)['code'] == 'forbidden_chat'
    result = relay_action({'telegram_action': 'sendMessage', 'payload': {'chat_id': -123}}, fake, -123)
    assert result == {'ok': False, 'code': 429, 'retry_after': 42}
    result = relay_action({'telegram_action': 'download', 'payload': {'file_id': 'abc'}}, fake, -123)
    assert base64.b64decode(result['result']['data']) == b'photo bytes'


def test_selected_ipv4_keeps_tls_hostname(monkeypatch):
    observations = {}
    class Socket:
        def settimeout(self, value): pass
        def connect(self, address): observations['tcp_address'] = address
    class Connection:
        def __init__(self, host, timeout): observations['tls_host'] = host
        def request(self, *args, **kwargs): self._create_connection(('api.telegram.org', 443), 25)
        def getresponse(self): return type('Response', (), {'status': 200, 'read': lambda self, n: b'{"ok": true, "result": {"id": 1}}'})()
        def close(self): pass
    monkeypatch.setattr('ttar.telegram.http.client.HTTPSConnection', Connection)
    monkeypatch.setattr('ttar.telegram.socket.socket', lambda *args: Socket())
    assert Telegram('test', '149.154.167.220').call('getMe')['id'] == 1
    assert observations == {'tls_host': 'api.telegram.org', 'tcp_address': ('149.154.167.220', 443)}


def test_cloud_adapter_signs_ps256_and_contacts_only_cloud(monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    key = {'id': 'key-id', 'service_account_id': 'account-id', 'private_key': 'PLEASE DO NOT REMOVE THIS LINE!\n' + pem}
    calls = []
    def open_request(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            jwt = json.loads(request.data)['jwt']
            header, payload, signature = jwt.split('.')
            decode = lambda part: base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))
            private.public_key().verify(decode(signature), (header + '.' + payload).encode(),
                                        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
            claims = json.loads(decode(payload))
            assert claims['iss'] == 'account-id' and claims['exp'] > time.time()
            result = {'iamToken': 'test-iam', 'expiresAt': datetime.fromtimestamp(time.time() + 3600, timezone.utc).isoformat()}
        else:
            assert request.get_header('Authorization') == 'Bearer test-iam'
            result = {'ok': True, 'result': {'status': 'administrator'}}
        return io.BytesIO(json.dumps(result).encode())
    monkeypatch.setattr('ttar.cloud_telegram.urllib.request.urlopen', open_request)
    adapter = CloudTelegram('https://functions.yandexcloud.net/own?integration=raw', key)
    assert adapter.admin(-123, 9)
    assert adapter.admin(-123, 9)
    assert len(calls) == 3 and all('telegram.org' not in url for url in calls)


def test_large_cloud_photo_is_chunked_with_integrity_check(monkeypatch):
    image = b'x' * (3 * 1024 * 1024 + 500)
    class Fake:
        def download(self, file_id): return image
    adapter = CloudTelegram('https://functions.yandexcloud.net/own', {})
    def request(action, payload):
        result = relay_action({'telegram_action': action, 'payload': payload}, Fake(), -123)
        assert len(json.dumps(result)) < 3500000
        return result['result']
    monkeypatch.setattr(adapter, '_request', request)
    assert adapter.download('file-id') == image
    monkeypatch.setattr(adapter, '_request', lambda *args: {'data': 'eA==', 'total': 1, 'sha256': 'incorrect'})
    with pytest.raises(TelegramError, match='cloud_image'):
        adapter.download('file-id')
