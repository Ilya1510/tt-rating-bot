"""Fixed root deployment helper; install only in /opt/ttar-control.

Build the cloud candidate without moving the live tag, verify it, then promote.
Secrets stay in systemd credentials and never enter the generated workspace.
"""
import base64
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from .cloud_telegram import CloudTelegram
from .booking_policy import load_policy

BASE = 'https://serverless-functions.api.cloud.yandex.net/functions/v1'


class ReleaseError(Exception):
    pass


class Cloud:
    def __init__(self, key):
        self.auth = CloudTelegram('', key)

    def request(self, url, data=None):
        req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
            headers={'Authorization': 'Bearer ' + self.auth._iam(), 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise ReleaseError('Cloud HTTP ' + str(error.code)) from None
        except Exception:
            raise ReleaseError('Cloud connection failed') from None

    def wait(self, operation):
        until = time.monotonic() + 420
        while not operation.get('done'):
            if time.monotonic() >= until:
                raise ReleaseError('Cloud build still running; operation not cancelled')
            time.sleep(3)
            operation = self.request('https://operation.api.cloud.yandex.net/operations/' + operation['id'])
        if operation.get('error'):
            raise ReleaseError('Cloud operation failed')
        return operation.get('response', {})

    def live(self, function_id):
        return self.request(BASE + '/versions:byTag?' + urllib.parse.urlencode({'functionId': function_id, 'tag': 'live'}))

    def tag(self, version_id):
        return self.wait(self.request(BASE + '/versions/' + version_id + ':setTag', {'tag': 'live'}))


def cloud_package(root):
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.write(root/'ttar/webhook.py', 'index.py')
        archive.write(root/'ttar/telegram.py', 'ttar/telegram.py')
        archive.write(root/'ttar/__init__.py', 'ttar/__init__.py')
        archive.writestr('requirements.txt', 'boto3==1.43.107\n')
    return base64.b64encode(result.getvalue()).decode()


def create_body(root, old, tag):
    names = ('functionId', 'runtime', 'entrypoint', 'resources', 'executionTimeout',
             'serviceAccountId', 'environment', 'secrets', 'concurrency')
    body = {key: old[key] for key in names if key in old}
    body.update(content=cloud_package(root), tag=[tag], logOptions={'disabled': True})
    return body


def run(argv, cwd=None):
    result = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env={'PATH': '/usr/bin:/bin', 'HOME': '/root', 'PYTHONDONTWRITEBYTECODE': '1'})
    if result.returncode:
        raise ReleaseError('Local deployment command failed')
    return result.stdout


def install_tree(source, target):
    # Both runtime and controller releases are installed by the prior release helper.
    staged = target/'ttar.next'
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(source/'ttar', staged, ignore=shutil.ignore_patterns('__pycache__'))
    for path in staged.rglob('*'):
        if path.is_symlink():
            raise ReleaseError('Symlink in candidate')
        path.chmod(0o755 if path.is_dir() else 0o644)
    backup = target/'ttar.previous'
    if backup.exists():
        shutil.rmtree(backup)
    (target/'ttar').rename(backup)
    staged.rename(target/'ttar')


