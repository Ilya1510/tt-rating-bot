import json
import sqlite3

import pytest

from ttar.core import Store, elo, valid_score


def raw(a='М', b='И', score=(11, 8), multi=False):
    block = {'label': 'left', 'player_a': a, 'player_b': b, 'rows': [
        {'player_a': a, 'player_b': b, 'color': 'red', 'text': f'{score[0]}:{score[1]}', 'score_a': score[0], 'score_b': score[1], 'kind': 'game', 'notes': ''}],
        'columns': [a, b],
        'preliminary_total': '1:0', 'notes': ''}
    return {'blocks': [block, block] if multi else [block], 'order_known': True,
            'ambiguities': [], 'preliminary_summary': 'test fixture'}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path/'test.sqlite3')
    s.db.execute("UPDATE settings SET value='true' WHERE key='configured'")
    yield s
    s.db.close()


def photo(store, unique='one', at=100, data=None):
    return store.put_photo(-123, at, unique, unique + '-hash', at, 42, data or raw())[0]


def approve(store, pid, revision, actor=42, member=True):
    changed = store.confirm(pid, revision, actor, member)
    if store.photo(pid)['status'] == 'confirmed':
        return changed
    return store.confirm(pid, revision, 43 if actor != 43 else 44, True)


def test_equal_elo_and_strong_opponent():
    assert elo(1000, 1000, 0) == (1016, 984)
    assert elo(1000, 1400, 0)[0] > 1016
    a, b = elo(1000, 1400, 1)
    assert a + b == pytest.approx(2400)


def test_confirm_is_idempotent_and_scores_pair(store):
    with store.transaction():
        p = photo(store)
        assert approve(store,p, 1,42,True)
        assert not approve(store,p, 1,42,True)
    assert store.db.execute('SELECT count(*) FROM games').fetchone()[0] == 1
    ratings = store.db.execute('SELECT * FROM ratings ORDER BY player_id').fetchall()
    assert [(r['rating'], r['wins'], r['losses']) for r in ratings] == [(1016, 1, 0), (984, 0, 1)]


def test_photo_dedup_by_unique_and_hash(store):
    with store.transaction():
        p = photo(store)
        duplicate, is_new = store.put_photo(-123, 777, 'two', 'one-hash', 200, 42, raw())
        assert (duplicate, is_new) == (p, False)
        duplicate, is_new = store.put_photo(-123, 888, 'one', 'other-hash', 300, 42, raw())
        assert (duplicate, is_new) == (p, False)


def test_ambiguity_requires_manual_selection_and_stale_button_rejected(store):
    with store.transaction():
        data = raw(multi=True)
        data['ambiguities'] = ['Нечитаемый цвет пары']
        p = photo(store, data=data)
        with pytest.raises(ValueError, match='неоднозначности'):
            approve(store,p, 1,42,True)
        store.fix_draft(p, [['М', 'И', 11, 8]], 42, False)
        with pytest.raises(ValueError, match='изменился'):
            approve(store,p, 1,42,True)
        assert approve(store,p, 2,42,True)


def test_recalculate_after_old_game_undo_and_edit(store):
    with store.transaction():
        p1 = photo(store)
        approve(store,p1, 1,42,True)
        p2 = photo(store, 'two', 200, raw(score=(8, 11)))
        approve(store,p2, 1,42,True)
        before = store.db.execute('SELECT rating FROM ratings WHERE player_id=1').fetchone()[0]
        assert before != 984
        assert store.amend(1, 9, True)
        assert store.db.execute('SELECT rating FROM ratings WHERE player_id=1').fetchone()[0] == 984
        assert not store.amend(1, 9, True)
        store.amend(2, 9, True, ['М', 'И', 11, 8])
        assert store.db.execute('SELECT rating FROM ratings WHERE player_id=1').fetchone()[0] == 1016
        assert store.db.execute('SELECT count(*) FROM audit WHERE action IN (?,?)', ('undo', 'edit')).fetchone()[0] == 2


