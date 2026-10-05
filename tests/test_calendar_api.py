from copy import deepcopy
from io import BytesIO
import urllib.error
import pytest
from ttar.calendar_api import CalendarClient, CalendarError, ROOM, PLAYERS, _marker

START = '2026-10-08T19:00:00+03:00'
END = '2026-10-08T20:00:00+03:00'
KEY = '2026-10-08T19:00'
ID = 'event-test-1'


def event():
    return {'event_id': ID, 'description': _marker(KEY), 'relation_type': 'ORGANIZER',
            'start': {'date_time': '2026-10-08T19:00:00', 'time_zone': 'Europe/Moscow'},
            'end': {'date_time': '2026-10-08T20:00:00', 'time_zone': 'Europe/Moscow'}}


class API:
    def __init__(self, existing=False):
        self.exists = existing
        self.data = event()
        self.decision = 'ACCEPTED'
        self.busy_id = ID
        self.calls = []
        self.persisted = False
        self.uncertain = False
        self.players = []
        self.invite_error = False

    def __call__(self, method, path, body, query):
        self.calls.append((method, path, body, query))
        if path == '/events' and method == 'GET':
            return {'items': [self.data] if self.exists else []}
        if path == '/events' and method == 'POST':
            self.exists = True
            self.data.update(body)
            if self.uncertain:
                raise CalendarError()
            return self.data
        if path.endswith('/participants'):
            if method == 'POST':
                self.players.extend(body['items'])
                if self.invite_error:
                    raise CalendarError()
                return {'items': body['items']}
            return {'items': self.players + ([] if self.decision is None else [{'email': ROOM, 'decision': self.decision}])}
        if path == '/free-busy/users/search':
            return {'items': [{'event_id': self.busy_id, 'start': self.data['start'], 'end': self.data['end']}] if self.exists else []}
        if path == '/events/' + ID:
            if method == 'DELETE':
                self.exists = False
                return {}
            if not self.exists:
                raise CalendarError(404)
            return deepcopy(self.data)
        raise AssertionError((method, path))


def test_create_persists_before_resource_verification():
    api = API()
    def persist(event_id):
        assert event_id == ID
        assert not any('/participants' in call[1] for call in api.calls)
        api.persisted = True
    result = CalendarClient(transport=api).create_booking(START, END, KEY, persist)
    assert result.status == 'accepted' and api.persisted
    body = next(c[2] for c in api.calls if c[:2] == ('POST', '/events'))
    assert body['participants'] == [{'email': ROOM, 'participation_type': 'ATTENDEE'}]
    assert body['description'] == ''
    assert {p['email'] for p in api.players} == set(PLAYERS)
    assert body['start']['date_time'] == '2026-10-08T19:00:00'


@pytest.mark.parametrize('decision,busy_id,expected', [(None, ID, 'unverifiable'),
    ('NEEDS_ACTION', ID, 'pending'), ('TENTATIVE', ID, 'pending'), ('DECLINED', ID, 'rejected'),
    ('ACCEPTED', 'other-event', 'unverifiable'), ('ACCEPTED', ID, 'accepted')])
def test_201_or_other_events_busy_time_never_imply_booking(decision, busy_id, expected):
    api = API(True)
    api.decision, api.busy_id = decision, busy_id
    assert CalendarClient(transport=api).verify_booking(ID, START, END).status == expected


def test_timeout_without_id_does_not_claim_same_slot_event_or_duplicate():
    api = API()
    api.uncertain = True
    client = CalendarClient(transport=api)
    result = client.create_booking(START, END, KEY, lambda _: pytest.fail('no id response'))
    assert result.status == 'uncertain'
    assert client.reconcile_booking(START, END, KEY).status == 'not_found'
    assert sum(c[:2] == ('POST', '/events') for c in api.calls) == 1


def test_existing_attempt_reuses_event_and_persists_id():
    api, saved = API(True), []
    assert CalendarClient(transport=api).create_booking(START, END, KEY, saved.append).status == 'accepted'
    assert saved == [ID]
    assert not any(c[:2] == ('POST', '/events') for c in api.calls)


def test_description_free_event_reconciles_and_cancels_by_persisted_binding():
    api = API(True)
    api.data['description'] = ''
    client = CalendarClient(transport=api, ownership_lookup=lambda eid, key: (eid, key) == (ID, KEY))
    assert client.reconcile_booking(START, END, KEY).status == 'accepted'
    assert client.cancel_booking(ID, KEY, START, END).status == 'cancelled'


