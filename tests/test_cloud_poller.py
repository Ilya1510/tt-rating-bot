import json
import threading

from ttar.cloud_poller import run
from ttar.telegram import TelegramError


def update(i, chat=-1):
    return {'update_id': i, 'message': {'chat': {'id': chat}, 'text': '/stat'}}


def test_offset_advances_only_after_whole_batch_is_persisted():
    class Stop:
        stopped = False
        def is_set(self): return self.stopped
        def wait(self, seconds): pass
    stop = Stop()
    calls = []
    writes = []
    class Telegram:
        def call(self, method, **kwargs):
            calls.append(kwargs)
            if len(calls) == 3:
                stop.stopped = True
                return []
            return [update(10), update(11)]
    def publish(body):
        i = json.loads(body)['update_id']
        writes.append(i)
        if writes == [10, 11]:
            raise RuntimeError('queue down')
    assert run(stop, Telegram(), publish, -1) == 0
    assert [c.get('offset') for c in calls] == [None, None, 12]
    assert writes == [10, 11, 10, 11]
    assert all(c['timeout'] == 25 for c in calls)


def test_foreign_chat_is_ignored_but_acknowledged():
    stop = threading.Event()
    calls = []
    class Telegram:
        def call(self, method, **kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                stop.set()
                return []
            return [update(20, chat=-2)]
    assert run(stop, Telegram(), lambda _: (_ for _ in ()).throw(AssertionError()), -1) == 0
    assert calls[1]['offset'] == 21


def test_conflicting_poller_fails_closed_without_spinning():
    class Telegram:
        def call(self, method, **kwargs): raise TelegramError(409)
    assert run(threading.Event(), Telegram(), lambda _: None, -1) == 78
