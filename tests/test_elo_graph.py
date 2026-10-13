import base64
import io
import json
from datetime import date

import pytest
from PIL import Image

from ttar.bot import Bot, ROSTER
from ttar.core import Store
from ttar.elo_graph import daily_history, game_history, render_graph
from ttar.telegram import Telegram, request_body
from ttar.webhook import relay_action
from test_core import photo, approve, raw


def test_history_order_daily_totals_and_undo(tmp_path):
    store = Store(tmp_path / 'test.db')
    store.db.execute("UPDATE settings SET value='true' WHERE key='configured'")
    for alias, name in ROSTER:
        store.add_player(alias, name, None, 9)
    later = photo(store, 'later', at=172800, data=raw('Р', 'В'))
    earlier = photo(store, 'earlier', at=86400)
    draft = photo(store, 'draft', at=259200)
    approve(store, later, 1)
    approve(store, earlier, 1)
    history = daily_history(store, ROSTER)
    assert [day for day, _ in history] == [date(1970, 1, 1), date(1970, 1, 2), date(1970, 1, 3)]
    assert history[0][1] == [1000] * 4
    assert history[1][1] == [1016, 984, 1000, 1000]
    assert history[-1][1] == [1016, 984, 1016, 984]
    store.db.execute('UPDATE games SET active=0 WHERE photo_id=?', (earlier,))
    store.recalculate()
    assert daily_history(store, ROSTER)[-1][1] == [1000, 1000, 1016, 984]
    assert game_history(store, ROSTER) == [(0, [1000] * 4), (1, [1000, 1000, 1016, 984])]
    assert store.photo(draft)['status'] == 'draft'
    store.db.close()


def test_same_day_and_final_ratings(tmp_path):
    store = Store(tmp_path / 'test.db')
    store.db.execute("UPDATE settings SET value='true' WHERE key='configured'")
    for alias, name in ROSTER:
        store.add_player(alias, name, None, 9)
    for unique, at, score in [('second', 86402, (8, 11)), ('first', 86401, (11, 8))]:
        approve(store, photo(store, unique, at, raw(score=score)), 1)
    history = daily_history(store, ROSTER)
    assert len(history) == 2
    games = game_history(store, ROSTER)
    assert [number for number, _ in games] == [0, 1, 2]
    assert games[0][1] == [1000] * 4
    assert games[1][1] == [1016, 984, 1000, 1000]
    assert games[2][1] == history[-1][1]
    assert games[2][1][0] < games[1][1][0]
    assert games[2][1][1] > games[1][1][1]
    assert games[2][1][2:] == games[1][1][2:]
    for i, (alias, _) in enumerate(ROSTER):
        row = store.db.execute('SELECT rating FROM ratings WHERE player_id=?', (store.player(alias),)).fetchone()
        assert history[-1][1][i] == pytest.approx(row[0] if row else 1000)
    store.db.close()


def test_command_png_outbox_dedup_and_group_filter(tmp_path):
    store = Store(tmp_path / 'test.db')
    store.db.execute("UPDATE settings SET value='true' WHERE key='configured'")
    bot = Bot(store, object(), None, -123)
    assert 'нет подтверждённых' in bot.command('/elo_graph', '/elo_graph', 42, False)[0]
    approve(store, photo(store), 1)
    for update_id, chat in [(1, -123), (2, -999)]:
        store.ingest({'update_id': update_id, 'message': {'chat': {'id': chat}, 'from': {'id': 42}, 'text': '/elo_graph@TestBot'}})
    jobs = store.db.execute('SELECT * FROM jobs ORDER BY id').fetchall()
    for job in [jobs[0], jobs[0], jobs[1]]:
        bot.handle(job)
    rows = store.db.execute("SELECT * FROM outbox WHERE method='sendPhoto'").fetchall()
    assert len(rows) == 2
    second = json.loads(rows[1]["payload"])
    assert second["chat_id"] == -123
    assert "после каждой партии" in second["caption"]
    second_image = Image.open(io.BytesIO(base64.b64decode(second["photo_base64"])))
    assert second_image.format == "PNG" and second_image.size == (1200, 720)
    payload = json.loads(rows[0]['payload'])
    png = base64.b64decode(payload['photo_base64'])
    image = Image.open(io.BytesIO(png))
    assert image.format == 'PNG' and image.size == (1200, 720)
    assert payload['chat_id'] == -123
    body, content_type = request_body(payload)
    assert content_type.startswith('multipart/form-data; boundary=')
    assert png in body and b'name="photo"; filename="elo.png"' in body
    assert b'photo_base64' not in body
    store.db.close()


def test_photo_relay_and_direct_transport(monkeypatch):
    png = render_graph([(date(2026, 1, 1), [1000] * 4), (date(2026, 1, 2), [1016, 984, 1000, 1000])])
    payload = {'chat_id': -123, 'photo_base64': base64.b64encode(png).decode()}
    requests = []
    class Connection:
        def __init__(self, *args, **kwargs): pass
        def request(self, *args, **kwargs): requests.append((args, kwargs))
        def getresponse(self):
            class Response:
                status = 200
                def read(self, limit): return b'{"ok":true,"result":{"message_id":1}}'
            return Response()
        def close(self): pass
    monkeypatch.setattr('ttar.telegram.http.client.HTTPSConnection', Connection)
    client = Telegram('test')
    assert relay_action({'telegram_action': 'sendPhoto', 'payload': payload}, client, -999)['code'] == 'forbidden_chat'
    assert not requests
    assert relay_action({'telegram_action': 'sendPhoto', 'payload': payload}, client, -123)['ok']
    assert requests[0][0][1] == '/bottest/sendPhoto'
    assert png in requests[0][1]['body']
    assert requests[0][1]['headers']['Content-Type'].startswith('multipart/form-data')
