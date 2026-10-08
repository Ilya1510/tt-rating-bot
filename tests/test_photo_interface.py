import json

import pytest

from ttar.bot import Bot, draft, matrix_values, parse_window, statistics, split_message
from ttar.core import Store, balance_reached, elo, valid_score
from ttar.recognizer import PARSER_VERSION
from ttar.webhook import accept_trigger
from test_core import raw, approve
from test_bot_webhook import FakeTelegram


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path/'history.sqlite3')
    with store.transaction():
        store.db.execute("UPDATE settings SET value='true' WHERE key='configured'")
        for initial, name in [('М','Максим'),('И','Илья'),('Р','Рома'),('В','Валя')]:
            store.add_player(initial, name, None, 0)
        store.db.execute("UPDATE settings SET value='true' WHERE key='roster_configured'")
    yield store
    store.db.close()


def add_games(store, scores):
    data = raw()
    pid, _ = store.put_photo(-123, 10, 'photo', 'hash', 100, 42, data)
    store.fix_draft(pid, scores, 42, False)
    approve(store,pid, 2,42,True)
    return pid


def test_diagonal_balance_and_winner_row_loser_column(store):
    with store.transaction():
        add_games(store, [['М','И',11,8], ['И','М',12,10], ['Р','В',21,19], ['В','Р',22,20]])
    matrix, ratings, count = matrix_values(store, 1000)
    assert count == 4
    assert [matrix[i][i] for i in range(4)] == [1, 0, 1, 0]
    assert matrix[0][1] == matrix[1][0] == matrix[2][3] == matrix[3][2] == 1
    assert ratings[:2] == list(elo(*elo(1000,1000,0),1))
    text, _ = statistics(store)
    assert 'Elo · вся история' in text and '<pre>' in text and 'учтено 4' in text


def test_window_is_last_games_of_group_and_elo_is_not_reset(store):
    with store.transaction():
        add_games(store, [['М','И',11,8], ['Р','В',11,7], ['И','М',13,11]])
    full, ratings, _ = matrix_values(store, 1000)
    recent, recent_ratings, count = matrix_values(store, 1)
    assert count == 1 and recent[1][0] == 1 and recent[0][1] == recent[2][3] == 0
    assert [recent[i][i] for i in range(4)] == [0,1,0,0]
    assert recent_ratings == ratings and full[0][1] == full[2][3] == 1
    with store.transaction():
        store.amend(3, 9, True)
    after, _, count = matrix_values(store, 1)
    assert count == 1 and after[2][3] == 1


@pytest.mark.parametrize('score,deuce', [((11,9),False),((12,10),True),((13,11),True),((21,10),False),((21,19),False),((22,20),True),((24,22),True)])
def test_balance_for_games_to_11_or_21(score, deuce):
    valid_score(*score)
    assert balance_reached(*score) == deuce


def test_four_columns_use_per_game_pair_and_ignore_totals(store):
    data = raw()
    block = data['blocks'][0]
    block.update(columns=['М','И','Р','В'], player_a='', player_b='')
    first = block['rows'][0]
    first.update(player_a='М',player_b='Р',color='red',score_a=11,score_b=5)
    second = dict(first,player_a='И',player_b='В',color='blue',score_a=8,score_b=11)
    subtotal = dict(first,score_a=3,score_b=1)  # even an OCR misclassification must be discarded
    block['rows'] = [first, second, subtotal]
    data['blocks'].append(raw(score=(21,10))['blocks'][0])
    with store.transaction():
        pid,_ = store.put_photo(-123,10,'four','hash-four',100,42,data)
        assert json.loads(store.photo(pid)['ambiguities']) == []
        proposal = json.loads(store.photo(pid)['proposal'])
        assert proposal == [[store.player('М'),store.player('Р'),11,5], [store.player('И'),store.player('В'),8,11], [store.player('М'),store.player('И'),21,10]]
        text, keyboard = draft(store, pid)
    assert '1. <u>Максим</u> +16 — Рома -16 11:5' in text and '2. Илья -16 — <u>Валя</u> +16 8:11' in text
    assert 'Требуется' not in text and 'Блок' not in text and '[game]' not in text and '3:1' not in text
    assert 'нажми «Подтвердить»' in text and keyboard['inline_keyboard'][0][0]['text'] == 'Подтвердить · 0/2'


