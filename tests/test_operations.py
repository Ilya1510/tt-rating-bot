import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from ttar.bot import Bot
from ttar.calendar_api import BookingResult
from ttar.core import Store
from ttar.maintenance import claim, run_booking, recover, check_pending
from ttar.operations import MOSCOW, OWNER_ID, booking_request, enqueue, schedule
from test_bot_webhook import FakeTelegram
from test_core import store


def now(value):
    return datetime.fromisoformat(value).replace(tzinfo=MOSCOW)


@pytest.mark.parametrize('command', ['/create_booking 2026-10-05', '/cancel_booking'])
def test_owner_commands_reject_every_other_group_member(store, command):
    for actor in (42, 9, 0):
        store.ingest({'update_id': actor + 100, 'message': {'chat': {'id': -123}, 'from': {'id': actor}, 'text': command}})
        Bot(store, FakeTelegram(), None, -123).handle(store.claim())
    assert store.db.execute('SELECT count(*) FROM operations').fetchone()[0] == 0
    assert all('только Илье' in json.loads(row[0])['text'] for row in store.db.execute('SELECT payload FROM outbox'))


def test_owner_work_is_durable_and_not_executed_in_bot(store):
    update = {'update_id': 1, 'message': {'chat': {'id': -123}, 'from': {'id': OWNER_ID},
                                       'text': '/work@tt_chatgpt_rating_bot\nПересчитай метрику'}}
    store.ingest(update)
    Bot(store, FakeTelegram(), None, -123).handle(store.claim())
    row = store.db.execute('SELECT * FROM operations').fetchone()
    assert row['kind'] == 'work' and row['status'] == 'pending'
    assert json.loads(row['request'])['text'] == 'Пересчитай метрику'
    assert store.db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0
    store.ingest(update)
    assert store.claim() is None


def test_group_member_can_enqueue_question_but_cannot_book(store):
    store.ingest({'update_id': 10, 'message': {'chat': {'id': -123}, 'from': {'id': 42},
                                            'text': '/work Сколько у меня побед?'}})
    Bot(store, FakeTelegram(), None, -123).handle(store.claim())
    row = store.db.execute('SELECT actor,kind,status FROM operations').fetchone()
    assert tuple(row) == (42, 'work', 'pending')
    assert store.db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0


@pytest.mark.parametrize('day,target', [('2026-10-02', '2026-10-05'), ('2026-10-05', '2026-10-08')])
def test_schedule_exact_window_moscow_and_restart_dedup(store, day, target):
    assert not schedule(store, -123, now(day + 'T19:00:59'))
    assert schedule(store, -123, now(day + 'T19:01:00'))
    assert not schedule(store, -123, now(day + 'T19:01:01'))
    assert not schedule(store, -123, now(day + 'T23:59:00'))
    row = store.db.execute('SELECT * FROM operations').fetchone()
    assert row['actor'] == 0 and row['dedupe'] == 'scheduled:' + target
    request = json.loads(row['request'])
    assert request == {'start': target + 'T19:00:00+03:00', 'end': target + 'T20:00:00+03:00'}


def test_schedule_does_not_create_past_friday_on_saturday(store):
    assert not schedule(store, -123, now('2026-10-03T21:00:00'))


def test_manual_slot_respects_room_limits_and_72_hours():
    value = booking_request('2026-10-05 19:00 90', now('2026-10-02T19:01:00'))
    assert value['end'] == '2026-10-05T20:30:00+03:00'
    with pytest.raises(ValueError, match='3 дня'):
        booking_request('2026-10-05 19:00', now('2026-10-02T19:00:00'))
    with pytest.raises(ValueError, match='90 минут'):
        booking_request('2026-10-05 19:00 120', now('2026-10-03T19:00:00'))
    with pytest.raises(ValueError, match='будущее'):
        booking_request('2026-10-01 19:00', now('2026-10-03T19:00:00'))


def operation(store, key='manual:1'):
    with store.transaction():
        enqueue(store, '/create_booking', '/create_booking 2026-10-05 19:00', OWNER_ID, -123,
                key, now('2026-10-03T19:00:00'))
    return claim(store)


