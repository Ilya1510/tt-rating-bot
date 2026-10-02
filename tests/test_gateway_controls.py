import json
import threading

import pytest

from ttar.bot import Bot, card_update
from ttar.core import Store
from ttar.webhook import accept_gateway, cloud_handler
from test_bot_webhook import FakeTelegram, event
from test_core import raw, photo, store


def callback(update_id=1, actor=42, pid=1):
    return {'update_id': update_id, 'callback_query': {'id': str(update_id),
        'from': {'id': actor}, 'data': f'confirm:{pid}:1',
        'message': {'message_id': 55, 'chat': {'id': -123}}}}


def test_webhook_ack_only_after_durable_publish_and_authenticated_chat():
    saved = []
    result = accept_gateway(event(callback()), 'secret', -123, saved.append)
    assert result['statusCode'] == 200
    assert json.loads(result['body'])['method'] == 'answerCallbackQuery'
    assert json.loads(saved[0])['_cloud_callback_acknowledged'] is True
    assert json.loads(result['body'])['callback_query_id'] == '1'
    assert accept_gateway(event(callback(), 'wrong'), 'secret', -123, saved.append)['statusCode'] == 403
    ignored = accept_gateway(event(callback()), 'secret', -999, saved.append)
    assert ignored['body'] == 'Ignored' and len(saved) == 1
    def failed(_):
        raise RuntimeError('Queue down')
    failed_result = accept_gateway(event(callback()), 'secret', -123, failed)
    assert failed_result['statusCode'] == 503 and 'answerCallbackQuery' not in failed_result['body']


def test_public_gateway_cannot_reach_private_relay(monkeypatch):
    for name, value in {'TG_TOKEN': 'fake', 'TG_IPV4_ADDRESS': '127.0.0.1',
                        'ALLOWED_CHAT_ID': '-123', 'WEBHOOK_SECRET': 'secret'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr('ttar.telegram.Telegram.call', lambda *_args, **_kw: pytest.fail('Private relay reached'))
    for supplied in ('wrong', 'secret'):
        envelope = event({'telegram_action': 'sendMessage', 'payload': {'chat_id': -123}}, supplied)
        envelope['telegram_action'] = 'sendMessage'
        result = cloud_handler(envelope, None)
        assert result['statusCode'] == (403 if supplied == 'wrong' else 400)


def test_gateway_votes_update_existing_card_without_expired_callback(store):
    pid = photo(store)
    store.register_card(pid, 1, -123, 55, 'text')
    bot = Bot(store, FakeTelegram(), None, -123)
    for update_id, actor, expected in [(1, 42, 1), (2, 42, 1), (3, 43, 2)]:
        accept_gateway(event(callback(update_id, actor, pid)), 'secret', -123,
                       lambda body: store.ingest(json.loads(body)))
        bot.handle(store.claim(photos=False))
        assert store.vote_count(pid) == expected
        update = json.loads(store.db.execute("SELECT payload FROM outbox WHERE method='updateDraftCard' ORDER BY id DESC LIMIT 1").fetchone()[0])
        assert f'{expected}/2' in card_update(store, update)[1]['text']
    assert store.db.execute("SELECT count(*) FROM outbox WHERE method='answerCallbackQuery'").fetchone()[0] == 0
    assert store.db.execute("SELECT count(*) FROM outbox WHERE method='sendMessage'").fetchone()[0] == 0
    assert store.db.execute('SELECT count(*) FROM games').fetchone()[0] == 1


def test_photo_ocr_does_not_hold_confirmation_transaction(store):
    pid = photo(store)
    started, finish = threading.Event(), threading.Event()
    errors = []
    message = {'chat': {'id': -123}, 'from': {'id': 42}, 'message_id': 11, 'date': 200,
               'photo': [{'file_id': 'new', 'file_unique_id': 'new', 'width': 8, 'height': 8}]}
    store.ingest({'update_id': 1, 'message': message})
    store.ingest(callback(2, 42, pid))
    def process():
        connection = Store(store.path)
        try:
            def slow_ocr(_):
                started.set()
                assert finish.wait(5)
                return raw()
            Bot(connection, FakeTelegram(), slow_ocr, -123).handle(connection.claim(photos=True))
        except BaseException as error:
            errors.append(error)
        finally:
            connection.db.close()
    thread = threading.Thread(target=process)
    thread.start()
    try:
        assert started.wait(5)
        Bot(store, FakeTelegram(), None, -123).handle(store.claim(photos=False))
        assert store.vote_count(pid) == 1
        assert store.db.execute('SELECT status FROM jobs WHERE id=1').fetchone()[0] == 'processing'
        assert store.claim(photos=False) is None
    finally:
        finish.set()
        thread.join(6)
    assert not errors and not thread.is_alive()
    assert store.db.execute('SELECT status FROM jobs WHERE id=1').fetchone()[0] == 'done'


def test_acknowledged_callback_failure_still_explains_error(store):
    pid = photo(store)
    update = callback(1, 999, pid)
    update['_cloud_callback_acknowledged'] = True
    store.ingest(update)
    Bot(store, FakeTelegram(), None, -123).handle(store.claim(photos=False))
    assert store.vote_count(pid) == 0
    rows = store.db.execute('SELECT method,payload FROM outbox').fetchall()
    assert len(rows) == 1 and rows[0]['method'] == 'sendMessage'
    assert json.loads(rows[0]['payload'])['text']
