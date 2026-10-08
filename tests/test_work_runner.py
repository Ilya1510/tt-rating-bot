import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from ttar import work_runner as work


@pytest.mark.parametrize('name', ['ttar/core.py', 'tests/test_score.py', 'README.md', 'docs/work.md',
    'ttar/work_runner.py', 'ttar/maintenance.py', 'ttar/release.py', 'scripts/deploy.py',
    'requirements.txt', 'deploy/owner_apply.py', '.github/workflows/tests.yml'])
def test_allowlist_accepts_bot_tests_and_docs(name):
    assert work.allowed_path(name)


@pytest.mark.parametrize('name', ['../ttar/core.py', '/ttar/core.py', 'ttar/.env',
    '.env', 'auth.json', 'tests/a/../../deploy/install.py',
    '.git/config', 'runtime/private.json', 'credentials.json', 'docs/key.pem',
    'ttar/core.py\n', 'ttar\\core.py'])
def test_secret_and_internal_paths_cannot_be_published(name):
    assert not work.allowed_path(name)


def test_report_does_not_follow_symlink_or_accept_non_json(tmp_path):
    secret = tmp_path/'private'
    secret.write_text('{"status": "changed", "summary": "sensitive", "technical": "secret"}')
    report = tmp_path/'result'
    report.symlink_to(secret)
    with pytest.raises(work.WorkError):
        work.read_report(report)
    report.unlink()
    report.write_text('not JSON')
    with pytest.raises(work.WorkError):
        work.read_report(report)
    report.write_text(json.dumps({'status': 'changed', 'summary': 'ok', 'technical': 'tested'}))
    assert work.read_report(report)['summary'] == 'ok'


def test_unpack_rejects_traversal_and_links(tmp_path):
    for name, kind in [('../outside', tarfile.REGTYPE), ('link', tarfile.SYMTYPE)]:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w') as archive:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = '/etc/passwd'
            archive.addfile(info)
        with pytest.raises(work.WorkError):
            work.unpack_archive(buf.getvalue(), tmp_path)


def test_errors_and_reports_redact_known_secrets_and_patterns():
    text = 'secret-value sk-abcDEF_123 123456789:abcdefghijklmnopqrstuvwxyzABCD'
    assert work.redact(text, ['secret-value']) == '[secret] [secret] [secret]'
    with pytest.raises(work.WorkError):
        work.validate_no_secrets(text.encode(), ['secret-value'])
    work.validate_no_secrets(b'+ TOKEN = os.environ["TOKEN"]\n', [])


def test_command_discards_process_stderr(monkeypatch):
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 1, b'', b'private-secret'))
    with pytest.raises(work.WorkError) as error:
        work.command(['false'])
    assert 'private-secret' not in str(error.value)


def test_privilege_drop_can_find_runuser_without_inheriting_secrets(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv('PRIVATE_TEST_TOKEN', 'must-not-inherit')
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b'ok', b'')
    monkeypatch.setattr(subprocess, 'run', run)
    work.git(tmp_path, 'init', user='ttar-code')
    argv, kwargs = calls[0]
    assert argv[:4] == ['runuser', '-u', 'ttar-code', '--']
    assert '/usr/sbin' in kwargs['env']['PATH'].split(':')
    assert '/usr/bin' in kwargs['env']['PATH'].split(':')
    assert 'PRIVATE_TEST_TOKEN' not in kwargs['env']


def test_pipeline_keeps_uncertain_outcome_and_sanitizes_it(tmp_path, monkeypatch):
    def fail(request, job, config, progress, outcome):
        outcome.update(status='uncertain', commit='a'*40, technical='secret-value')
        raise RuntimeError('provider secret-value')
    monkeypatch.setattr(work, '_run_work', fail)
    monkeypatch.setattr(work, 'trusted_directory', lambda p: None)
    result = work.run_work('изменить', 1, {'release_root': str(tmp_path), 'redact_values': ['secret-value']}, lambda p: None)
    assert result['status'] == 'uncertain'
    assert result['commit'] == 'a'*40
    assert 'secret-value' not in json.dumps(result)


def test_existing_job_is_not_replayed(tmp_path, monkeypatch):
    root = tmp_path/'release'
    home = tmp_path/'code'
    (home/'jobs'/'15').mkdir(parents=True)
    (root/'repo').mkdir(parents=True)
    monkeypatch.setattr(work, 'trusted_directory', lambda p: None)
    result = work.run_work('change', 15, {'release_root': str(root), 'code_home': str(home)}, lambda p: None)
    assert result['status'] == 'failed'
    assert 'Автоповтор' in result['summary']