def test_lost_create_response_never_creates_again_even_after_not_found(store):
    class Client:
        created = 0
        def create_booking(self, *args):
            self.created += 1
            return BookingResult('uncertain', reason='Ответ потерян')
        def reconcile_booking(self, *args):
            return BookingResult('not_found', reason='Не найдено')
    client = Client()
    run_booking(store, client, operation(store))
    run_booking(store, client, operation(store, 'manual:2'))
    run_booking(store, client, operation(store, 'manual:3'))
    assert client.created == 1
    assert store.db.execute('SELECT create_attempted FROM bookings').fetchone()[0] == 1


def test_id_committed_before_verification_and_manual_auto_no_duplicate(store):
    class Client:
        created = 0
        def create_booking(self, start, end, key, saved):
            self.created += 1
            saved('event123')
            assert store.db.execute('SELECT event_id FROM bookings').fetchone()[0] == 'event123'
            return BookingResult('accepted', 'event123', 'https://example.com/event123')
        def verify_booking(self, *args):
            return BookingResult('accepted', 'event123', 'https://example.com/event123')
    client = Client()
    run_booking(store, client, operation(store))
    run_booking(store, client, operation(store, 'scheduled:2026-10-05'))
    assert client.created == 1
    assert store.db.execute('SELECT count(*) FROM bookings').fetchone()[0] == 1


def test_cancel_confirmation_checks_cancellation_instead_of_reconfirming_booking(store):
    op = operation(store)
    class Client:
        cancelled = 0
        def create_booking(self, start, end, key, saved):
            saved('event123')
            return BookingResult('accepted', 'event123', 'https://example.com/event123')
        def cancel_booking(self, *args):
            self.cancelled += 1
            return BookingResult('pending' if self.cancelled == 1 else 'cancelled', 'event123')
        def verify_booking(self, *args):
            pytest.fail('Should check pending cancellation')
    client = Client()
    run_booking(store, client, op)
    with store.transaction():
        enqueue(store, '/cancel_booking', '/cancel_booking 2026-10-05', OWNER_ID, -123, 'cancel:1')
    run_booking(store, client, claim(store))
    store.db.execute('UPDATE bookings SET next_check=0')
    check_pending(store, client, -123)
    assert store.db.execute('SELECT status FROM bookings').fetchone()[0] == 'cancelled'


def test_restart_does_not_repeat_work_but_reconciles_bookings(store):
    operation(store)
    with store.transaction():
        enqueue(store, '/work', '/work изменить метрику', OWNER_ID, -123, 'work:1')
    claim(store, work=True)
    recover(store)
    assert dict(store.db.execute('SELECT kind,status FROM operations')) == {'book': 'pending', 'work': 'uncertain'}


class BookingTelegram:
    def __init__(self): self.calls = []
    def call(self, method, **payload):
        assert not any(key.startswith('_') for key in payload)
        self.calls.append((method, payload))
        return {'message_id': 501} if method == 'sendMessage' else True


class MissingResourceClient:
    def create_booking(self, start, end, key, saved):
        saved('event123')
        return self.verify_booking('event123', start, end)
    def verify_booking(self, *args):
        return BookingResult('unverifiable', 'event123', 'https://example.com/event123',
                             'Календарь не показывает участие ресурса зала.')


def drain(store, tg):
    from ttar.worker import flush_outbox
    while flush_outbox(store, tg): pass


def test_missing_resource_sends_one_short_notice_even_after_ten_checks(store):
    client, tg = MissingResourceClient(), BookingTelegram()
    run_booking(store, client, operation(store))
    drain(store, tg)
    for _ in range(10):
        store.db.execute('UPDATE bookings SET next_check=0')
        check_pending(store, client, -123)
        drain(store, tg)
    assert len(tg.calls) == 1
    assert tg.calls[0] == ('sendMessage', {'chat_id': -123,
        'text': 'Встреча создана: 05.10.2026 19:00–20:00 МСК.\nhttps://example.com/event123'})
    assert store.db.execute('SELECT next_check FROM bookings').fetchone()[0] is None


