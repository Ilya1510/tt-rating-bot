import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

from test_core import store
from ttar.codex_health import check_due, probe_work, Busy, INTERVAL
from ttar.recognizer import RecognitionError, health_probe

CONFIG = {'allowed_chat_id': -123}


def test_work_probe_locates_runuser_under_restricted_service_path(tmp_path, monkeypatch):
    def run(argv, **kwargs):
        assert argv[0] == '/usr/sbin/runuser'
        assert 'HOME=/var/lib/ttar-code' in argv
        assert 'CODEX_HOME=/var/lib/ttar-code/.codex' in argv
        assert 'PYTHONPATH=/opt/ttar-control' in argv
        assert kwargs['env'] == {'PATH': '/usr/bin:/bin'}
        return subprocess.CompletedProcess(argv, 0, b'ok\n', b'')
    monkeypatch.setattr(subprocess, 'run', run)
    probe_work({'work': {'release_root': str(tmp_path)}})


def notices(store):
    return [json.loads(r[0])['text'] for r in store.db.execute('SELECT payload FROM outbox')]


def test_real_probe_has_no_image_or_tools_and_validates_response(monkeypatch):
    def run(argv, **kwargs):
        assert '--image' not in argv
        assert 'features.shell_tool=false' in argv
        assert 'features.apps=false' in argv
        assert kwargs['timeout'] == 60
        Path(argv[argv.index('-o') + 1]).write_text('{"ok":true}')
        return subprocess.CompletedProcess(argv, 0, b'', b'')
    monkeypatch.setattr(subprocess, 'run', run)
    health_probe()


def test_six_hour_schedule_persists_and_success_is_silent(store):
    probe = Mock()
    for at in (100, 101, 100 + INTERVAL - 1):
        check_due(store, CONFIG, probes={'ocr': probe}, clock=lambda: at)
    assert probe.call_count == 1
    # A fresh checker/closure reads the due time from SQLite after a restart.
    fresh_probe = Mock()
    check_due(store, CONFIG, probes={'ocr': fresh_probe}, clock=lambda: 100 + INTERVAL)
    assert fresh_probe.call_count == 1
    assert notices(store) == []


def test_auth_notifies_once_until_recovered_and_accounts_are_independent(store):
    fail = Mock(side_effect=RecognitionError('auth'))
    good = Mock()
    for at in (100, 100 + INTERVAL):
        check_due(store, CONFIG, probes={'ocr': fail, 'work': good}, clock=lambda: at)
    assert len(notices(store)) == 1 and 'распознавания фото' in notices(store)[0]
    assert '/work' not in notices(store)[0]
    check_due(store, CONFIG, probes={'ocr': good}, clock=lambda: 100 + 2 * INTERVAL)
    assert len(notices(store)) == 1
    check_due(store, CONFIG, probes={'ocr': fail}, clock=lambda: 100 + 3 * INTERVAL)
    assert len(notices(store)) == 2


def test_transient_failure_is_rechecked_before_notifying(store):
    fail = Mock(side_effect=RecognitionError('timeout'))
    check_due(store, CONFIG, probes={'work': fail}, clock=lambda: 100)
    check_due(store, CONFIG, probes={'work': fail}, clock=lambda: 159)
    assert fail.call_count == 1 and notices(store) == []
    check_due(store, CONFIG, probes={'work': fail}, clock=lambda: 160)
    assert len(notices(store)) == 1 and 'авторизации не подтверждён' in notices(store)[0]
    check_due(store, CONFIG, probes={'work': fail}, clock=lambda: 160 + INTERVAL)
    assert len(notices(store)) == 1


def test_transient_recovery_and_busy_executor_do_not_alarm(store):
    check_due(store, CONFIG, probes={'work': Mock(side_effect=Busy())}, clock=lambda: 100)
    assert store.db.execute('SELECT count(*) FROM codex_health').fetchone()[0] == 0
    check_due(store, CONFIG, probes={'work': Mock(side_effect=OSError('private'))}, clock=lambda: 100)
    check_due(store, CONFIG, probes={'work': Mock()}, clock=lambda: 160)
    assert notices(store) == []
    assert store.db.execute('SELECT status FROM codex_health').fetchone()[0] == 'ok'


def test_different_failure_reason_is_reported_without_raw_details(store):
    check_due(store, CONFIG, probes={'work': Mock(side_effect=RecognitionError('auth'))}, clock=lambda: 100)
    check_due(store, CONFIG, probes={'work': Mock(side_effect=RuntimeError('secret'))}, clock=lambda: 100 + INTERVAL)
    assert len(notices(store)) == 2
    assert 'secret' not in ' '.join(notices(store))
