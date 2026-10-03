import json
import io
from PIL import Image

import pytest

from ttar.bot import Bot, matrix_values, elo_chase
from ttar.core import Store, elo
from ttar.telegram import redact
from ttar.webhook import accept, accept_trigger, poll_updates
from test_core import raw, approve


def event(update, token='secret'):
    return {'httpMethod': 'POST', 'headers': {'X-Telegram-Bot-Api-Secret-Token': token}, 'body': json.dumps(update)}


def test_webhook_auth_filter_and_durable_ack():
    published = []
    update = {'update_id': 1, 'message': {'chat': {'id': -123}, 'text': '/top'}}
    assert accept(event(update), 'secret', -123, published.append)['statusCode'] == 200
    assert len(published) == 1
    assert accept(event(update, 'wrong'), 'secret', -123, published.append)['statusCode'] == 403
    assert accept(event(update), 'secret', -999, published.append)['statusCode'] == 200
    assert len(published) == 1
    def failed(_):
        raise RuntimeError('queue down')
    assert accept(event(update), 'secret', -123, failed)['statusCode'] == 503


def test_native_trigger_retries_failed_persistence_and_filters_chats():
    update = {'update_id': 1, 'message': {'date': 200, 'chat': {'id': -123}, 'text': '/top'}}
    published = []
    assert accept_trigger(update, -123, published.append, 100)['body'] == 'Saved'
    assert json.loads(published[0]) == update
    assert accept_trigger(update, -999, published.append, 100)['body'] == 'Ignored'
    assert accept_trigger(update, -123, published.append, 300)['body'] == 'Old message ignored'
    assert len(published) == 1
    def failed(_):
        raise RuntimeError('queue down')
    with pytest.raises(RuntimeError, match='persistence failed'):
        accept_trigger(update, -123, failed, 100)


def test_cloud_poll_acknowledges_only_persisted_batches():
    updates = [{'update_id': i, 'message': {'date': 200, 'chat': {'id': -123}, 'text': '/top'}} for i in (10, 11)]
    saved, calls = [], []
    def call(method, **options):
        calls.append(options)
        if 'offset' in options:
            assert len(saved) == 2
            assert options['offset'] == 12
            return []
        return updates
    assert poll_updates(call, saved.append, -123, 100) == {'received': 2, 'saved': 2}
    assert len(calls) == 2


def test_cloud_poll_failure_leaves_batch_unacknowledged():
    updates = [{'update_id': i, 'message': {'date': 200, 'chat': {'id': -123}, 'text': '/top'}} for i in (10, 11)]
    calls, saved = [], []
    def call(method, **options):
        calls.append(options)
        return updates
    def publish(body):
        if saved:
            raise RuntimeError('queue down')
        saved.append(body)
    with pytest.raises(RuntimeError, match='persistence failed'):
        poll_updates(call, publish, -123, 100)
    assert len(saved) == 1
    assert all('offset' not in c for c in calls)


def test_cloud_poll_leaves_next_response_unacknowledged_at_batch_limit():
    def update(i):
        return {'update_id': i, 'message': {'date': 200, 'chat': {'id': -123}, 'text': '/top'}}
    calls, saved = [], []
    def call(method, **options):
        calls.append(options)
        return [update(11)] if 'offset' in options else [update(10)]
    assert poll_updates(call, saved.append, -123, 100, max_batches=1) == {'received': 1, 'saved': 1}
    assert json.loads(saved[0])['update_id'] == 10
    assert calls[-1]['offset'] == 11  # update 11 itself has not been acknowledged


class FakeTelegram:
    def __init__(self):
        self.downloads = 0
    def download(self, file_id):
        self.downloads += 1
        out = io.BytesIO()
        Image.new('RGB',(8,8),'white').save(out, format='PNG')
        return out.getvalue()
    def admin(self, chat_id, user_id):
        return user_id == 9
    def member(self, chat_id, user_id):
        return user_id in (9, 42, 43)


@pytest.mark.parametrize('ratings,k,count', [
    ((1000, 1000), 32, 1), ((1100, 1000), 32, 0),
    ((1000, 1100), 32, 3), ((1000, 1400), 200, 2),
    ((1000.1, 1000.2), 32, 1),
])
def test_elo_chase_minimum_wins_without_mutations(tmp_path, ratings, k, count):
    s = Store(tmp_path/'test.sqlite3', k=k)
    s.db.execute("UPDATE settings SET value='true' WHERE key='configured'")
    for alias, rating in zip(('И', 'М'), ratings):
        pid = s.player(alias, create=True)
        s.db.execute('INSERT INTO ratings VALUES (?,?,0,0)', (pid, rating))
    before = list(s.db.iterdump())
    response, keyboard = elo_chase(s)
    assert f': {count}' in response or f'нужно {count}' in response
    assert keyboard is None
    assert list(s.db.iterdump()) == before
    a, b = ratings
    for _ in range(count):
        assert a <= b
        a, b = elo(a, b, 0, k)
    assert a > b


def test_elo_chase_reply_is_deduplicated_and_uses_initial_rating(tmp_path):
    s = Store(tmp_path/'test.sqlite3')
    s.db.execute("UPDATE settings SET value='true' WHERE key='configured'")
    for alias in ('И', 'М'):
        s.player(alias, create=True)
    bot = Bot(s, FakeTelegram(), lambda _: pytest.fail('OCR called'), -123)
    s.ingest({'update_id': 1, 'message': {'chat': {'id': -123},
              'from': {'id': 42}, 'text': '/elo_chase'}})
    job = s.claim()
    bot.handle(job)
    bot.handle(job)
    replies = s.db.execute('SELECT * FROM outbox').fetchall()
    assert len(replies) == 1
    assert replies[0]['method'] == 'sendMessage'
    assert 'побед подряд над Максом: 1' in json.loads(replies[0]['payload'])['text']
    assert s.db.execute('SELECT count(*) FROM games').fetchone()[0] == 0
    assert s.db.execute('SELECT count(*) FROM ratings').fetchone()[0] == 0


