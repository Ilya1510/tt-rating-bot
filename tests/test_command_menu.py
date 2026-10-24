import json

from ttar.bot import Bot
from ttar.worker import queue_command_menu, flush_outbox
from ttar.telegram import TelegramError
from test_core import store


def test_spreadsheet_command_from_group_has_one_reply_and_does_not_modify_history(store):
    bot = Bot(store, object(), None, -123, spreadsheet_id='history-sheet')
    store.ingest({'update_id': 101, 'message': {'chat': {'id': -123},
        'from': {'id': 77}, 'text': '/spreadsheet@TestBot'}})
    job = store.db.execute('SELECT * FROM jobs').fetchone()
    bot.handle(job)
    bot.handle(job)
    rows = store.db.execute("SELECT payload FROM outbox WHERE method='sendMessage'").fetchall()
    assert len(rows) == 1
    reply = json.loads(rows[0][0])
    assert reply['text'].endswith('https://docs.google.com/spreadsheets/d/history-sheet/edit')
    assert reply['chat_id'] == -123
    assert store.db.execute('SELECT count(*) FROM games').fetchone()[0] == 0
    assert bot.command('/spredsheet', '/spredsheet', 77, False)[0] == reply['text']


def test_menu_uses_durable_outbox_and_retries_without_duplicate_registration(store):
    queue_command_menu(store)
    queue_command_menu(store)
    rows = store.db.execute('SELECT * FROM outbox').fetchall()
    assert len(rows) == 1
    commands = {c['command'] for c in json.loads(rows[0]['payload'])['commands']}
    assert {'graph', 'spreadsheet', 'confirm', 'stat', 'work'} <= commands
    class Offline:
        def call(self, *args, **kwargs):
            raise TelegramError(503)
    flush_outbox(store, Offline())
    row = store.db.execute('SELECT * FROM outbox').fetchone()
    assert row['status'] == 'pending' and row['attempts'] == 1
