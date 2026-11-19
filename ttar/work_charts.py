"""Typed chart requests: calculate from the trusted snapshot, render and queue PNG."""
import base64
import json
import math
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont
from jsonschema import validate

CHART_SCHEMA = {'type': 'object', 'additionalProperties': False,
    'properties': {
        'metric': {'type': 'string', 'enum': ['elo', 'mean_elo', 'daily_mean_elo', 'wins', 'win_rate']},
        'period': {'type': 'string', 'enum': ['all', 'today', 'last_photo']},
        'x_axis': {'type': 'string', 'enum': ['date', 'game']},
        'player_ids': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1},
                       'maxItems': 4, 'uniqueItems': True}},
    'required': ['metric', 'period', 'x_axis', 'player_ids']}

TITLES = {'elo': 'Рейтинг Elo', 'mean_elo': 'Накопленное среднее Elo каждого игрока',
          'daily_mean_elo': 'Среднее Elo каждого игрока за игровой день',
          'wins': 'Накопленное количество побед', 'win_rate': 'Доля побед в личных партиях'}
NOTES = {'elo': 'Рейтинг после партии; начальный рейтинг — 0.',
         'mean_elo': 'Среднее после всех общих партий с начала истории, включая партии без участия игрока. Начальная точка 0 не включена.',
         'daily_mean_elo': 'Для каждого дня — среднее рейтингов после личных партий игрока в этот день. Дни без личных игр пропущены.',
         'wins': 'Победы накоплены с начала истории.',
         'win_rate': 'Процент побед среди личных партий с начала истории.'}
COLORS = {'м': '#e63946', 'и': '#16834b', 'р': '#222222', 'в': '#2864dc'}


def chart_data(snapshot, spec):
    validate(spec, CHART_SCHEMA)
    known = {p['id']: p for p in snapshot['players']}
    ids = spec['player_ids'] or list(known)
    if not ids or len(ids) > 4 or any(pid not in known for pid in ids):
        raise ValueError('Не нашёл выбранных игроков в нашей истории.')
    games = snapshot['games']
    if not games:
        raise ValueError('Пока нет подтверждённых партий для графика.')
    metric, period = spec['metric'], spec['period']
    tz = ZoneInfo('Europe/Moscow')
    today = datetime.fromisoformat(snapshot['as_of']).astimezone(tz).date()
    latest = games[-1]['photo_id']
    ratings = {pid: 0. for pid in known}
    totals = {pid: 0. for pid in known}
    wins = {pid: 0 for pid in known}
    played = {pid: 0 for pid in known}
    daily = {}
    points = []
    for number, game in enumerate(games, 1):
        day = datetime.fromtimestamp(game['occurred_at'], tz).date()
        a, b = game['a'], game['b']
        ratings[a], ratings[b] = game['rating_a_after'], game['rating_b_after']
        played[a] += 1; played[b] += 1
        wins[a if game['score_a'] > game['score_b'] else b] += 1
        for pid in known:
            totals[pid] += ratings[pid]
        selected = period == 'all' or (period == 'today' and day == today) or (
            period == 'last_photo' and game['photo_id'] == latest)
        if not selected:
            continue
        if metric == 'daily_mean_elo':
            bucket = daily.setdefault(day, {})
            for pid in (a, b):
                total, count = bucket.get(pid, (0., 0))
                bucket[pid] = (total + ratings[pid], count + 1)
            continue
        values = {pid: ratings[pid] if metric == 'elo' else totals[pid] / number if metric == 'mean_elo'
                  else wins[pid] if metric == 'wins'
                  else 100 * wins[pid] / played[pid] if played[pid] else None for pid in ids}
        points.append((day if spec['x_axis'] == 'date' else number, values))
    if metric == 'daily_mean_elo':
        points = [(day, {pid: values[pid][0] / values[pid][1] if pid in values else None for pid in ids})
                  for day, values in daily.items()]
    elif spec['x_axis'] == 'date':
        points = list(dict(points).items())  # End-of-day cumulative values.
    if not points or not any(v is not None for _, values in points for v in values.values()):
        raise ValueError('За выбранный период нет подтверждённых партий для этого графика.')
    if any(v is not None and not math.isfinite(v) for _, values in points for v in values.values()):
        raise ValueError('В рейтинговой истории некорректные значения.')
    players = [known[pid] for pid in ids]
    caption = TITLES[metric] + '.\n' + NOTES[metric]
    if spec['x_axis'] == 'date' and metric != 'daily_mean_elo':
        caption += '\nТочки — на конец игрового дня.'
    if period != 'all':
        caption += '\nПоказан период: ' + ('сегодня по Москве.' if period == 'today' else 'последнее подтверждённое фото.')
    return points, players, caption