def test_unauthorized_changes_rejected(store):
    with store.transaction():
        p = photo(store)
        with pytest.raises(ValueError):
            store.confirm(p, 1, 99, False)
        approve(store,p, 1,42,True)
        with pytest.raises(ValueError, match='администраторам'):
            store.amend(1, 42, False)


def test_no_production_write_before_policy(tmp_path):
    s = Store(tmp_path/'unconfigured.sqlite3')
    with s.transaction():
        p = photo(s)
        with pytest.raises(ValueError, match='не настроена'):
            approve(s,p, 1,42,True)
    assert s.db.execute('SELECT count(*) FROM games').fetchone()[0] == 0


def test_restart_recovers_processing_and_dedups_update(store):
    update = {'update_id': 123, 'message': {'text': '/top'}}
    store.ingest(update)
    store.ingest(update)
    assert store.claim()['attempts'] == 1
    path = store.path
    second = Store(path)
    second.recover()
    assert second.claim()['id'] == 123
    assert second.db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
    second.retry(123, 'timeout')
    second.db.execute('UPDATE jobs SET ready_at=0')
    assert second.claim()['attempts'] == 3
    second.retry(123, 'timeout')
    assert second.db.execute('SELECT status FROM jobs').fetchone()[0] == 'failed'
    second.db.close()


def test_backup_wal_and_restore(store, tmp_path):
    with store.transaction():
        p = photo(store)
        approve(store,p, 1,42,True)
    target = tmp_path/'backup.sqlite3'
    store.backup(target)
    with sqlite3.connect(target) as restored:
        assert restored.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert restored.execute('SELECT count(*) FROM games').fetchone()[0] == 1


def test_late_confirmation_replays_chronologically(store):
    with store.transaction():
        later = photo(store, 'later', 200, raw(score=(8, 11)))
        approve(store,later, 1,42,True)
        earlier = photo(store, 'earlier', 100)
        approve(store,earlier, 1,42,True)
    after_first = elo(1000, 1000, 0)
    after_second = elo(*after_first, 1)
    assert store.db.execute('SELECT rating FROM ratings WHERE player_id=1').fetchone()[0] == after_second[0]


@pytest.mark.parametrize('score', [(14,6), (3,1), (11,10), (11,11), (13,10), (-1,11), (True,11)])
def test_invalid_game_score(score):
    with pytest.raises(ValueError):
        valid_score(*score)


def test_alias_ambiguity_never_guessed(store):
    with store.transaction():
        a = store.player('М', True)
        b = store.player('И', True)
        store.db.execute('INSERT INTO aliases VALUES (?,?)', ('м', b))
        with pytest.raises(ValueError, match='Неоднозначный'):
            store.player('М')


def test_same_second_games_order_by_telegram_message(store):
    with store.transaction():
        later, _ = store.put_photo(-123, 401, 'later', 'h-later', 100, 42, raw(score=(8, 11)))
        approve(store,later, 1,42,True)
        earlier, _ = store.put_photo(-123, 400, 'earlier', 'h-earlier', 100, 42, raw())
        approve(store,earlier, 1,42,True)
    assert store.db.execute('SELECT rating FROM ratings WHERE player_id=1').fetchone()[0] == elo(*elo(1000,1000,0),1)[0]


def test_alias_and_telegram_identity(store):
    with store.transaction():
        pid = store.add_player('М', 'Михаил', 123456, 9)
        store.add_alias('М', 'M', 9)
        assert store.player('M') == store.player('Михаил') == pid
        assert store.db.execute('SELECT telegram_id FROM players WHERE id=?', (pid,)).fetchone()[0] == 123456
        store.player('И', True)
        with pytest.raises(ValueError, match='занят'):
            store.add_alias('И', 'M', 9)


def test_configurable_match_model(tmp_path):
    s = Store(tmp_path/'match.sqlite3', unit='match', k=16)
    s.db.execute("UPDATE settings SET value='true' WHERE key='configured'")
    with s.transaction():
        p = photo(s, data=raw(score=(3, 1)))
        approve(s,p, 1,42,True)
    assert s.db.execute('SELECT rating FROM ratings WHERE player_id=1').fetchone()[0] == 1008
