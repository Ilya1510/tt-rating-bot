"""Explicit, consistent tennis-only snapshot; no credentials or raw chat text."""
from datetime import datetime, timezone
from .work_actions import photo_state


def tennis_snapshot(store, chat_id):
    with store.transaction():
        players = [dict(r) for r in store.db.execute('''
            SELECT p.id,p.name,COALESCE(r.rating,0.0) AS elo,
                   COALESCE(r.wins,0) AS wins,COALESCE(r.losses,0) AS losses
            FROM players p LEFT JOIN ratings r ON r.player_id=p.id ORDER BY p.id''')]
        for player in players:
            player['aliases'] = [r[0] for r in store.db.execute(
                'SELECT alias FROM aliases WHERE player_id=? ORDER BY alias', (player['id'],))]
        games = [dict(r) for r in store.db.execute('''
            SELECT g.id,g.photo_id,g.ordinal+1 AS number,g.occurred_at,g.a,g.b,g.score_a,g.score_b,
                   g.rating_a_before,g.rating_b_before,g.rating_a_after,g.rating_b_after
            FROM games g JOIN photos p ON p.id=g.photo_id
            WHERE g.active=1 AND p.chat_id=? AND p.status='confirmed'
            ORDER BY g.occurred_at,p.message_id,g.ordinal,g.id''', (chat_id,))]
        bookings = [dict(r) for r in store.db.execute(
            'SELECT start,end,status,url FROM bookings ORDER BY start DESC')]
        pending = store.db.execute(
            "SELECT count(*) FROM photos WHERE chat_id=? AND status='draft'", (chat_id,)).fetchone()[0]
        photos = [photo_state(store, p) for p in store.db.execute(
            'SELECT * FROM photos WHERE chat_id=? ORDER BY message_id,id', (chat_id,)).fetchall()]
        return {'as_of': datetime.now(timezone.utc).isoformat(), 'timezone': 'Europe/Moscow',
                'rating': {'initial': 0, 'k': float(store.setting('k')),
                           'scope': 'all confirmed games; no rolling window'},
                'stats_default_window': int(store.setting('stats_window')),
                'players': players, 'games': games, 'games_complete': True,
                'unconfirmed_photos': pending, 'photos': photos,
                'latest_photo_id': photos[-1]['id'] if photos else None, 'bookings': bookings}