def install_project_files(source, target):
    for name in ('scripts', 'deploy', 'docs', 'tests'):
        if (source/name).exists():
            shutil.copytree(source/name, target/name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('README.md', 'requirements.txt', 'requirements-dev.txt'):
        if (source/name).exists():
            shutil.copyfile(source/name, target/name)


def controller_changed(source, control):
    def files(root):
        return {p.relative_to(root): p.read_bytes() for p in root.rglob('*.py')}
    return files(source/'ttar') != files(control/'ttar')


def main(candidate, old_commit):
    if os.geteuid() != 0:
        raise ReleaseError('Root control service required')
    root = Path(candidate).resolve()
    if root.parent != Path('/var/lib/ttar-release/candidates') or root.stat().st_uid != 0:
        raise ReleaseError('Invalid candidate directory')
    settings = json.loads(Path('/etc/ttar/maintenance.json').read_text())
    creds = json.loads(Path('/run/credentials/ttar-maintenance.service/maintenance.json').read_text())
    runtime_config = json.loads(Path('/etc/ttar/config.json').read_text())
    direct = runtime_config.get('telegram_transport') == 'direct'
    cloud = None if direct else Cloud(creds['release_cloud_key'])
    old = None if direct else cloud.live(settings['function_id'])
    commit = run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(root), 'rev-parse', 'HEAD']).decode().strip()
    deployed_marker = Path('/var/lib/ttar-release/deployed.json')
    previous_commit = json.loads(deployed_marker.read_text())['commit'] if deployed_marker.exists() else old_commit
    live_root = Path('/opt/ttar')
    policy = load_policy(root/'booking-policy.json')
    policy_file = live_root/'booking-policy.json'
    previous_policy = policy_file.read_bytes() if policy_file.exists() else None
    control = Path('/opt/ttar-control')
    reload_controller = controller_changed(root, control) or (root/'deploy/owner_apply.py').exists()
    old_units = {}
    for unit in (root/'deploy').glob('ttar-*.service'):
        # The cloud poller belongs to a separate cloud host.
        if unit.name == 'ttar-poller.service':
            continue
        target = Path('/etc/systemd/system')/unit.name
        if not target.exists() or target.read_bytes() != unit.read_bytes():
            old_units[unit.name] = target.read_text() if target.exists() else None
            reload_controller = True
    guard_dir = Path('/var/lib/ttar-release/guards')
    guard_dir.mkdir(mode=0o700, exist_ok=True)
    guard_script = guard_dir/(commit + '.py')
    # Capture the currently trusted guard, not its unactivated replacement.
    if reload_controller:
        shutil.copyfile(control/'ttar/controller_guard.py', guard_script)
        old_ready = Path('/var/lib/ttar-release/controller-ready.json')
        old_pid = json.loads(old_ready.read_text()).get('pid') if old_ready.exists() else None
        state_file = guard_dir/(commit + '.json')
        state_file.write_text(json.dumps({'commit': commit, 'old_commit': previous_commit, 'old_pid': old_pid,
            'old_policy': (previous_policy or b'{"regular_minutes":150}').decode(),
            'old_units': old_units, 'database': settings['database']}))
    files = ['webhook.py', 'telegram.py', '__init__.py']
    changed_cloud = not direct and any((root/'ttar'/f).read_bytes() != (live_root/'ttar'/f).read_bytes() for f in files)
    promoted = installed = policy_installed = control_installed = False
    try:
        if changed_cloud:
            tag = 'candidate-' + commit[:12]
            new = cloud.wait(cloud.request(BASE + '/versions', create_body(root, old, tag)))
            endpoint = f"https://functions.yandexcloud.net/{settings['function_id']}?integration=raw&tag={tag}"
            relay = CloudTelegram(endpoint, creds['release_cloud_key'])
            relay.call('getChatMember', chat_id=settings['allowed_chat_id'], user_id=220427487)
            cloud.tag(new['id'])
            promoted = True
        # Back up the live DB before any worker migration; never roll back votes.
        backup = Path('/var/backups/ttar')/('before-work-' + commit[:12] + '.sqlite3')
        with sqlite3.connect(settings['database']) as connection, sqlite3.connect(backup) as output:
            connection.backup(output)
        backup.chmod(0o600)
        install_tree(root, live_root)
        installed = True
        install_project_files(root, live_root)
        staged_policy = live_root/'booking-policy.next'
        staged_policy.write_text(json.dumps(policy) + '\n')
        staged_policy.chmod(0o644)
        staged_policy.replace(policy_file)
        policy_installed = True
        hook = root/'deploy/owner_apply.py'
        if hook.exists():
            run(['/opt/ttar/.venv/bin/python', str(hook), str(root), previous_commit], cwd=str(root))
        if reload_controller:
            install_tree(root, control)
            control_installed = True
            (control/'commit').write_text(commit)
            for name in old_units:
                target = Path('/etc/systemd/system')/name
                shutil.copyfile(root/'deploy'/name, target)
                target.chmod(0o644)
            if old_units:
                run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'restart', 'ttar-worker.service'])
        if (root/'ttar/recognizer.py').read_bytes() != (live_root/'ttar.previous/recognizer.py').read_bytes():
            run(['systemctl', 'restart', 'ttar-ocr.service'])
        time.sleep(2)
        run(['systemctl', 'is-active', '--quiet', 'ttar-worker.service', 'ttar-ocr.service'])
        Path('/var/lib/ttar-release/deployed.json').write_text(json.dumps({'commit': commit, 'at': time.time()}))
        if reload_controller:
            run(['systemd-run', '--quiet', '--collect', '--unit=ttar-reload-' + commit[:12],
                 '/opt/ttar/.venv/bin/python', str(guard_script), str(state_file)])
        return {'status': 'done', 'summary': 'Изменение опубликовано.',
                'technical': 'Тесты пройдены; БД сохранена; сервисы работают.' +
                    (' Прямой Telegram-транспорт.' if direct else
                     ' Облачная функция проверена и обновлена.' if changed_cloud else ' Облачная функция не требовала изменений.'),
                'rollback_status': 'not_needed', 'controller_reload': reload_controller}
    except Exception as error:
        recovered = True
        try:
            if control_installed:
                shutil.rmtree(control/'ttar')
                (control/'ttar.previous').rename(control/'ttar')
                (control/'commit').write_text(previous_commit)
                for name, text in old_units.items():
                    target = Path('/etc/systemd/system')/name
                    if text is None:
                        target.unlink(missing_ok=True)
                    else:
                        target.write_text(text)
                run(['systemctl', 'daemon-reload'])
            if policy_installed:
                if previous_policy is None:
                    policy_file.unlink()
                else:
                    restored = live_root/'booking-policy.restore'
                    restored.write_bytes(previous_policy)
                    restored.chmod(0o644)
                    restored.replace(policy_file)
            if promoted:
                cloud.tag(old['id'])
            if installed:
                shutil.rmtree(live_root/'ttar')
                (live_root/'ttar.previous').rename(live_root/'ttar')
                run(['systemctl', 'restart', 'ttar-worker.service', 'ttar-ocr.service'])
                run(['systemctl', 'is-active', '--quiet', 'ttar-worker.service', 'ttar-ocr.service'])
        except Exception:
            recovered = False
        return {'status': 'failed' if recovered else 'uncertain',
                'summary': 'Обновление не удалось; предыдущая версия восстановлена.' if recovered else 'Обновление не удалось, состояние сервиса требует проверки.',
                'technical': type(error).__name__ + '; результаты игр не откатывались.',
                'rollback_status': 'restored' if recovered else 'failed'}


if __name__ == '__main__':
    try:
        result = main(*sys.argv[1:])
    except Exception as error:
        result = {'status': 'failed', 'summary': 'Не удалось подготовить публикацию.',
                  'technical': type(error).__name__, 'rollback_status': 'not_needed'}
    print(json.dumps(result, ensure_ascii=False))
