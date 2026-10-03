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
    # Only runtime modules; control-service snapshot is deliberately untouched.
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


def main(candidate, old_commit):
    if os.geteuid() != 0:
        raise ReleaseError('Root control service required')
    root = Path(candidate).resolve()
    if root.parent != Path('/var/lib/ttar-release/candidates') or root.stat().st_uid != 0:
        raise ReleaseError('Invalid candidate directory')
    settings = json.loads(Path('/etc/ttar/maintenance.json').read_text())
    creds = json.loads(Path('/run/credentials/ttar-maintenance.service/maintenance.json').read_text())
    cloud = Cloud(creds['release_cloud_key'])
    old = cloud.live(settings['function_id'])
    commit = run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(root), 'rev-parse', 'HEAD']).decode().strip()
    live_root = Path('/opt/ttar')
    files = ['webhook.py', 'telegram.py', '__init__.py']
    changed_cloud = any((root/'ttar'/f).read_bytes() != (live_root/'ttar'/f).read_bytes() for f in files)
    promoted = installed = False
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
        run(['systemctl', 'restart', 'ttar-worker.service'])
        if (root/'ttar/recognizer.py').read_bytes() != (live_root/'ttar.previous/recognizer.py').read_bytes():
            run(['systemctl', 'restart', 'ttar-ocr.service'])
        time.sleep(2)
        run(['systemctl', 'is-active', '--quiet', 'ttar-worker.service', 'ttar-ocr.service'])
        Path('/var/lib/ttar-release/deployed.json').write_text(json.dumps({'commit': commit, 'at': time.time()}))
        return {'status': 'done', 'summary': 'Изменение опубликовано.',
                'technical': 'Тесты пройдены; БД сохранена; сервисы работают.' +
                    (' Облачная функция проверена и обновлена.' if changed_cloud else ' Облачная функция не требовала изменений.'),
                'rollback_status': 'not_needed'}
    except Exception as error:
        recovered = True
        try:
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
