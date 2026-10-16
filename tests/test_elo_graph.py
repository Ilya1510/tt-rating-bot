import base64
import io
import json
from datetime import date

import pytest
from PIL import Image

from ttar.bot import Bot, ROSTER
from ttar.core import Store
from ttar.elo_graph import (daily_history, game_history, render_graph,
                            smooth_history, peak_indices, session_history, COLORS)
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
    assert history[0][1] == [0] * 4
    assert history[1][1] == [16, -16, 0, 0]
    assert history[-1][1] == [16, -16, 16, -16]
    store.db.execute('UPDATE games SET active=0 WHERE photo_id=?', (earlier,))
    store.recalculate()
    assert daily_history(store, ROSTER)[-1][1] == [0, 0, 16, -16]
    assert game_history(store, ROSTER) == [(0, [0] * 4), (1, [0, 0, 16, -16])]
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
    assert games[0][1] == [0] * 4
    assert games[1][1] == [16, -16, 0, 0]
    assert games[2][1] == history[-1][1]
    assert games[2][1][0] < games[1][1][0]
    assert games[2][1][1] > games[1][1][1]
    assert games[2][1][2:] == games[1][1][2:]
    for i, (alias, _) in enumerate(ROSTER):
        row = store.db.execute('SELECT rating FROM ratings WHERE player_id=?', (store.player(alias),)).fetchone()
        assert history[-1][1][i] == pytest.approx(row[0] if row else 0)
    store.db.close()


def test_command_png_outbox_dedup_and_group_filter(tmp_path, monkeypatch):
    from PIL import ImageDraw
    original = ImageDraw.ImageDraw.text
    titles = []
    legends = []
    def record(draw, xy, text, *args, **kwargs):
        if xy == (90, 20):
            titles.append(text)
        if xy[1] == 57:
            legends.append(text)
        return original(draw, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', record)
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
    rows = store.db.execute("SELECT * FROM outbox WHERE method='sendMediaGroup'").fetchall()
    assert len(rows) == 1
    assert not store.db.execute("SELECT * FROM outbox WHERE method='sendPhoto'").fetchall()
    payload = json.loads(rows[0]['payload'])
    assert payload['chat_id'] == -123
    assert len(payload['media']) == 3
    # Rehandling renders again, but creates no second outbox delivery.
    assert titles == ['Elo / by day', 'Elo / all games', 'Elo / last photo'] * 2
    assert legends == (['Maxim', 'Ilya', 'Roma', 'Valya'] * 2 + ['Maxim', 'Ilya']) * 2
    body, content_type = request_body(payload)
    assert content_type.startswith('multipart/form-data; boundary=')
    for index, item in enumerate(payload['media']):
        assert item['type'] == 'photo' and 'caption' not in item
        png = base64.b64decode(item['photo_base64'])
        image = Image.open(io.BytesIO(png))
        assert image.format == 'PNG' and image.size == (1200, 720)
        assert png in body
        assert f'"media": "attach://photo_{index}"'.encode() in body
        assert f'name="photo_{index}"; filename="elo.png"'.encode() in body
    assert b'photo_base64' not in body
    assert 'media' not in payload['media'][0]  # Encoding must not mutate the outbox payload.
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


@pytest.mark.parametrize('games', [1, 149, 299, 300, 301, 598, 599, 1000, 10000])
def test_smoothing_equal_blocks_and_remainder(games):
    history = [(i, [1000 + i] * 4) for i in range(games + 1)]
    result = smooth_history(history)
    assert result[0] == history[0] and result[-1] == history[-1]
    assert len({i for i, _ in result}) == len(result)
    assert len(result) <= 300
    if games >= 299:
        assert len(result) >= 150
    gaps = [b[0] - a[0] for a, b in zip(result, result[1:])]
    assert all(g == gaps[0] for g in gaps[:-1])
    assert 0 < gaps[-1] <= gaps[0]
    assert all(values == history[i][1] for i, values in result)


def test_personal_peaks_reversals_strict_windows_and_ties():
    def history(values):
        return [(i, [v, 1000, 1000, 1000]) for i, v in enumerate(values)]
    assert peak_indices(history([0, 1, 2, 1, 0]), 0) == [2]
    assert peak_indices(history([2, 1, 0, 1, 2]), 0) == [2]
    assert peak_indices(history([0, 1, 1, 2, 2, 1, 1, 0]), 0) == [3]
    assert peak_indices(history([0, 1, 2, 3, 4]), 0) == []
    # A strict five-game extremum without two consecutive wins before it.
    values = [1, 3, 2, 4, 0, 10, 5, 6, 3, 4, 2]
    assert peak_indices(history(values), 0) == [5]
    values[1] = 10
    assert peak_indices(history(values), 0) == []
    assert peak_indices(history([0, 1, 2, 1, 0]), 1) == []


def test_session_today_fallback_starting_ratings_and_inactive(tmp_path):
    store = Store(tmp_path / 'session.db')
    store.db.execute("UPDATE settings SET value='true' WHERE key='configured'")
    for alias, name in ROSTER:
        store.add_player(alias, name, None, 9)
    first = photo(store, 'first', at=86400)
    approve(store, first, 1)
    second = photo(store, 'second', at=172800, data=raw('Р', 'В'))
    approve(store, second, 1)
    third = photo(store, 'third', at=172801)
    approve(store, third, 1)
    recent, players, is_today = session_history(store, ROSTER, date(1970, 1, 3))
    assert is_today and players == [0, 1, 2, 3]
    assert recent == [(0, game_history(store, ROSTER)[1][1]),
                      (1, game_history(store, ROSTER)[2][1]),
                      (2, game_history(store, ROSTER)[3][1])]
    recent, players, is_today = session_history(store, ROSTER, date(2026, 1, 1))
    assert not is_today and players == [0, 1]
    assert recent[0][1] == game_history(store, ROSTER)[2][1]
    assert recent[1][1] == game_history(store, ROSTER)[3][1]
    store.db.execute('UPDATE games SET active=0 WHERE photo_id=?', (third,))
    recent, players, _ = session_history(store, ROSTER, date(2026, 1, 1))
    assert players == [2, 3] and len(recent) == 2
    store.db.close()


def test_requested_player_colors_in_png():
    assert COLORS == ('#ff0000', '#008000', '#000000', '#0000ff')
    png = render_graph([(0, [1000, 1100, 1200, 1300]),
                        (1, [1010, 1110, 1210, 1310])], by_game=True)
    image = Image.open(io.BytesIO(png))
    pixels = {image.getpixel((x, y)) for x in range(image.width) for y in range(image.height)}
    assert {(255, 0, 0), (0, 128, 0), (0, 0, 0), (0, 0, 255)} <= pixels



def test_render_marks_only_peaks_and_session_only_selected_players(monkeypatch):
    from PIL import ImageDraw
    original = ImageDraw.ImageDraw.ellipse
    marks = []
    def record(draw, xy, *args, **kwargs):
        marks.append(kwargs['fill'])
        return original(draw, xy, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, 'ellipse', record)
    history = [(i, [v, 1000, 1000, 1000])
               for i, v in enumerate([1000, 1010, 1020, 1010, 1000])]
    render_graph(history, by_game=True)
    assert marks == [COLORS[0]]
    marks.clear()
    render_graph(history, by_game=True, players=[0, 1], all_points=True)
    assert marks == [COLORS[0]] * 5 + [COLORS[1]] * 5


def test_album_relay_and_transport(monkeypatch):
    png = render_graph([(0, [1000] * 4), (1, [1016, 984, 1000, 1000])], by_game=True)
    payload = {'chat_id': -123, 'media': [
        {'type': 'photo', 'photo_base64': base64.b64encode(png).decode()} for _ in range(3)]}
    calls = []
    def read(client, path, payload):
        body, content_type = request_body(payload)
        calls.append((path, body, content_type))
        return b'{"ok":true,"result":[{"message_id":1},{"message_id":2},{"message_id":3}]}'
    monkeypatch.setattr(Telegram, '_read', read)
    client = Telegram('test')
    assert relay_action({'telegram_action': 'sendMediaGroup', 'payload': payload}, client, -999)['code'] == 'forbidden_chat'
    assert not calls
    result = relay_action({'telegram_action': 'sendMediaGroup', 'payload': payload}, client, -123)
    assert result['ok'] and len(result['result']) == 3
    assert calls[0][0] == '/bottest/sendMediaGroup'
    assert calls[0][1].count(png) == 3
    assert calls[0][2].startswith('multipart/form-data')


@pytest.mark.parametrize('encoded', ['not base64!', base64.b64encode(b'not png').decode(),
                                    base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'x' * (2 * 1024 * 1024)).decode()])
