import json

import pytest

from ttar.booking_policy import load_policy
from ttar.operations import schedule, MOSCOW
from ttar.core import Store
from datetime import datetime


@pytest.mark.parametrize('value', [True, 0, 151, '150', -1])
def test_policy_rejects_invalid_duration(tmp_path, value):
    path = tmp_path/'policy.json'
    path.write_text(json.dumps({'regular_minutes': value}))
    with pytest.raises(ValueError):
        load_policy(path)


def test_policy_refresh_changes_next_regular_slot_without_restarting(tmp_path):
    path = tmp_path/'policy.json'
    path.write_text('{"regular_minutes": 60}')
    assert load_policy(path)['regular_minutes'] == 60
    path.write_text('{"regular_minutes": 150}')
    store = Store(str(tmp_path/'history.sqlite3'))
    schedule(store, -123, datetime(2026, 10, 5, 19, 1, tzinfo=MOSCOW),
             load_policy(path)['regular_minutes'])
    request = json.loads(store.db.execute('SELECT request FROM operations').fetchone()[0])
    assert request == {'start': '2026-10-08T19:00:00+03:00', 'end': '2026-10-08T21:30:00+03:00'}
