import json
import urllib.error
from unittest.mock import Mock

import pytest

from ttar import sheets_backup as backup
from test_core import store, photo, approve, raw

CONFIG = {'spreadsheet_id': 'sheet-test', 'sheet_id': 7}


def test_only_second_vote_exports_and_restart_skips_same_snapshot(store, tmp_path):
    client = Mock()
    state = tmp_path/'backup.json'
    pid = photo(store)
    assert not backup.sync_once(store.path, -123, CONFIG, client, state)
    store.confirm(pid, 1, 42, True)
    assert not backup.sync_once(store.path, -123, CONFIG, client, state)
    assert client.write.call_count == 0
    store.confirm(pid, 1, 43, True)
    assert backup.sync_once(store.path, -123, CONFIG, client, state, now=1000)
    data = client.write.call_args.args[1]
    assert len(data['rows']) == 1
    assert data['rows'][0][9:15] == [0, 16, 16, 0, -16, -16]
    # A fresh client has no in-memory checkpoint: persisted state is sufficient.
    assert not backup.sync_once(store.path, -123, CONFIG, Mock(), state)
    assert json.loads(state.read_text())['last_success'] == 1000


def test_failure_retains_checkpoint_and_retry_includes_new_confirmation(store, tmp_path):
    first = photo(store)
    approve(store, first, 1)
    state = tmp_path/'backup.json'
    client = Mock()
    backup.sync_once(store.path, -123, CONFIG, client, state)
    saved = state.read_bytes()
    second = photo(store, unique='second', at=200)
    approve(store, second, 1)
    client.write.side_effect = backup.BackupError('Google HTTP 503')
    with pytest.raises(backup.BackupError):
        backup.sync_once(store.path, -123, CONFIG, client, state)
    assert state.read_bytes() == saved
    third = photo(store, unique='third', at=300)
    approve(store, third, 1)
    client.write.side_effect = None
    assert backup.sync_once(store.path, -123, CONFIG, client, state)
    assert len(client.write.call_args.args[1]['rows']) == 3


def test_late_game_reorders_history_and_corrections_refresh_ratings(store, tmp_path):
    second = photo(store, at=200)
    approve(store, second, 1)
    client, state = Mock(), tmp_path/'backup.json'
    backup.sync_once(store.path, -123, CONFIG, client, state)
    earlier = photo(store, unique='older', at=100, data=raw(score=(8, 11)))
    approve(store, earlier, 1)
    assert backup.sync_once(store.path, -123, CONFIG, client, state)
    data = client.write.call_args.args[1]
    assert [r[2] for r in data['rows']] == [earlier, second]
    assert data['rows'][1][9] == -16
    gid = data['rows'][0][0]
    with store.transaction():
        store.amend(gid, 42, True)
    assert backup.sync_once(store.path, -123, CONFIG, client, state)
    rows = client.write.call_args.args[1]['rows']
    assert rows[0][15] == 'Отменена' and rows[1][9] == 0


def test_rolled_back_confirmation_is_never_exported(store):
    pid = photo(store)
    store.confirm(pid, 1, 42, True)
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.confirm(pid, 1, 43, True)
            assert backup.snapshot(store.path, -123)['rows'] == []
            raise RuntimeError('rollback')
    assert backup.snapshot(store.path, -123)['rows'] == []


def test_lost_game_ids_do_not_erase_backup(store, tmp_path):
    pid = photo(store)
    approve(store, pid, 1)
    state, client = tmp_path/'state.json', Mock()
    backup.sync_once(store.path, -123, CONFIG, client, state)
    store.db.execute('DELETE FROM games')
    with pytest.raises(backup.BackupError, match='refusing'):
        backup.sync_once(store.path, -123, CONFIG, client, state)
    assert client.write.call_count == 1


def test_target_change_forces_first_upload(store, tmp_path):
    approve(store, photo(store), 1)
    state, client = tmp_path/'state.json', Mock()
    backup.sync_once(store.path, -123, CONFIG, client, state)
    assert backup.sync_once(store.path, -123, dict(CONFIG, spreadsheet_id='replacement'), client, state)
    assert client.write.call_count == 2


def test_atomic_payload_contains_typed_numbers_literal_names_and_no_append(store):
    approve(store, photo(store), 1)
    store.db.execute("UPDATE players SET name='=IMPORTXML(unsafe)' WHERE id=1")
    data = backup.snapshot(store.path, -123)
    reqs = backup.update_requests(7, data, 1000, 1000, 26)
    write = next(r['updateCells'] for r in reqs if 'updateCells' in r)
    assert write['range']['endRowIndex'] == 1000
    assert write['fields'] == 'userEnteredValue'
    assert write['rows'][4]['values'][4] == {'userEnteredValue': {'stringValue': '=IMPORTXML(unsafe)'}}
    assert write['rows'][4]['values'][10] == {'userEnteredValue': {'numberValue': 16}}
    assert not any('appendCells' in r or 'deleteRange' in r for r in reqs)


def test_missing_or_renamed_tab_refuses_write():
    client = backup.SheetsClient({'type': 'service_account'})
    client.call = Mock(return_value={'sheets': [{'properties': {'sheetId': 7, 'title': 'Other'}}]})
    with pytest.raises(backup.BackupError, match='missing or renamed'):
        client.write(CONFIG, {}, 1000)
    assert client.call.call_count == 1


def test_transport_error_does_not_expose_url_token_or_response(monkeypatch):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError('https://private/token', 403, 'private-secret', {}, None)
    monkeypatch.setattr(backup.urllib.request, 'urlopen', fail)
    with pytest.raises(backup.BackupError) as error:
        backup.request_json('https://sheets.googleapis.com')
    assert str(error.value) == 'Google HTTP 403'


def test_retry_loop_recovers_without_stopping_telegram(monkeypatch):
    calls = []
    class Stop:
        done = False
        def is_set(self):
            return self.done
        def wait(self, seconds):
            calls.append(seconds)
            if len(calls) == 2:
                self.done = True
    monkeypatch.setattr(backup.Path, 'read_text', lambda self: '{}')
    monkeypatch.setattr(backup, 'SheetsClient', lambda _: Mock())
    monkeypatch.setattr(backup, 'sync_once', Mock(side_effect=[backup.BackupError('Google HTTP 503'), True]))
    backup.run({'database': '/var/lib/ttar/history.sqlite3', 'allowed_chat_id': -123,
                'google_backup': CONFIG}, '/credential', Stop())
    assert calls == [20, 10]
