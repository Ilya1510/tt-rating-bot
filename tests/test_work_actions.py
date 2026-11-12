import json
import pytest

from ttar.bot import card_update, game_lines
from ttar.maintenance import claim
from ttar.operations import OWNER_ID, enqueue
from ttar.sheets_backup import snapshot as sheet_snapshot
from ttar.work_actions import complete_data_action
from ttar.work_context import tennis_snapshot
from tests.test_core import store, photo, approve, raw


def operation(store, actor=OWNER_ID, key='one'):
    enqueue(store, '/work', '/work Исправь результат', actor, -123, key)
    return claim(store, work=True)


def result(pid, kind='edit_game', number=1, sa=8, sb=11):
    return {'status': 'data_action', 'action': {
        'kind': kind, 'photo_id': pid, 'game_number': number, 'score_a': sa, 'score_b': sb}}


def apply(store, op, plan, context=None):
    return complete_data_action(store, op, plan, context or tennis_snapshot(store, -123), -123)


def test_correct_confirmed_replays_later_games_updates_proposal_and_is_idempotent(store):
    first = photo(store); approve(store, first, 1)
    second = photo(store, 'later', 200); approve(store, second, 1)
    store.register_card(first, 1, -123, 10, 'text:0')
    store.register_card(second, 1, -123, 20, 'text:0')
    before = store.db.execute('select rating_a_before from games where photo_id=?', (second,)).fetchone()[0]
    op = operation(store)
    assert apply(store, op, result(first))['status'] == 'done'
    assert json.loads(store.photo(first)['proposal'])[0][2:] == [8, 11]
    assert '8:11' in game_lines(store, first)[0]
    after = store.db.execute('select rating_a_before from games where photo_id=?', (second,)).fetchone()[0]
    assert before == 16 and after == -16
    assert store.db.execute('select count(*) from games').fetchone()[0] == 2
    assert store.db.execute("select count(*) from outbox where method='updateDraftCard'").fetchone()[0] == 2
    assert apply(store, op, result(first)) is None
    assert store.db.execute("select count(*) from audit where action='work_data_action'").fetchone()[0] == 1
    assert store.db.execute("select count(*) from outbox where method='sendMessage'").fetchone()[0] == 1


def test_edit_draft_resets_votes_and_reuses_card_with_new_revision(store):
    pid = photo(store)
    store.confirm(pid, 1, 42, True)
    store.register_card(pid, 1, -123, 10, 'text:0')
    assert apply(store, operation(store), result(pid))['status'] == 'done'
    assert store.photo(pid)['revision'] == 2 and store.vote_count(pid) == 0
    assert store.db.execute('select count(*) from games').fetchone()[0] == 0
    method, payload = card_update(store, store.db.execute('select * from photo_cards').fetchone())
    assert method == 'editMessageText' and '8:11' in payload['text']
    assert payload['reply_markup']['inline_keyboard'][0][0]['callback_data'] == f'confirm:{pid}:2'
    with pytest.raises(ValueError, match='изменился'):
        store.confirm(pid, 1, 43, True)
    assert not store.confirm(pid, 2, 42, True)
    assert store.confirm(pid, 2, 43, True)


@pytest.mark.parametrize('confirmed', [False, True])
def test_cancel_photo_retains_history_but_removes_its_contribution(store, confirmed):
    pid = photo(store)
    if confirmed: approve(store, pid, 1)
    store.register_card(pid, 1, -123, 10, 'text:0')
    later = photo(store, 'later', 200); approve(store, later, 1)
    exported_before = sheet_snapshot(store.path, -123)
    assert apply(store, operation(store), result(pid, 'cancel_photo', None, None, None))['status'] == 'done'
    assert store.photo(pid)['status'] == 'cancelled'
    assert store.db.execute('select count(*) from games where active=1').fetchone()[0] == 1
    assert store.db.execute('select rating from ratings where player_id=1').fetchone()[0] == 16
    exported_after = sheet_snapshot(store.path, -123)
    assert [r[0] for r in exported_before['rows']] == [r[0] for r in exported_after['rows']]
    if confirmed: assert exported_after['rows'][0][15] == 'Отменена'
    _, payload = card_update(store, store.db.execute('select * from photo_cards').fetchone())
    assert payload['text'] == 'Фотография отменена. Партии не учитываются.'
    assert all(not b['callback_data'].startswith('confirm:') for row in payload['reply_markup']['inline_keyboard'] for b in row)
    assert tennis_snapshot(store, -123)['unconfirmed_photos'] == 0
    with pytest.raises(ValueError): store.confirm(pid, 1, 42, True)


def test_nonowner_cannot_apply_model_action(store):
    pid = photo(store); approve(store, pid, 1)
    assert apply(store, operation(store, actor=42), result(pid))['status'] == 'failed'
    assert '11:8' in game_lines(store, pid)[0]


@pytest.mark.parametrize('change', ['score', 'confirmation', 'chat'])
def test_stale_or_foreign_photo_is_rejected(store, change):
    pid = photo(store)
    context = tennis_snapshot(store, -123)
    if change == 'score': store.fix_draft(pid, [['М', 'И', 11, 5]], OWNER_ID, True)
    if change == 'confirmation': approve(store, pid, 1)
    if change == 'chat': store.db.execute('update photos set chat_id=-999 where id=?', (pid,))
    before = store.photo(pid)['proposal']
    assert apply(store, operation(store), result(pid), context)['status'] == 'failed'
    assert store.photo(pid)['proposal'] == before


@pytest.mark.parametrize('bad', [result(1, sa=3, sb=1), result(1, number=99),
                              result(1, 'cancel_photo'), {'action': {'kind': 'sql', 'query': 'delete'}}])
def test_invalid_action_changes_nothing(store, bad):
    pid = photo(store); approve(store, pid, 1)
    assert apply(store, operation(store), bad)['status'] == 'failed'
    assert store.photo(pid)['status'] == 'confirmed'
    assert '11:8' in game_lines(store, pid)[0]


def test_recalculation_failure_rolls_back_mutation_and_card_updates(store, monkeypatch):
    pid = photo(store); approve(store, pid, 1)
    monkeypatch.setattr(store, 'recalculate', lambda: (_ for _ in ()).throw(ValueError('bad history')))
    assert apply(store, operation(store), result(pid))['status'] == 'failed'
    assert store.db.execute('select score_a,score_b from games').fetchone()[:] == (11, 8)
    assert json.loads(store.photo(pid)['proposal'])[0][2:] == [11, 8]


def test_snapshot_includes_draft_rows_and_photo_ids_without_raw(store):
    pid = photo(store, data=raw(a='Р', b='В', score=(8, 11)))
    context = tennis_snapshot(store, -123)
    assert context['games'] == [] and context['latest_photo_id'] == pid
    assert context['photos'][0]['games'][0]['score_a'] == 8
    assert context['photos'][0]['games'][0]['number'] == 1
    assert context['rating']['initial'] == 0
    assert 'raw' not in context['photos'][0]