def font(size):
    for path in ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                 '/System/Library/Fonts/Supplemental/Arial.ttf'):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def render_chart(points, players, spec):
    image = Image.new('RGB', (1400, 840), '#ffffff')
    draw = ImageDraw.Draw(image)
    title_font, label_font, small = font(30), font(22), font(18)
    draw.text((90, 26), TITLES[spec['metric']], fill='#17212b', font=title_font)
    colors = {}
    for i, player in enumerate(players):
        color = next((COLORS[a.casefold()] for a in player.get('aliases', []) if a.casefold() in COLORS),
                     list(COLORS.values())[i])
        colors[player['id']] = color
        x = 95 + i * 320
        draw.line((x, 100, x + 35, 100), fill=color, width=5)
        draw.text((x + 46, 84), player['name'][:18], fill=color, font=label_font)
    values = [v for _, row in points for v in row.values() if v is not None]
    padding = max(5, (max(values) - min(values)) * .10)
    low, high = min(values) - padding, max(values) + padding
    first, last = points[0][0], points[-1][0]
    dates = spec['metric'] == 'daily_mean_elo' or spec['x_axis'] == 'date'
    span = max(1, (last - first).days if dates else last - first)
    def xy(x, value):
        offset = (x - first).days if dates else x - first
        return (100 + 1140 * offset / span, 700 - 540 * (value - low) / (high - low))
    for i in range(7):
        value = low + (high - low) * i / 6
        y = xy(first, value)[1]
        draw.line((100, y, 1240, y), fill='#e4e8ee', width=1)
        draw.text((15, y - 11), f'{value:.0f}', fill='#56616f', font=small)
    if low <= 0 <= high:
        y = xy(first, 0)[1]
        draw.line((100, y, 1240, y), fill='#a8b1bc', width=2)
    indices = sorted({round((len(points) - 1) * i / 5) for i in range(6)})
    for i in indices:
        xval = points[i][0]
        x = xy(xval, low)[0]
        label = xval.strftime('%d.%m.%y') if dates else str(xval)
        draw.text((x, 725), label, fill='#56616f', font=small, anchor='mt')
    for player in players:
        pid = player['id']
        data = [(x, row[pid]) for x, row in points if row[pid] is not None]
        plotted = [xy(x, v) for x, v in data]
        if len(plotted) > 1:
            draw.line(plotted, fill=colors[pid], width=4)
        for x, y in plotted:
            draw.ellipse((x-3, y-3, x+3, y+3), fill=colors[pid])
    axis = 'Игровая дата' if dates else 'Номер партии в общей истории'
    draw.text((700, 790), axis, fill='#56616f', font=label_font, anchor='mm')
    out = BytesIO(); image.save(out, format='PNG')
    return out.getvalue()


def complete_chart(store, op, result, snapshot, allowed_chat):
    spec = result['chart']
    points, players, caption = chart_data(snapshot, spec)
    png = render_chart(points, players, spec)
    with store.transaction():
        current = store.db.execute('SELECT * FROM operations WHERE id=?', (op['id'],)).fetchone()
        if current and current['status'] == 'answered':
            return
        if (not current or current['status'] != 'running' or current['kind'] != 'work'
                or current['chat_id'] != allowed_chat or current['actor'] <= 0):
            raise ValueError('Нет доступа к графику этой задачи.')
        store.send(f"operation-result:{op['id']}", 'sendPhoto', {
            'chat_id': allowed_chat, 'photo_base64': base64.b64encode(png).decode(),
            'caption': caption})
        store.db.execute('UPDATE operations SET status=?,finished_at=?,result=? WHERE id=?',
                         ('answered', time.time(), json.dumps({'status': 'answered', 'chart': spec,
                          'summary': caption, 'snapshot_as_of': snapshot['as_of']}, ensure_ascii=False), op['id']))