def test_elo_chase_requires_configured_model_and_known_players(tmp_path):
    s = Store(tmp_path/'test.sqlite3')
    with pytest.raises(ValueError, match='не настроена'):
        elo_chase(s)
    s.db.execute("UPDATE settings SET value='true' WHERE key='configured'")
    with pytest.raises(ValueError, match='Неизвестный игрок'):
        elo_chase(s)


def test_full_photo_draft_confirm_pair_amend(tmp_path):
    s = Store(tmp_path/'test.sqlite3')
    s.db.execute("UPDATE settings SET value='true' WHERE key='configured'")
    tg = FakeTelegram()
    calls = []
    def ocr(image):
        calls.append(image)
        return raw()
    bot = Bot(s, tg, ocr, -123)
    def handle(update):
        s.ingest(update)
        bot.handle(s.claim())
    message = {'chat': {'id': -123}, 'from': {'id': 42}, 'message_id': 11, 'date': 100,
               'photo': [{'file_id': 'one', 'file_unique_id': 'unique', 'width': 10, 'height': 10}]}
    handle({'update_id': 1, 'message': message})
    assert s.db.execute('SELECT count(*) FROM games').fetchone()[0] == 0
    for update_id, actor in ((2, 42), (3, 43)):
        handle({'update_id': update_id, 'callback_query': {'id': str(update_id), 'from': {'id': actor},
                    'message': {'chat': {'id': -123}}, 'data': 'confirm:1:1'}})
    assert s.db.execute('SELECT count(*) FROM games').fetchone()[0] == 1
    handle({'update_id': 4, 'message': message})
    assert len(calls) == 1
    with s.transaction():
        assert matrix_values(s, 1000)[0][0][1] == 1
        s.amend(1, 9, True)
        assert matrix_values(s, 1000)[0][0][1] == 0
    assert s.db.execute("SELECT count(*) FROM outbox WHERE method='sendMessage'").fetchone()[0] == 2


def test_other_chat_never_calls_ocr(tmp_path):
    s = Store(tmp_path/'test.sqlite3')
    bot = Bot(s, FakeTelegram(), lambda _: pytest.fail('OCR called'), -123)
    s.ingest({'update_id': 1, 'message': {'chat': {'id': 999}, 'photo': [{}]}})
    bot.handle(s.claim())
    assert s.db.execute('SELECT count(*) FROM photos').fetchone()[0] == 0


def test_secret_redaction():
    token = '123456789:' + 'x' * 35
    text = redact(f'https://api.telegram.org/bot{token}/getMe https://api.telegram.org/file/bot{token}/photos/a.jpg {token}')
    assert token not in text
    assert '[TELEGRAM_URL]' in text


def test_old_updates_not_imported():
    published = []
    update = {'update_id': 1, 'message': {'chat': {'id': -123}, 'date': 99, 'photo': [{}]}}
    assert accept(event(update), 'secret', -123, published.append, accept_from=100)['statusCode'] == 200
    assert published == []


def test_non_ascii_header_rejected():
    update = {'update_id': 1, 'message': {'chat': {'id': -123}, 'text': '/top'}}
    assert accept(event(update, 'секрет'), 'secret', -123, lambda _: None)['statusCode'] == 403


def test_database_failure_after_confirmation_rolls_back_rating(tmp_path, monkeypatch):
    s = Store(tmp_path/'test.sqlite3')
    s.db.execute("UPDATE settings SET value='true' WHERE key='configured'")
    with s.transaction():
        s.put_photo(-123, 10, 'u', 'h', 100, 42, raw())
        s.confirm(1, 1, 43, True)
    s.ingest({'update_id': 1, 'callback_query': {'id': 'callback', 'from': {'id': 42},
                'message': {'chat': {'id': -123}}, 'data': 'confirm:1:1'}})
    bot = Bot(s, FakeTelegram(), lambda _: raw(), -123)
    original_send = s.send
    def fail(*args):
        raise RuntimeError('disk failure')
    monkeypatch.setattr(s, 'send', fail)
    with pytest.raises(RuntimeError):
        bot.handle(s.claim())
    assert s.db.execute('SELECT count(*) FROM games').fetchone()[0] == 0
    assert s.photo(1)['status'] == 'draft'
    assert s.vote_count(1) == 1
    monkeypatch.setattr(s, 'send', original_send)
    s.recover()
    bot.handle(s.claim())
    assert s.db.execute('SELECT count(*) FROM games').fetchone()[0] == 1


def test_pair_tracks_both_player_orientations(tmp_path):
    s = Store(tmp_path/'test.sqlite3')
    s.db.execute("UPDATE settings SET value='true' WHERE key='configured'")
    bot = Bot(s, FakeTelegram(), lambda _: raw(), -123)
    with s.transaction():
        p, _ = s.put_photo(-123, 10, 'u', 'h', 100, 42, raw())
        s.fix_draft(p, [['М','И',11,8], ['И','М',11,7], ['М','И',12,10]], 42, False)
        approve(s,p, 2,42,True)
        matrix, _, _ = matrix_values(s, 1000)
        assert matrix[0][1] == 2 and matrix[1][0] == 1