def init_repo(repo):
    repo.mkdir()
    work.git(repo, 'init')
    (repo/'ttar').mkdir()
    (repo/'ttar'/'core.py').write_text('VALUE = 1\n')
    work.git(repo, 'add', '.')
    work.git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@local', 'commit', '-m', 'base')
    return work.git(repo, 'rev-parse', 'HEAD').decode().strip()


def test_validate_applied_patch_rejects_symlinks_and_protected_files(tmp_path):
    repo = tmp_path/'repo'
    base = init_repo(repo)
    (repo/'ttar'/'core.py').unlink()
    (repo/'ttar'/'core.py').symlink_to('/etc/passwd')
    work.git(repo, 'add', '.')
    with pytest.raises(work.WorkError, match='Ссылки'):
        work.validate_changes(repo, base)
    work.git(repo, 'reset', '--hard', base)
    (repo/'ttar'/'work_runner.py').write_text('print("privilege escalation")\n')
    work.git(repo, 'add', '.')
    assert 'ttar/work_runner.py' in work.validate_changes(repo, base)


@pytest.mark.parametrize('failures,deploy_fails', [(3, False), (1, False), (2, False), (0, False), (0, True)])
def test_pipeline_tests_before_push_before_deploy(tmp_path, monkeypatch, failures, deploy_fails):
    release, home = tmp_path/'release', tmp_path/'code'
    release.mkdir()
    home.mkdir()
    repo = release/'repo'
    base = init_repo(repo)
    work.git(repo, 'update-ref', 'refs/remotes/origin/main', base)
    real_command, real_git = work.command, work.git
    events = []
    prompts = []

    def fake_command(argv, **kwargs):
        if argv[0] == 'runuser' and 'exec' in argv:
            workspace = Path(argv[argv.index('-C') + 1])
            if argv[argv.index('--sandbox') + 1] == 'workspace-write':
                prompts.append(kwargs['data'].decode())
                assert Path(argv[argv.index('-o') + 1]).read_text() == ''
                (workspace/'ttar'/'core.py').write_text(f'VALUE = {len(prompts) + 1}\n')
                if len(prompts) == 1:
                    (workspace/'ttar'/'first_attempt.py').write_text('OLD = True\n')
                elif (workspace/'ttar'/'first_attempt.py').exists():
                    (workspace/'ttar'/'first_attempt.py').unlink()
            Path(argv[argv.index('-o') + 1]).write_text(json.dumps(
                {'status': 'changed', 'summary': 'Изменено', 'technical': 'Проверено'}))
            return b''
        if argv[0] == 'systemd-run':
            events.append('test')
            assert 'PrivateNetwork=yes' in argv
            assert 'ProtectSystem=strict' in argv
            assert 'PYTEST_DISABLE_PLUGIN_AUTOLOAD=1' in argv
            candidate = Path(argv[argv.index('--working-directory') + 1])
            assert (candidate/'ttar'/'core.py').read_text() == f'VALUE = {len(prompts) + 1}\n'
            assert (candidate/'ttar'/'first_attempt.py').exists() == (len(prompts) == 1)
            if events.count('test') <= failures:
                raise work.CommandFailure('systemd-run', 1,
                    'FAILED tests/test_score.py::test_score - AssertionError\nprivate-secret', '')
            return b'1 passed'
        if argv[0] == '/trusted/deploy':
            events.append('deploy')
            assert argv[-1] == base
            if deploy_fails:
                raise work.WorkError('Связь с публикацией потеряна.')
            return b'{"status":"done","technical":"healthy"}'
        return real_command(argv, **kwargs)

    def fake_git(path, *args, **kwargs):
        kwargs.pop('user', None)
        if args[0] == 'fetch':
            return b''
        if args[0] == 'push':
            events.append('push')
            return b''
        return real_git(path, *args, **kwargs)

    monkeypatch.setattr(work, 'command', fake_command)
    monkeypatch.setattr(work, 'git', fake_git)
    monkeypatch.setattr(work, 'trusted_directory', lambda p: None)
    monkeypatch.setattr(work.pwd, 'getpwnam', lambda u: work.pwd.getpwuid(os.getuid()))
    monkeypatch.setattr(os, 'chown', lambda *a: None)
    result = work.run_work('Измени метрику', 30,
                          {'release_root': str(release), 'code_home': str(home),
                           'deploy_command': ['/trusted/deploy'], 'model': 'example-model', 'actor_id': work.OWNER_ID,
                           'redact_values': ['private-secret']}, lambda s: None)
    exhausted = failures >= work.MAX_TEST_ATTEMPTS
    attempts = min(failures + 1, work.MAX_TEST_ATTEMPTS)
    assert events == ['test'] * attempts + ([] if exhausted else ['push', 'deploy'])
    assert len(prompts) == attempts
    for prompt in prompts[1:]:
        assert 'FAILED tests/test_score.py::test_score' in prompt
        assert 'private-secret' not in prompt
        assert 'Измени метрику' in prompt
    assert result['status'] == ('failed' if exhausted else 'uncertain' if deploy_fails else 'done')
    if exhausted:
        assert 'после 3 попыток' in result['summary'] and 'test_score' in result['summary']
    else:
        assert result['commit']
        assert (release/'work-30-deploy.json').exists()