def test_album_rejects_invalid_images(encoded):
    with pytest.raises(ValueError):
        request_body({'media': [{'type': 'photo', 'photo_base64': encoded}]})


def test_average_full_history_statistics_and_undo(tmp_path):
    from ttar.bot import statistics
    from ttar.elo_graph import elo_averages
    store = Store(tmp_path / 'average.db')
    store.db.execute("UPDATE settings SET value='true' WHERE key='configured'")
    for alias, name in ROSTER:
        store.add_player(alias, name, None, 9)
    assert elo_averages([], 4) == [0] * 4
    assert elo_averages([(0, [0] * 4)], 4) == [0] * 4
    assert elo_averages([(0, [0, 0]), (1, [10, -10]),
                         (2, [15, -15]), (3, [18, -18])], 2) == pytest.approx([43 / 3, -43 / 3])
    first = photo(store, 'first', at=86400)
    approve(store, first, 1)
    second = photo(store, 'second', at=172800, data=raw('Р', 'В'))
    approve(store, second, 1)
    assert elo_averages(game_history(store, ROSTER), 4) == [16, -16, 8, -8]
    text, _ = statistics(store, 1)
    assert 'учтено 1' in text
    assert 'Диагональ — баланс игрока:' in text
    assert 'старт 0' in text
    for name, rating, average in zip([name for _, name in ROSTER], [16, -16, 16, -16], [16, -16, 8, -8]):
        assert f'{name}: {rating} · средний Elo: {average:.0f}' in text
    store.db.execute('UPDATE games SET active=0 WHERE photo_id=?', (first,))
    store.recalculate()
    assert elo_averages(game_history(store, ROSTER), 4) == [0, 0, 16, -16]


def test_zero_baseline_upgrade_is_idempotent(tmp_path):
    path = tmp_path / 'upgrade.db'
    store = Store(path)
    store.db.execute("UPDATE settings SET value='true' WHERE key='configured'")
    approve(store, photo(store), 1)
    store.db.execute('UPDATE ratings SET rating=rating+1000')
    store.db.execute('UPDATE games SET rating_a_before=1000,rating_b_before=1000,rating_a_after=1016,rating_b_after=984')
    store.db.execute("DELETE FROM settings WHERE key='elo_zero_baseline'")
    store.db.close()
    for _ in range(2):
        store = Store(path)
        assert [r[0] for r in store.db.execute('SELECT rating FROM ratings ORDER BY player_id')] == [16, -16]
        game = store.db.execute('SELECT * FROM games').fetchone()
        assert [game[k] for k in ('rating_a_before', 'rating_b_before', 'rating_a_after', 'rating_b_after')] == [0, 0, 16, -16]
        assert store.db.execute('SELECT count(*) FROM games').fetchone()[0] == 1
        store.db.close()
