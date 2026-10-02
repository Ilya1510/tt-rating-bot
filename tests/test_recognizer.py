import json
import subprocess
from pathlib import Path

import pytest

from ttar.recognizer import recognize
from test_core import raw


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
