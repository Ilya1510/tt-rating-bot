"""Daily and per-game Elo history and an in-memory PNG; no filesystem or network access."""
from datetime import datetime, timedelta
from io import BytesIO
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from .core import elo


def _game_history(store, roster):
    ids = []
    for alias, _ in roster:
        row = store.db.execute('SELECT player_id FROM aliases WHERE alias=?',
                               (alias.casefold(),)).fetchone()
        ids.append(row[0] if row else None)
    ratings, history = {}, []
    for game in store.db.execute(
            'SELECT g.* FROM games g JOIN photos p ON p.id=g.photo_id '
            'WHERE g.active=1 ORDER BY g.occurred_at,p.message_id,g.ordinal,g.id'):
        day = datetime.fromtimestamp(game['occurred_at'], ZoneInfo('Europe/Moscow')).date()
        if not history:
            history.append((day - timedelta(days=1), [1000.] * len(ids)))
        a, b = game['a'], game['b']
        ratings[a], ratings[b] = elo(ratings.get(a, 1000.), ratings.get(b, 1000.),
                                   0 if game['score_a'] > game['score_b'] else 1,
                                   float(store.setting('k')))
        history.append((day, [ratings.get(pid, 1000.) for pid in ids]))
    return history


def daily_history(store, roster):
    return list(dict(_game_history(store, roster)).items())


def game_history(store, roster):
    """Point zero is the initial rating; each next point is one active game."""
    return [(number, values) for number, (_, values) in
            enumerate(_game_history(store, roster))]


COLORS = ('#ff0000', '#008000', '#000000', '#0000ff')


def smooth_history(history):
    """Sample block-end ratings, including zero and the final partial block."""
    games = len(history) - 1
    step = max(1, (games + 298) // 299)
    indices = [0, *range(step, len(history), step)]
    if indices[-1] != games:
        indices.append(games)
    return [history[i] for i in indices]


def peak_indices(history, player):
    # Only personal games count toward win/loss streaks and extrema windows.
    personal = [0] + [i for i in range(1, len(history))
                      if history[i][1][player] != history[i - 1][1][player]]
    values = [history[i][1][player] for i in personal]
    peaks = []
    for j in range(2, len(values) - 2):
        a, b, c, d, e = values[j - 2:j + 3]
        reversal = a < b < c > d > e or a > b > c < d < e
        strict = False
        if 5 <= j < len(values) - 5:
            neighbors = values[j - 5:j] + values[j + 1:j + 6]
            strict = c > max(neighbors) or c < min(neighbors)
        if reversal or strict:
            peaks.append(personal[j])
    return peaks


def session_history(store, roster, today=None):
    """Today in Moscow, or the latest active photo when today has no games."""
    today = today or datetime.now(ZoneInfo('Europe/Moscow')).date()
    games = store.db.execute(
        'SELECT g.* FROM games g JOIN photos p ON p.id=g.photo_id '
        'WHERE g.active=1 ORDER BY g.occurred_at,p.message_id,g.ordinal,g.id').fetchall()
    if not games:
        return [], [], False
    selected = [i for i, g in enumerate(games)
                if datetime.fromtimestamp(g['occurred_at'], ZoneInfo('Europe/Moscow')).date() == today]
    is_today = bool(selected)
    if not selected:
        selected = [i for i, g in enumerate(games) if g['photo_id'] == games[-1]['photo_id']]
    participants = {pid for i in selected for pid in (games[i]['a'], games[i]['b'])}
    players = []
    for i, (alias, _) in enumerate(roster):
        row = store.db.execute('SELECT player_id FROM aliases WHERE alias=?',
                               (alias.casefold(),)).fetchone()
        if row and row[0] in participants:
            players.append(i)
    history = game_history(store, roster)
    result = [(0, history[selected[0]][1])]
    result.extend((n, history[i + 1][1]) for n, i in enumerate(selected, 1))
    return result, players, is_today


def render_graph(history, *, by_game=False, players=None, all_points=False, title=None):
    image = Image.new('RGB', (1200, 720), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=22)
    small = ImageFont.load_default(size=17)
    colors = COLORS
    players = list(range(4)) if players is None else players
    title = title or ('Elo / all games' if by_game else 'Elo / by day')
    draw.text((90, 20), title, fill='#222222', font=font)
    for slot, i in enumerate(players):
        name, color = ('Maxim', 'Ilya', 'Roma', 'Valya')[i], colors[i]
        x = 90 + slot * 265
        draw.line((x, 70, x + 32, 70), fill=color, width=4)
        draw.text((x + 42, 57), name, fill=color, font=font)
    low = min(values[i] for _, values in history for i in players) - 20
    high = max(values[i] for _, values in history for i in players) + 20
    start, end = history[0][0], history[-1][0]
    days = max(1, end - start if by_game else (end - start).days)
    def point(day, rating):
        offset = day - start if by_game else (day - start).days
        return (90 + offset / days * 1050,
                605 - (rating - low) / (high - low) * 475)
    for i in range(6):
        rating = low + (high - low) * i / 5
        y = point(start, rating)[1]
        draw.line((90, y, 1140, y), fill='#e0e0e0')
        draw.text((15, y - 10), f'{rating:.0f}', fill='#333333', font=small)
    for i in range(min(6, days + 1)):
        offset = round(days * i / min(5, days))
        day = start + offset if by_game else start + timedelta(days=offset)
        x = point(day, low)[0]
        draw.text((x - 45, 622), str(day) if by_game else day.isoformat(), fill='#333333', font=small)
    draw.text((15, 95), 'Elo', fill='#222222', font=font)
    draw.text((565, 675), 'Game number' if by_game else 'Date', fill='#222222', font=font)
    line_history = smooth_history(history) if by_game and not all_points else history
    for i in players:
        color = colors[i]
        points = [point(day, values[i]) for day, values in line_history]
        if len(points) > 1:
            draw.line(points, fill=color, width=3)
        marked = (peak_indices(history, i) if by_game and not all_points
                  else range(len(history)))
        for index in marked:
            day, values = history[index]
            x, y = point(day, values[i])
            draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=color)
    out = BytesIO()
    image.save(out, format='PNG')
    return out.getvalue()
