"""Daily Elo history and an in-memory PNG; no filesystem or network access."""
from datetime import datetime, timedelta
from io import BytesIO
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from .core import elo


def daily_history(store, roster):
    ids = []
    for alias, _ in roster:
        row = store.db.execute('SELECT player_id FROM aliases WHERE alias=?',
                               (alias.casefold(),)).fetchone()
        ids.append(row[0] if row else None)
    ratings, daily = {}, {}
    for game in store.db.execute(
            'SELECT g.* FROM games g JOIN photos p ON p.id=g.photo_id '
            'WHERE g.active=1 ORDER BY g.occurred_at,p.message_id,g.ordinal,g.id'):
        day = datetime.fromtimestamp(game['occurred_at'], ZoneInfo('Europe/Moscow')).date()
        if not daily:
            daily[day - timedelta(days=1)] = [1000.] * len(ids)
        a, b = game['a'], game['b']
        ratings[a], ratings[b] = elo(ratings.get(a, 1000.), ratings.get(b, 1000.),
                                   0 if game['score_a'] > game['score_b'] else 1,
                                   float(store.setting('k')))
        daily[day] = [ratings.get(pid, 1000.) for pid in ids]
    return list(daily.items())


def render_graph(history):
    image = Image.new('RGB', (1200, 720), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=22)
    small = ImageFont.load_default(size=17)
    colors = ['#2474c4', '#df6933', '#289454', '#964fc4']
    draw.text((90, 20), 'Elo history / daily ratings (Moscow)', fill='#222222', font=font)
    for i, (name, color) in enumerate(zip(('Maxim', 'Ilya', 'Roma', 'Valya'), colors)):
        x = 90 + i * 265
        draw.line((x, 70, x + 32, 70), fill=color, width=4)
        draw.text((x + 42, 57), name, fill=color, font=font)
    low = min(min(values) for _, values in history) - 20
    high = max(max(values) for _, values in history) + 20
    start, end = history[0][0], history[-1][0]
    days = max(1, (end - start).days)
    def point(day, rating):
        return (90 + (day - start).days / days * 1050,
                605 - (rating - low) / (high - low) * 475)
    for i in range(6):
        rating = low + (high - low) * i / 5
        y = point(start, rating)[1]
        draw.line((90, y, 1140, y), fill='#e0e0e0')
        draw.text((15, y - 10), f'{rating:.0f}', fill='#333333', font=small)
    for i in range(min(6, days + 1)):
        offset = round(days * i / min(5, days))
        day = start + timedelta(days=offset)
        x = point(day, low)[0]
        draw.text((x - 45, 622), day.isoformat(), fill='#333333', font=small)
    draw.text((15, 95), 'Elo', fill='#222222', font=font)
    draw.text((565, 675), 'Date', fill='#222222', font=font)
    for i, color in enumerate(colors):
        points = [point(day, values[i]) for day, values in history]
        draw.line(points, fill=color, width=3)
        for x, y in points:
            draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=color)
    out = BytesIO()
    image.save(out, format='PNG')
    return out.getvalue()