def test_resending_old_draft_refreshes_recognition_without_duplicate_games(store):
    fake = FakeTelegram()
    with store.transaction():
        pid,_ = store.put_photo(-123,10,'same','old-hash',100,42,raw())
    calls = []
    def ocr(image):
        calls.append(image)
        return raw(score=(21,10))
    bot = Bot(store,fake,ocr,-123)
    message = {'chat':{'id':-123},'from':{'id':42},'message_id':20,'date':200,
               'photo':[{'file_id':'f','file_unique_id':'same','width':10,'height':10}]}
    for update_id in (1,2):
        store.ingest({'update_id':update_id,'message':message})
        bot.handle(store.claim())
    assert len(calls) == 1 and store.photo(pid)['revision'] == 2
    assert json.loads(store.photo(pid)['raw'])['_parser_version'] == PARSER_VERSION
    assert store.db.execute('SELECT count(*) FROM photos').fetchone()[0] == 1
    assert store.db.execute('SELECT count(*) FROM games').fetchone()[0] == 0
    with store.transaction(), pytest.raises(ValueError, match='изменился'):
        approve(store,pid,1,42,True)


def test_plain_stat_command_passes_ingress_and_uses_html(store):
    update = {'update_id':1,'message':{'chat':{'id':-123},'date':200,'from':{'id':42},'text':'посчитать стату 10'}}
    published=[]
    assert accept_trigger(update,-123,published.append,100)['body'] == 'Saved'
    store.ingest(update)
    Bot(store,FakeTelegram(),lambda _:raw(),-123).handle(store.claim())
    result=json.loads(store.db.execute("SELECT payload FROM outbox WHERE method='sendMessage'").fetchone()[0])
    assert result['parse_mode'] == 'HTML' and 'Последние 10 партий' in result['text']
    assert parse_window('') == 1000
    assert '/pair' not in Bot(store,FakeTelegram(),None,-123).command('/help','/help',42,False)[0]


def test_long_game_list_is_never_silently_truncated():
    text='\n'.join(f'{i}. Максим — Илья 11:9' for i in range(1,201)) + '\nНажми «Подтвердить».'
    chunks=split_message(text)
    assert '\n'.join(chunks) == text and all(len(part)<=3900 for part in chunks)
    assert chunks[-1].endswith('Нажми «Подтвердить».')


def test_photo_elo_changes_are_sequential_and_confirmation_uses_saved_values(store):
    with store.transaction():
        pid, _ = store.put_photo(-123, 10, 'elo', 'elo-hash', 100, 42, raw())
        store.fix_draft(pid, [['М', 'И', 11, 8], ['И', 'М', 11, 9]], 42, False)
        before, _ = draft(store, pid)
        assert '<u>Максим</u> +16 — Илья -16 11:8' in before
        assert '<u>Илья</u> +17 — Максим -17 11:9' in before
        for line in before.splitlines()[:2]:
            assert line.count('Максим') == line.count('Илья') == 1
            assert 'Elo' not in line
        assert 'Elo рассчитан предварительно' in before
        assert store.db.execute('SELECT count(*) FROM games').fetchone()[0] == 0
        approve(store, pid, 2, 42, True)
        after, _ = draft(store, pid)
    assert 'предварительно' not in after
    assert '<u>Максим</u> +16 — Илья -16 11:8' in after
    assert '<u>Илья</u> +17 — Максим -17 11:9' in after


def test_old_photo_elo_preview_ignores_later_games(store):
    with store.transaction():
        add_games(store, [['М', 'И', 11, 8]])
        pid, _ = store.put_photo(-123, 9, 'old-elo', 'old-elo-hash', 99, 42, raw())
        preview, _ = draft(store, pid)
        assert '<u>Максим</u> +16 — Илья -16 11:8' in preview
        approve(store, pid, 1, 42, True)
        confirmed, _ = draft(store, pid)
    assert '<u>Максим</u> +16 — Илья -16 11:8' in confirmed
