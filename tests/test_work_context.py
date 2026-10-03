import json

from ttar.work_context import tennis_snapshot
from tests.test_core import store, photo, approve


def test_snapshot_contains_confirmed_games_and_no_private_message_payloads(store):
    pid = photo(store)
    approve(store, pid, 1)
    photo(store, unique='unconfirmed', at=200)
    store.db.execute('UPDATE players SET telegram_id=987654321')
    store.db.execute("UPDATE photos SET raw=?", ('sensitive raw photo instructions',))
    result = tennis_snapshot(store, -123)
    assert len(result['games']) == 1
    assert result['unconfirmed_photos'] == 1
    assert result['games_complete'] is True
    assert result['rating']['k'] == 32
    assert sum(p['wins'] for p in result['players']) == 1
    assert '987654321' not in json.dumps(result)
    assert 'sensitive raw' not in json.dumps(result)
    assert not tennis_snapshot(store, -999)['games']