@pytest.mark.parametrize('code,stdout', [(203, ''), (1, ''), (5, 'no tests ran'), (1, 'Unit start failed')])
def test_infrastructure_errors_are_not_treated_as_code_repair(code, stdout):
    with pytest.raises(work.WorkError, match='изолированном окружении'):
        work.test_feedback(work.CommandFailure('systemd-run', code, stdout, 'internal error'))


def test_feedback_retains_collection_errors_but_masks_secrets():
    error = work.CommandFailure('systemd-run', 2, 'ERROR tests/test_score.py - ImportError secret-value', '')
    feedback = work.test_feedback(error, ['secret-value'])
    assert 'ERROR tests/test_score.py' in feedback and 'secret-value' not in feedback


@pytest.mark.parametrize('decision,status', [('unchanged', 'answered'), ('changed', 'failed')])
def test_nonowner_never_enters_writable_stage_even_if_model_requests_change(tmp_path, monkeypatch, decision, status):
    release, home = tmp_path/'release', tmp_path/'code'
    release.mkdir(); home.mkdir()
    repo = release/'repo'
    base = init_repo(repo)
    work.git(repo, 'update-ref', 'refs/remotes/origin/main', base)
    real_command, real_git = work.command, work.git
    calls = []
    def fake_command(argv, **kwargs):
        if argv[0] == 'runuser' and 'exec' in argv:
            calls.append('answer')
            assert argv[argv.index('--sandbox') + 1] == 'read-only'
            context = home/'contexts'/'31'/'tennis.json'
            assert json.loads(context.read_text()) == {'players': [{'name': 'Илья', 'elo': 950}]}
            assert context.stat().st_mode & 0o222 == 0
            assert not (home/'jobs'/'31'/'tennis.json').exists()
            Path(argv[argv.index('-o') + 1]).write_text(json.dumps(
                {'status': decision, 'summary': 'Илье нужны 2 победы подряд.', 'technical': 'computed'}))
            return b''
        assert argv[0] not in ('systemd-run', '/trusted/deploy')
        return real_command(argv, **kwargs)
    def fake_git(path, *args, **kwargs):
        kwargs.pop('user', None)
        assert args[0] != 'push'
        return b'' if args[0] == 'fetch' else real_git(path, *args, **kwargs)
    monkeypatch.setattr(work, 'command', fake_command)
    monkeypatch.setattr(work, 'git', fake_git)
    monkeypatch.setattr(work, 'trusted_directory', lambda p: None)
    monkeypatch.setattr(work.pwd, 'getpwnam', lambda u: work.pwd.getpwuid(os.getuid()))
    monkeypatch.setattr(os, 'chown', lambda *a: None)
    result = work.run_work('Сколько нужно побед?', 31,
        {'release_root': str(release), 'code_home': str(home), 'deploy_command': ['/trusted/deploy'],
         'actor_id': 42, 'tennis_context': {'players': [{'name': 'Илья', 'elo': 950}]}}, lambda _: None)
    assert calls == ['answer']
    assert result['status'] == status and result['commit'] is None
    if decision == 'changed':
        assert 'только Илья' in result['summary']
    assert not (release/'candidates'/'31').exists()
