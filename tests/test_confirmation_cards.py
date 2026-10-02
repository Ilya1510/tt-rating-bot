import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from ttar.bot import Bot, card_update, draft_footer, draft_pages, queue_draft
from ttar.core import Store
from ttar.telegram import TelegramError, human_member
from ttar.worker import flush_outbox
from test_core import photo, store
from test_bot_webhook import FakeTelegram


def test_quorum_duplicate_and_unprivileged_group_member(store):
    pid = photo(store)
    assert not store.confirm(pid, 1, 42, True)
    assert not store.confirm(pid, 1, 42, True)
    assert store.vote_count(pid) == 1
    assert store.db.execute('SELECT count(*) FROM games').fetchone()[0] == 0
    assert store.confirm(pid, 1, 43, True)
    assert not store.confirm(pid, 1, 44, True)
    assert store.vote_count(pid) == 2
    assert store.db.execute('SELECT count(*) FROM games').fetchone()[0] == 1
    assert store.db.execute("SELECT count(*) FROM audit WHERE action='confirm_vote'").fetchone()[0] == 2


def test_new_revision_requires_two_fresh_votes(store):
    pid = photo(store)
    store.confirm(pid, 1, 42, True)
    with store.transaction():
        store.fix_draft(pid, [['М','И',12,10]], 42, False)
    assert store.vote_count(pid) == 0
    with pytest.raises(ValueError, match='изменился'):
        store.confirm(pid, 1, 43, True)
    assert not store.confirm(pid, 2, 43, True)
    assert store.confirm(pid, 2, 42, True)
    assert store.db.execute('SELECT score_a FROM games').fetchone()[0] == 12


def test_concurrent_votes_commit_games_once(store):
    pid = photo(store)
    def vote(actor):
        connection = Store(store.path)
        try:
            return connection.confirm(pid, 1, actor, True)
        finally:
            connection.db.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(vote, [42,42,43,43]))
    assert sum(results) == 1 and store.vote_count(pid) == 2
    assert store.db.execute('SELECT count(*) FROM games').fetchone()[0] == 1


@pytest.mark.parametrize('member', [
    {'status':'left'}, {'status':'kicked'}, {'status':'restricted','is_member':False},
    {'status':'member','user':{'is_bot':True}}
])
def test_only_human_current_members_can_vote(member):
    assert not human_member(member)
    assert human_member({'status':'member','user':{'is_bot':False}})
    assert human_member({'status':'restricted','is_member':True})


def test_card_count_updates_in_place_and_callback_does_not_spam(store):
    pid = photo(store)
    store.register_card(pid, 1, -123, 55, 'text')
    bot = Bot(store, FakeTelegram(), None, -123)
    for update_id, actor, expected in [(1,42,1),(2,42,1),(3,43,2)]:
        update={'update_id':update_id,'callback_query':{'id':str(update_id),'from':{'id':actor},
                'data':f'confirm:{pid}:1','message':{'message_id':55,'chat':{'id':-123},'text':'draft'}}}
        store.ingest(update)
        bot.handle(store.claim())
        assert store.vote_count(pid) == expected
        text, markup = draft_footer(store, pid)
        assert f'{expected}/2' in text
        if expected == 1:
            assert markup['inline_keyboard'][0][0]['text'] == 'Подтвердить · 1/2'
        else:
            assert 'Учтено партий: 1.' in text
            assert all(not button['callback_data'].startswith('confirm:') for row in markup['inline_keyboard'] for button in row)
    assert store.db.execute("SELECT count(*) FROM outbox WHERE method='sendMessage'").fetchone()[0] == 0
    assert store.db.execute("SELECT count(*) FROM outbox WHERE method='updateDraftCard'").fetchone()[0] == 3


def test_plain_confirm_allows_second_member_and_refreshes_card(store):
    pid = photo(store)
    store.register_card(pid, 1, -123, 55, 'text')
    bot = Bot(store, FakeTelegram(), None, -123)
    for update_id, actor in [(1,42),(2,43)]:
        store.ingest({'update_id':update_id,'message':{'chat':{'id':-123},'from':{'id':actor},'text':'/confirm'}})
        bot.handle(store.claim())
    assert store.photo(pid)['status'] == 'confirmed'
    assert store.db.execute("SELECT count(*) FROM outbox WHERE method='updateDraftCard'").fetchone()[0] == 2