@pytest.mark.parametrize('patch', [{'relation_type': 'ATTENDEE'}, {'repetition': {'freq': 'WEEKLY'}},
    {'start': {'date_time': '2026-10-08T18:00:00', 'time_zone': 'Europe/Moscow'}}])
def test_binding_does_not_bypass_organizer_slot_or_recurrence_guards(patch):
    api = API(True)
    api.data.update(patch, description='')
    with pytest.raises(ValueError):
        CalendarClient(transport=api, ownership_lookup=lambda *_: True).cancel_booking(ID, KEY, START, END)
    assert not any(c[0] == 'DELETE' for c in api.calls)


def test_wrong_binding_cannot_cancel_unmarked_event():
    api = API(True)
    api.data['description'] = ''
    with pytest.raises(ValueError):
        CalendarClient(transport=api, ownership_lookup=lambda eid, key: key == 'other').cancel_booking(ID, KEY, START, END)
    assert not any(c[0] == 'DELETE' for c in api.calls)


def test_invites_only_missing_players():
    api = API()
    api.players = [{'email': PLAYERS[0].upper()}]
    assert CalendarClient(transport=api).create_booking(START, END, KEY, lambda _: None).status == 'accepted'
    body = next(c[2] for c in api.calls if c[0] == 'POST' and c[1].endswith('/participants'))
    assert body['items'] == [{'email': PLAYERS[1], 'participation_type': 'ATTENDEE'}]


def test_lost_invitation_response_keeps_id_and_never_repeats_write():
    api, saved = API(), []
    api.invite_error = True
    client = CalendarClient(transport=api)
    result = client.create_booking(START, END, KEY, saved.append)
    assert result.status == 'unverifiable' and saved == [ID]
    assert 'приглашения' in result.reason
    client.verify_booking(ID, START, END)
    assert sum(c[0] == 'POST' and c[1].endswith('/participants') for c in api.calls) == 1


def test_conflict_does_not_create_or_alter_other_event():
    api = API(True)
    api.data['description'] = 'Another meeting'
    result = CalendarClient(transport=api).create_booking(START, END, KEY, lambda _: None)
    assert result.status == 'conflict'
    assert not any(c[:2] == ('POST', '/events') or c[0] == 'DELETE' for c in api.calls)


@pytest.mark.parametrize('patch', [{'description': 'Not ours'}, {'relation_type': 'ATTENDEE'},
    {'repetition': {'freq': 'WEEKLY'}}, {'start': {'date_time': '2026-10-08T18:00:00', 'time_zone': 'Europe/Moscow'}}])
def test_cancel_refuses_foreign_changed_or_recurring_event(patch):
    api = API(True)
    api.data.update(patch)
    with pytest.raises(ValueError):
        CalendarClient(transport=api).cancel_booking(ID, KEY, START, END)
    assert not any(c[0] == 'DELETE' for c in api.calls)


def test_cancel_checks_absence_and_is_idempotent():
    api = API(True)
    client = CalendarClient(transport=api)
    assert client.cancel_booking(ID, KEY, START, END).status == 'cancelled'
    assert client.cancel_booking(ID, KEY, START, END).status == 'cancelled'
    assert sum(c[0] == 'DELETE' for c in api.calls) == 1


def test_failed_persistence_escapes_before_verification():
    api = API()
    def fail(_):
        raise RuntimeError('disk full')
    with pytest.raises(RuntimeError, match='disk full'):
        CalendarClient(transport=api).create_booking(START, END, KEY, fail)
    assert not any('/participants' in c[1] for c in api.calls)


def test_broken_pagination_fails_closed():
    def pages(*_):
        return {'items': [], 'iteration_key': 'same'}
    with pytest.raises(CalendarError):
        CalendarClient(transport=pages).list_events(START, END)


def test_no_token_or_http_error_body_leaks(monkeypatch):
    token = 'secret-token-not-for-output'
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError('https://example', 403, token, {}, BytesIO(token.encode()))
    class Opener:
        open = staticmethod(fail)
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    with pytest.raises(CalendarError) as caught:
        CalendarClient(token).get_event(ID)
    assert token not in str(caught.value)


def test_timezone_offsets_are_converted_not_truncated():
    api = API()
    result = CalendarClient(transport=api).create_booking('2026-10-08T16:00:00Z', '2026-10-08T17:00:00Z', KEY, lambda _: None)
    assert result.status == 'accepted'


def test_invalid_duration_rejected_before_network():
    api = API()
    with pytest.raises(ValueError):
        CalendarClient(transport=api).create_booking(START, '2026-10-08T21:31:00+03:00', KEY, lambda _: None)
    assert not api.calls