@pytest.mark.parametrize('initial_delivered', [True, False])
def test_late_refusal_edits_original_or_updates_unsent_notice(store, initial_delivered):
    client, tg = MissingResourceClient(), BookingTelegram()
    run_booking(store, client, operation(store))
    if initial_delivered: drain(store, tg)
    client.verify_booking = lambda *args: BookingResult('rejected', 'event123',
        'https://example.com/event123', 'Зал отклонил приглашение.')
    store.db.execute('UPDATE bookings SET next_check=0')
    check_pending(store, client, -123)
    drain(store, tg)
    assert [method for method, _ in tg.calls].count('sendMessage') == 1
    assert len(tg.calls) == (2 if initial_delivered else 1)
    assert tg.calls[-1][0] == ('editMessageText' if initial_delivered else 'sendMessage')
    assert 'Зал отклонил приглашение.' in tg.calls[-1][1]['text']
    if initial_delivered: assert tg.calls[-1][1]['message_id'] == 501


def test_acceptance_recheck_does_not_add_or_edit_same_notice(store):
    client, tg = MissingResourceClient(), BookingTelegram()
    run_booking(store, client, operation(store))
    drain(store, tg)
    client.verify_booking = lambda *args: BookingResult('accepted', 'event123',
        'https://example.com/event123', 'Зал принял приглашение; занятость подтверждена.')
    store.db.execute('UPDATE bookings SET next_check=0')
    check_pending(store, client, -123)
    drain(store, tg)
    assert len(tg.calls) == 1


def test_uncertain_creation_without_id_is_not_reported_as_created(store):
    from ttar.maintenance import booking_text
    run_booking(store, MissingResourceClient(), operation(store))
    row = store.db.execute('SELECT * FROM bookings').fetchone()
    text = booking_text(row, BookingResult('uncertain', reason='Ответ потерян'))
    assert 'Встреча создана' not in text
    assert 'Ответ потерян' in text


def test_pending_cancellation_updates_its_own_single_message(store):
    client, tg = MissingResourceClient(), BookingTelegram()
    run_booking(store, client, operation(store))
    drain(store, tg)
    with store.transaction():
        enqueue(store, '/cancel_booking', '/cancel_booking 2026-10-05', OWNER_ID, -123, 'cancel:single')
    client.cancel_booking = lambda *args: BookingResult('pending', 'event123')
    run_booking(store, client, claim(store));drain(store, tg)
    assert 'Отмена встречи' in tg.calls[-1][1]['text']
    client.cancel_booking = lambda *args: BookingResult('cancelled', 'event123', reason='Бронь отменена.')
    store.db.execute('UPDATE bookings SET next_check=0')
    check_pending(store, client, -123);drain(store, tg)
    assert [m for m, _ in tg.calls] == ['sendMessage', 'sendMessage', 'editMessageText']
    assert 'Бронь отменена' in tg.calls[-1][1]['text']


def test_result_changed_during_initial_send_is_edited_without_second_send(store):
    client = MissingResourceClient()
    run_booking(store, client, operation(store))
    class RacingTelegram(BookingTelegram):
        def call(self, method, **payload):
            result = super().call(method, **payload)
            if method == 'sendMessage':
                client.verify_booking = lambda *args: BookingResult('rejected', 'event123',
                    'https://example.com/event123', 'Зал отклонил приглашение.')
                store.db.execute('UPDATE bookings SET next_check=0')
                check_pending(store, client, -123)
            return result
    tg = RacingTelegram();drain(store, tg)
    assert [method for method, _ in tg.calls] == ['sendMessage', 'editMessageText']
    assert 'Зал отклонил приглашение.' in tg.calls[-1][1]['text']


def test_invitation_error_is_not_hidden_as_resource_metadata(store):
    from ttar.maintenance import booking_text
    run_booking(store, MissingResourceClient(), operation(store))
    row = store.db.execute('SELECT * FROM bookings').fetchone()
    reason = 'Встреча создана, но приглашения Роме и Максиму пока не подтверждены API.'
    assert reason in booking_text(row, BookingResult('unverifiable', 'event123', reason=reason))
