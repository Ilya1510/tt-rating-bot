import json
import subprocess
from pathlib import Path

import pytest

from ttar.recognizer import recognize, RecognitionError, remote_recognize
from test_core import raw, store


def fake_run(events):
    def run(command, **kwargs):
        Path(command[command.index('-o') + 1]).write_text(json.dumps(raw()))
        return subprocess.CompletedProcess(command, 0, b'\n'.join(json.dumps(event).encode() for event in events), b'')
    return run


def test_tool_use_is_rejected_even_with_valid_final_json(monkeypatch):
    monkeypatch.setattr(subprocess, 'run', fake_run([{'type': 'item.completed', 'item': {'type': 'command_execution'}}]))
    with pytest.raises(RuntimeError, match='unexpected tool'):
        recognize(b'test-fixture')


def test_child_does_not_inherit_cloud_or_telegram_credentials(monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-only-secret')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'test-only-cloud-secret')
    captured = fake_run([{'type': 'item.completed', 'item': {'type': 'agent_message'}}])
    def run(command, **kwargs):
        assert 'TELEGRAM_BOT_TOKEN' not in kwargs['env']
        assert 'AWS_SECRET_ACCESS_KEY' not in kwargs['env']
        return captured(command, **kwargs)
    monkeypatch.setattr(subprocess, 'run', run)
    assert recognize(b'test-fixture') == raw()


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_auth_failure_is_classified_without_forwarding_provider_details(monkeypatch, stream):
    def run(command, **kwargs):
        output = {'stdout': b'', 'stderr': b''}
        output[stream] = b'401 Unauthorized: refresh_token_expired private-secret'
        return subprocess.CompletedProcess(command, 1, **output)
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(RecognitionError) as error:
        recognize(b'test-fixture')
    assert error.value.code == 'auth'
    assert 'private-secret' not in str(error.value)


def test_auth_code_survives_socket_boundary(monkeypatch):
    import struct
    import ttar.recognizer as module
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, *args): pass
        def connect(self, *args): pass
        def sendall(self, *args): pass
    body = json.dumps({'error': 'private-provider-details', 'error_code': 'auth'}).encode()
    chunks = iter([struct.pack('!I', len(body)), body])
    monkeypatch.setattr(module.socket, 'socket', lambda *_: Socket())
    monkeypatch.setattr(module, 'read_exact', lambda *_: next(chunks))
    with pytest.raises(RecognitionError) as error:
        remote_recognize(b'photo')
    assert error.value.code == 'auth'
    assert 'private' not in str(error.value)


def test_auth_failure_reports_once_without_three_useless_retries(store):
    from ttar.worker import process_job
    store.ingest({'update_id': 100, 'message': {'chat': {'id': -123}, 'text': 'test'}})
    job = store.claim(photos=False)
    class Bot:
        def handle(self, job): raise RecognitionError('auth')
    process_job(store, Bot(), job, -123)
    process_job(store, Bot(), job, -123)
    assert store.db.execute('SELECT status FROM jobs WHERE id=100').fetchone()[0] == 'failed'
    rows = store.db.execute('SELECT payload FROM outbox').fetchall()
    assert len(rows) == 1
    text = json.loads(rows[0][0])['text']
    assert 'авторизация Codex' in text and 'распознавания фото' in text
    assert '3 попыток' not in text
