"""Explicit, consistent tennis-only snapshot; no credentials or raw chat text."""
from datetime import datetime, timezone


def tennis_snapshot(store, chat_id):
    with store.transaction():
        players = [dict(r) for r in store.db.execute('''
            SELECT p.id,p.name,COALESCE(r.rating,1000.0) AS elo,
                   COALESCE(r.wins,0) AS wins,COALESCE(r.losses,0) AS losses
            FROM players p LEFT JOIN ratings r ON r.player_id=p.id ORDER BY p.id''')]
        for player in players:
            player['aliases'] = [r[0] for r in store.db.execute(
                'SELECT alias FROM aliases WHERE player_id=? ORDER BY alias', (player['id'],))]
        games = [dict(r) for r in store.db.execute('''
            SELECT g.id,g.occurred_at,g.a,g.b,g.score_a,g.score_b,
                   g.rating_a_before,g.rating_b_before,g.rating_a_after,g.rating_b_after
            FROM games g JOIN photos p ON p.id=g.photo_id
            WHERE g.active=1 AND p.chat_id=? AND p.status='confirmed'
            ORDER BY g.occurred_at,p.message_id,g.ordinal,g.id''', (chat_id,))]
        bookings = [dict(r) for r in store.db.execute(
            'SELECT start,end,status,url FROM bookings ORDER BY start DESC')]
        pending = store.db.execute(
            "SELECT count(*) FROM photos WHERE chat_id=? AND status!='confirmed'", (chat_id,)).fetchone()[0]
        return {'as_of': datetime.now(timezone.utc).isoformat(), 'timezone': 'Europe/Moscow',
                'rating': {'initial': 1000, 'k': float(store.setting('k')),
                           'scope': 'all confirmed games; no rolling window'},
                'stats_default_window': int(store.setting('stats_window')),
                'players': players, 'games': games, 'games_complete': True,
                'unconfirmed_photos': pending, 'bookings': bookings}
