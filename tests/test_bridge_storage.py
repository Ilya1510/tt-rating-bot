import sqlite3
import pytest
from bridge import storage


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setenv('BRIDGE_DATABASE', str(tmp_path/'bridge.sqlite3'))


def test_atomic_batch_deduplicates_across_restarts():
    events = [{'update_id': 10}, {'update_id': 11}]
    storage.persist_batch('tg', events, 12)
    storage.persist_batch('tg', events, 12)
    assert storage.get_state('tg_cursor') == 12
    with storage.database() as db:
        assert db.execute('SELECT count(*) FROM inbox').fetchone()[0] == 2


def test_failed_batch_cannot_advance_cursor_or_leave_partial_messages():
    storage.persist_batch('tg', [{'update_id': 1}], 2)
    with pytest.raises(KeyError):
        storage.persist_batch('tg', [{'update_id': 2}, {}], 4)
    assert storage.get_state('tg_cursor') == 2
    with storage.database() as db:
        assert db.execute('SELECT count(*) FROM inbox').fetchone()[0] == 1


def test_vk_events_and_reply_mapping_survive_reopen():
    storage.persist_batch('vk', [{'event_id': 'one', 'type': 'message_new'}], '123')
    storage.save_mapping({'tg_to_vk': {'5': '7'}, 'vk_to_tg': {'7': 5}, 'processed_updates': [8]})
    assert storage.load_mapping()['vk_to_tg']['7'] == 5
    job = storage.next_job()
    storage.retry(job['id'])
    assert storage.next_job() is None
    storage.finish(job['id'])
    storage.persist_batch('vk', [{'event_id': 'one', 'type': 'message_new'}], '124')
    assert storage.next_job() is None
    assert storage.get_state('vk_cursor') == '124'
