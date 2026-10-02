"""VM talks only to a private Cloud Function, never to the Telegram API."""
import base64
import json
import hashlib
import time
import urllib.error
import urllib.request
from datetime import datetime

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .telegram import TelegramError, human_member


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


class CloudTelegram:
    def __init__(self, endpoint, key):
        self.endpoint, self.key = endpoint, key
        self.iam_token, self.expires = None, 0

    def _iam(self):
        if self.iam_token and time.time() < self.expires - 300:
            return self.iam_token
        now = int(time.time())
        header = b64(json.dumps({'alg': 'PS256', 'typ': 'JWT', 'kid': self.key['id']}).encode())
        payload = b64(json.dumps({'aud': 'https://iam.api.cloud.yandex.net/iam/v1/tokens',
                                 'iss': self.key['service_account_id'], 'iat': now, 'exp': now + 600}).encode())
        message = (header + '.' + payload).encode()
        pem = self.key['private_key']
        pem = pem[pem.index('-----BEGIN'):]
        private = serialization.load_pem_private_key(pem.encode(), password=None)
        signature = private.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
        req = urllib.request.Request('https://iam.api.cloud.yandex.net/iam/v1/tokens',
            data=json.dumps({'jwt': message.decode() + '.' + b64(signature)}).encode(),
            headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                result = json.load(response)
            self.iam_token = result['iamToken']
            self.expires = datetime.fromisoformat(result['expiresAt'].replace('Z', '+00:00')).timestamp()
            return self.iam_token
        except Exception:
            raise TelegramError('cloud_auth') from None

    def _request(self, action, payload):
        req = urllib.request.Request(self.endpoint,
            data=json.dumps({'telegram_action': action, 'payload': payload}).encode(),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self._iam()})
        try:
            with urllib.request.urlopen(req, timeout=65) as response:
                body = response.read(9 * 1024 * 1024 + 1)
            if len(body) > 9 * 1024 * 1024:
                raise TelegramError('cloud_response_too_large')
            result = json.loads(body)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                self.iam_token = None
            raise TelegramError('cloud_http_' + str(error.code)) from None
        except TelegramError:
            raise
        except Exception:
            raise TelegramError('cloud_transport') from None
        if not result.get('ok'):
            raise TelegramError(result.get('code', 'cloud_failure'), result.get('retry_after', 0), result.get('not_modified', False))
        return result['result']

    def call(self, method, **payload):
        return self._request(method, payload)

    def download(self, file_id):
        try:
            data = bytearray()
            fingerprint = None
            total = None
            for _ in range(20):
                part = self._request('download', {'file_id': file_id, 'offset': len(data)})
                if total is None:
                    total, fingerprint = part['total'], part['sha256']
                    if type(total) is not int or not 0 < total <= 20 * 1024 * 1024:
                        raise ValueError()
                if part['total'] != total or part['sha256'] != fingerprint:
                    raise ValueError()
                chunk = base64.b64decode(part['data'], validate=True)
                if not chunk or len(chunk) > 1024 * 1024:
                    raise ValueError()
                data.extend(chunk)
                if len(data) > total:
                    raise ValueError()
                if len(data) == total:
                    if hashlib.sha256(data).hexdigest() != fingerprint:
                        raise ValueError()
                    return bytes(data)
            raise ValueError()
        except TelegramError:
            raise
        except Exception:
            raise TelegramError('cloud_image') from None

    def admin(self, chat_id, user_id):
        return self.call('getChatMember', chat_id=chat_id, user_id=user_id).get('status') in ('creator', 'administrator')

    def member(self, chat_id, user_id):
        return human_member(self.call('getChatMember', chat_id=chat_id, user_id=user_id))
