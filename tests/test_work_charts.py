import base64
import json
from io import BytesIO

import pytest
from PIL import Image
from jsonschema import ValidationError

from ttar.maintenance import claim
from ttar.operations import enqueue
from ttar.work_context import tennis_snapshot
from ttar.work_charts import chart_data, render_chart, complete_chart
from ttar.work_runner import read_report, WorkError
from ttar.telegram import request_body
from tests.test_core import store, photo, approve, raw


def spec(metric='mean_elo'):
    return dict(metric=metric, period='all', x_axis='date', player_ids=[])


def data(store):
    p = photo(store, at=100); approve(store, p, 1)
    q = photo(store, 'two', at=200, data=raw(score=(8, 11))); approve(store, q, 1)
    r = photo(store, 'three', at=86500); approve(store, r, 1)
    return tennis_snapshot(store, -123)


def test_cumulative_mean_uses_all_game_end_ratings_excluding_initial_point(store):
    ctx = data(store)
    request = spec(); request['x_axis'] = 'game'
    points, players, caption = chart_data(ctx, request)
    assert len(points) == 3 and points[0][0] == 1
    for n, values in points:
        for player in players:
            key = 'rating_a_after' if player['id'] == ctx['games'][0]['a'] else 'rating_b_after'
            assert values[player['id']] == pytest.approx(sum(g[key] for g in ctx['games'][:n])/n)
    daily, _, _ = chart_data(ctx, spec())
    assert len(daily) == 2 and daily[0][1] == points[1][1]
    assert 'общих партий' in caption


def test_daily_mean_only_counts_personal_games_and_does_not_invent_zeroes(store):
    ctx = data(store)
    ctx['players'].append({'id': 3, 'name': 'Рома', 'aliases': ['р']})
    ctx['games'][-1].update(a=1, b=3, rating_a_after=23., rating_b_after=-7.)
    points, _, caption = chart_data(ctx, spec('daily_mean_elo'))
    assert points[0][1][3] is None and points[1][1][2] is None
    assert points[1][1][3] == -7
    assert 'личных партий' in caption


def test_today_does_not_silently_fall_back_to_last_photo(store):
    ctx = data(store)
    request = spec(); request['period'] = 'today'
    with pytest.raises(ValueError, match='нет подтверждённых'):
        chart_data(ctx, request)
    request['period'] = 'last_photo'
    points, _, _ = chart_data(ctx, request)
    assert len(points) == 1
    expected = sum(g['rating_a_after'] for g in ctx['games']) / 3
    assert points[0][1][1] == pytest.approx(expected)


def test_filters_wins_and_win_rate(store):
    ctx = data(store)
    request = spec('wins'); request['player_ids'] = [2]
    points, players, _ = chart_data(ctx, request)
    assert [p['id'] for p in players] == [2]
    assert points[-1][1] == {2: 1}
    request['metric'] = 'win_rate'
    assert chart_data(ctx, request)[0][-1][1][2] == pytest.approx(100/3)
    request['player_ids'] = [999]
    with pytest.raises(ValueError, match='игроков'):
        chart_data(ctx, request)
    request['player_ids'] = [1, 1]
    with pytest.raises(ValueError, match='игроков'):
        chart_data(ctx, request)


def test_all_metrics_render_real_png_including_single_point(store):
    ctx = data(store)
    for metric in ['elo', 'mean_elo', 'daily_mean_elo', 'wins', 'win_rate']:
        request = spec(metric); request['period'] = 'last_photo'
        points, players, _ = chart_data(ctx, request)
        png = render_chart(points, players, request)
        with Image.open(BytesIO(png)) as image:
            assert image.format == 'PNG' and image.size == (1400, 840)


def test_nonowner_chart_is_sent_once_as_image_without_mutating_games(store):
    ctx = data(store)
    before = [tuple(g) for g in store.db.execute('select * from games')]
    enqueue(store, '/work', '/work нарисуй среднее эло', 42, -123, 'graph')
    op = claim(store, work=True)
    result = {'chart': spec()}
    complete_chart(store, op, result, ctx, -123)
    complete_chart(store, op, result, ctx, -123)
    rows = store.db.execute('select * from outbox').fetchall()
    assert len(rows) == 1 and rows[0]['method'] == 'sendPhoto'
    payload = json.loads(rows[0]['payload'])
    assert base64.b64decode(payload['photo_base64']).startswith(b'\x89PNG')
    body, content_type = request_body(payload)
    assert 'multipart/form-data' in content_type and b'image/png' in body
    assert [tuple(g) for g in store.db.execute('select * from games')] == before
    assert store.db.execute('select status from operations').fetchone()[0] == 'answered'


def test_invalid_specs_and_wrong_chat_never_send(store):
    ctx = data(store)
    enqueue(store, '/work', '/work график', 42, -123, 'graph')
    op = claim(store, work=True)
    with pytest.raises(ValueError, match='Нет доступа'):
        complete_chart(store, op, {'chart': spec()}, ctx, -999)
    bad = spec(); bad['image_path'] = '/etc/passwd'
    with pytest.raises(ValidationError):
        complete_chart(store, op, {'chart': bad}, ctx, -123)
    assert store.db.execute('select count(*) from outbox').fetchone()[0] == 0


def test_chart_report_must_not_also_request_mutation(tmp_path):
    path = tmp_path/'report.json'
    report = {'status': 'chart', 'summary': '', 'technical': '', 'action': None, 'chart': spec()}
    path.write_text(json.dumps(report))
    assert read_report(path, answer=True)['chart']['metric'] == 'mean_elo'
    report['action'] = {'kind': 'cancel_photo', 'photo_id': 1, 'game_number': None, 'score_a': None, 'score_b': None}
    path.write_text(json.dumps(report))
    with pytest.raises(WorkError): read_report(path, answer=True)
