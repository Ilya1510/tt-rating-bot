import json
import threading

from ttar.core import Store
from ttar.cloud_poller import run
from ttar.worker import telegram_client
from ttar import telegram
from types import SimpleNamespace


def test_direct_transport_uses_hostname_without_cloud_credentials():
    client = telegram_client({'telegram_transport': 'direct'}, {'telegram_token': 'test'})
    assert client.ipv4_address is None
    assert client.token == 'test'


def test_direct_poll_restart_deduplicates_before_acknowledging(tmp_path):
    path = tmp_path/'history.sqlite3'
    update = {'update_id': 42, 'message': {'chat': {'id': -1}, 'text': '/stat'}}
    for _ in range(2):
        stop = threading.Event()
        calls = []
        class Telegram:
            def call(self, method, **params):
                calls.append(params)
                if len(calls) == 1:
                    return [update]
                with Store(path).db as db:
                    assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
                stop.set()
                return []
        store = Store(path)
        assert run(stop, Telegram(), lambda b: store.ingest(json.loads(b)), -1) == 0
        store.db.close()
        assert calls[1]['offset'] == 43


def test_connect_failure_retries_before_sending_request(monkeypatch):
    connects, requests = [], []
    sock = SimpleNamespace(settimeout=lambda value: None)
    def connect(*args):
        connects.append(args)
        if len(connects) < 3:
            raise TimeoutError()
        return sock
    class Connection:
        def __init__(self, *a, **kw): pass
        def request(self, *args, **kwargs):
            self._create_connection(('api.telegram.org', 443), 35)
            requests.append(args)
        def getresponse(self):
            return SimpleNamespace(status=200, read=lambda _: b'{"ok":true,"result":{}}')
        def close(self): pass
    monkeypatch.setattr(telegram.socket, 'create_connection', connect)
    monkeypatch.setattr(telegram.http.client, 'HTTPSConnection', Connection)
    monkeypatch.setattr(telegram.time, 'sleep', lambda _: None)
    assert telegram.Telegram('test').call('sendMessage', text='test') == {}
    assert len(connects) == 3 and len(requests) == 1
