import json
from pathlib import Path

import pytest

from ttar.booking_policy import load_policy
from ttar.operations import schedule, MOSCOW
from ttar.core import Store
from ttar.calendar_api import CalendarClient
from ttar.maintenance import claim, run_booking, check_pending
from test_calendar_api import API
from datetime import datetime


@pytest.mark.parametrize('decision,status', [('ACCEPTED', 'accepted'), ('DECLINED', 'rejected')])
def test_repository_policy_requests_one_full_regular_booking(tmp_path, decision, status):
    policy = load_policy(Path(__file__).resolve().parents[1] / 'booking-policy.json')
    assert policy['regular_minutes'] == 150
    store = Store(str(tmp_path / 'history.sqlite3'))
    try:
        tick = datetime(2026, 10, 5, 19, 1, tzinfo=MOSCOW)
        assert schedule(store, -123, tick, policy['regular_minutes'])
        api = API()
        api.decision = decision
        client = CalendarClient(transport=api)
        run_booking(store, client, claim(store))
        check_pending(store, client, -123)
        assert not schedule(store, -123, tick, policy['regular_minutes'])
        assert claim(store) is None
        posts = [call[2] for call in api.calls if call[:2] == ('POST', '/events')]
        assert len(posts) == 1
        assert posts[0]['start']['date_time'] == '2026-10-08T19:00:00'
        assert posts[0]['end']['date_time'] == '2026-10-08T21:30:00'
        rows = store.db.execute('SELECT status, start, end, next_check FROM bookings').fetchall()
        assert len(rows) == 1
        assert tuple(rows[0]) == (status, '2026-10-08T19:00:00+03:00',
                                  '2026-10-08T21:30:00+03:00', None)
        message = json.loads(store.db.execute('SELECT payload FROM outbox').fetchone()[0])['text']
        assert '19:00–21:30' in message
        if status == 'rejected':
            assert 'Не удалось забронировать' in message
    finally:
        store.db.close()


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