def test_outbox_retry_uses_current_count_and_registers_returned_message(store):
    pid=photo(store)
    queue_draft(store,pid,'text-list')
    class Fake:
        def __init__(self): self.calls=[]
        def call(self, method, **payload):
            self.calls.append((method,payload))
            return {'message_id':70} if method == 'sendMessage' else True
    tg=Fake()
    flush_outbox(store,tg)
    assert '_draft_id' not in tg.calls[0][1]
    card=dict(store.db.execute('SELECT * FROM photo_cards').fetchone())
    assert card['message_id'] == 70
    # This queued edit was created at 1/2; a delayed retry must show 2/2.
    store.confirm(pid,1,42,True)
    store.update_cards(pid,'first-vote')
    store.confirm(pid,1,43,True)
    flush_outbox(store,tg)
    method,payload=tg.calls[-1]
    assert method == 'editMessageText' and '2/2' in payload['text']
    assert 'Учтено партий: 1.' in payload['text']
    assert all(not b['callback_data'].startswith('confirm:') for row in payload['reply_markup']['inline_keyboard'] for b in row)


def test_old_card_never_confirms_an_unreviewed_revision(store):
    pid=photo(store)
    with store.transaction(): store.fix_draft(pid,[['М','И',12,10]],42,False)
    method,payload=card_update(store,{'photo_id':pid,'revision':1,'chat_id':-123,'message_id':55,'kind':'text'})
    assert method == 'editMessageText' and 'Список обновлён' in payload['text']
    assert all(not b['callback_data'].startswith('confirm:') for row in payload['reply_markup']['inline_keyboard'] for b in row)


def test_winner_underlined_and_all_200_games_preserved(store):
    with store.transaction():
        for initial,name in [('М','Максим'),('И','Илья'),('Р','Рома'),('В','Валя')]:
            store.add_player(initial,name,None,0)
        pid=photo(store)
        games=[['М','И',11,8],['М','И',8,11],['Р','В',11,8],['Р','В',8,11]] * 50
        store.fix_draft(pid,games,42,False)
        queue_draft(store,pid,'long')
    pages=[json.loads(r[0]) for r in store.db.execute('SELECT payload FROM outbox ORDER BY id')]
    text='\n'.join(page['text'] for page in pages)
    assert '1. <u>Максим</u> — Илья 11:8' in text
    assert '2. Максим — <u>Илья</u> 8:11' in text
    assert '3. <u>Рома</u> — Валя 11:8' in text
    assert '4. Рома — <u>Валя</u> 8:11' in text
    assert len(pages)>1 and text.count('<u>') == text.count('</u>') == 200
    for page in pages:
        assert page['parse_mode']=='HTML' and len(page['text'])<=3900
        assert page['text'].count('<u>')==page['text'].count('</u>')
    assert 'Подтвердили: 0/2' in pages[-1]['text']
    assert all('reply_markup' not in page for page in pages[:-1])
    assert pages[-1]['reply_markup']['inline_keyboard'][0][0]['text']=='Подтвердить · 0/2'


def test_identical_edit_is_successful_without_retry(store):
    store.send('edit','editMessageText',{'chat_id':-123,'message_id':55,'text':'same'})
    class Fake:
        def call(self,*args,**kwargs): raise TelegramError(400,not_modified=True)
    assert flush_outbox(store,Fake())
    assert store.db.execute('SELECT status FROM outbox').fetchone()[0] == 'sent'


def test_counter_changes_never_move_games_between_messages(store):
    pid=photo(store)
    for count in range(100,201):
        proposal=[[store.player('М'),store.player('И'),11,8]] * count
        store.db.execute('UPDATE photos SET proposal=?,status=? WHERE id=?',(json.dumps(proposal),'draft',pid))
        before,_=draft_pages(store,pid)
        store.db.execute("UPDATE photos SET status='confirmed' WHERE id=?",(pid,))
        after,_=draft_pages(store,pid)
        assert len(before)==len(after)
        assert [page.split('\n\n')[0] for page in before]==[page.split('\n\n')[0] for page in after]
