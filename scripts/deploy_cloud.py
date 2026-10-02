#!/usr/bin/env python3
"""Run only after cost approval and confirmation that this bot has no other poller.

All subprocess output is captured. Keys enter Lockbox / SSH through stdin, never
through argv. No personal yc configuration is copied to the VM.
"""
import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ttar.telegram import Telegram

PRIVATE = Path.home()/'.config/ttar'
STATE = ROOT/'.deploy-state.json'


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.ttar-')
    with os.fdopen(fd, 'w') as out:
        json.dump(data, out)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def yc(*args, payload=None):
    run = subprocess.run(['yc', *args, '--format', 'json'],
                         input=json.dumps(payload).encode() if payload is not None else None,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if run.returncode:
        print(f'Cloud operation failed: {" ".join(args[:3])} (exit {run.returncode})', file=sys.stderr)
        raise RuntimeError(f'yc operation {" ".join(args[:3])} failed (exit {run.returncode}); raw output withheld')
    return json.loads(run.stdout) if run.stdout.strip() else {}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--approved-costs', action='store_true', required=True)
    p.add_argument('--bot-idle-confirmed', action='store_true', required=True)
    p.add_argument('--chat-id', required=True, type=int)
    p.add_argument('--bot-username', required=True)
    args = p.parse_args()
    if args.chat_id >= 0:
        raise SystemExit('Expected a negative group chat ID')
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if state.get('ingress_mode') in ('cloud_only', 'network_probe'):
        raise SystemExit('Use scripts/enable_cloud_only.py for the cloud-only transport; direct Telegram access on VM is disabled.')
    cloud_poll = state.get('ingress_mode') == 'cloud_poll'
    secret_path = PRIVATE/'cloud-secrets.json'
    creds = json.loads(secret_path.read_text()) if secret_path.exists() else {}
    tg_secret = json.loads((PRIVATE/'secrets.json').read_text())['telegram_token']
    tg = Telegram(tg_secret)
    me = tg.call('getMe')
    if me.get('username', '').casefold() != args.bot_username.lstrip('@').casefold():
        raise SystemExit('Saved token does not belong to the requested bot; no cloud changes made.')
    info = tg.call('getWebhookInfo')
    known_url = state.get('webhook_url', '')
    if info.get('url') and info['url'] != known_url:
        raise SystemExit('Existing webhook belongs to another handler. Deployment stopped before cloud changes.')
    chat = tg.call('getChat', chat_id=args.chat_id)
    if chat.get('type') not in ('group', 'supergroup'):
        raise SystemExit('Chat must be a group or supergroup')
    member = tg.call('getChatMember', chat_id=args.chat_id, user_id=me['id'])
    if not me.get('can_read_all_group_messages') and member.get('status') not in ('administrator', 'creator'):
        raise SystemExit('Bot cannot read group photos: disable privacy in BotFather or make it a group admin')
    print(json.dumps({'bot': me['username'], 'group': chat.get('title'), 'chat_id': args.chat_id}, ensure_ascii=False))
    # Avoid provisioning paid resources while the independent recognizer cannot run.
    probe = subprocess.run(['ssh', '-o', 'BatchMode=yes', 'ilya-grid-vm',
        "sudo -n -u ttar-ocr env HOME=/var/lib/ttar-ocr CODEX_HOME=/var/lib/ttar-ocr/.codex "
        "sh -c 'cd /var/lib/ttar-ocr && /opt/ttar/bin/codex login status'"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if probe.returncode:
        raise SystemExit('Recognizer needs Codex login on ilya-grid-vm; no cloud changes made.')
    def checkpoint():
        save(STATE, state)
        save(secret_path, creds)
    if 'folder_id' not in state:
        folders = yc('resource-manager', 'folder', 'list', '--cloud-id', 'b1gpepleoc65o075hq1c')
        if any(folder.get('name') == 'tt-rating' for folder in folders):
            raise SystemExit('Folder tt-rating already exists without local deployment state; inspect before adopting it')
        state['folder_id'] = yc('resource-manager', 'folder', 'create', '--cloud-id', 'b1gpepleoc65o075hq1c', '--name', 'tt-rating')['id']
        checkpoint()
    folder = state['folder_id']
    # Separate folder is necessary because YMQ roles are scoped to folder/cloud.
    for role, name in [('producer', 'ttar-webhook-sa'), ('consumer', 'ttar-worker-sa')]:
        if role + '_id' not in state:
            state[role + '_id'] = yc('iam', 'service-account', 'create', '--name', name, '--folder-id', folder)['id']
            checkpoint()
        if role not in creds:
            key = yc('iam', 'access-key', 'create', '--service-account-id', state[role + '_id'], '--description', 'TTAR queue only')
            creds[role] = {'access_key_id': key['access_key']['key_id'], 'secret_access_key': key['secret']}
            state[role + '_key_id'] = key['access_key']['id']
            checkpoint()
    producer, consumer = state['producer_id'], state['consumer_id']
    yc('resource-manager', 'folder', 'add-access-binding', folder, '--role', 'ymq.reader', '--service-account-id', consumer)
    yc('resource-manager', 'folder', 'add-access-binding', folder, '--role', 'ymq.writer', '--service-account-id', producer)
    yc('resource-manager', 'folder', 'add-access-binding', folder, '--role', 'ymq.admin', '--service-account-id', producer)
    queue = boto3.client('sqs', endpoint_url='https://message-queue.api.cloud.yandex.net', region_name='ru-central1',
                         aws_access_key_id=creds['producer']['access_key_id'],
                         aws_secret_access_key=creds['producer']['secret_access_key'])
    try:
        # IAM propagation can take a moment; retry only provisioning of our own queues.
        for attempt in range(8):
            try:
                dlq = queue.create_queue(QueueName='ttar-dead-letters', Attributes={'MessageRetentionPeriod': '1209600'})['QueueUrl']
                arn = queue.get_queue_attributes(QueueUrl=dlq, AttributeNames=['QueueArn'])['Attributes']['QueueArn']
                url = queue.create_queue(QueueName='ttar-updates', Attributes={'MessageRetentionPeriod': '1209600',
                    'VisibilityTimeout': '60', 'ReceiveMessageWaitTimeSeconds': '20',
                    'RedrivePolicy': json.dumps({'deadLetterTargetArn': arn, 'maxReceiveCount': 5})})['QueueUrl']
                state.update(queue_url=url, dlq_url=dlq)
                checkpoint()
                break
            except Exception as error:
                code = getattr(error, 'response', {}).get('Error', {}).get('Code', '')
                # Emit only bounded SDK error codes, never provider messages or URLs.
                if isinstance(code, str) and code.isascii() and code.replace('.', '').replace('_', '').isalnum():
                    print(f'Queue provisioning attempt {attempt + 1}: {code[:80]}', file=sys.stderr)
                if attempt == 7:
                    raise RuntimeError('Queue provisioning failed; raw exception withheld') from None
                time.sleep(5)
    finally:
        yc('resource-manager', 'folder', 'remove-access-binding', folder, '--role', 'ymq.admin', '--service-account-id', producer)
    if 'webhook_secret' not in creds:
        creds['webhook_secret'] = secrets.token_urlsafe(32)
        checkpoint()
    if 'secret_id' not in state:
        entries = [{'key': 'AWS_ACCESS_KEY_ID', 'text_value': creds['producer']['access_key_id']},
                   {'key': 'AWS_SECRET_ACCESS_KEY', 'text_value': creds['producer']['secret_access_key']},
                   {'key': 'WEBHOOK_SECRET', 'text_value': creds['webhook_secret']}]
        secret = yc('lockbox', 'secret', 'create', '--name', 'ttar-webhook-secrets', '--folder-id', folder, '--payload', '-', payload=entries)
        state['secret_id'] = secret['id']
        state['secret_version_id'] = secret['current_version']['id']
        checkpoint()
    yc('lockbox', 'secret', 'add-access-binding', state['secret_id'], '--role', 'lockbox.payloadViewer', '--service-account-id', producer)
    if 'function_id' not in state:
        state['function_id'] = yc('serverless', 'function', 'create', '--name', 'ttar-webhook', '--folder-id', folder)['id']
        state['webhook_url'] = 'https://functions.yandexcloud.net/' + state['function_id']
        checkpoint()
    state.setdefault('accept_from', int(time.time()))
    checkpoint()
    with tempfile.TemporaryDirectory(prefix='ttar-function-') as temp:
        archive_path = Path(temp)/'function.zip'
        with zipfile.ZipFile(archive_path, 'w') as archive:
            archive.write(ROOT/'ttar/webhook.py', 'index.py')
            archive.writestr('requirements.txt', 'boto3==1.43.107\n')
        cmd = ['serverless', 'function', 'version', 'create', '--function-id', state['function_id'],
               '--runtime', 'python312', '--entrypoint', 'index.poll_handler' if cloud_poll else 'index.handler', '--memory', '256m',
               '--execution-timeout', '10s', '--service-account-id', producer, '--source-path', str(archive_path), '--no-logging',
               '--environment', f"ALLOWED_CHAT_ID={args.chat_id},QUEUE_URL={state['queue_url']},ACCEPT_FROM={state['accept_from']}"]
        for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'TG_TOKEN' if cloud_poll else 'WEBHOOK_SECRET'):
            cmd.extend(['--secret', f"environment-variable={key},id={state['secret_id']},version-id={state['secret_version_id']},key={key}"])
        state['function_version_id'] = yc(*cmd)['id']
        if cloud_poll:
            state['poll_function_version_id'] = state['function_version_id']
        checkpoint()
    yc('serverless', 'function', 'set-scaling-policy', state['function_id'], '--tag', '$latest',
       '--zone-instances-limit', '1', '--zone-requests-limit', '2', '--provisioned-instances-count', '0')
    if cloud_poll:
        yc('serverless', 'function', 'deny-unauthenticated-invoke', state['function_id'])
    else:
        yc('serverless', 'function', 'allow-unauthenticated-invoke', state['function_id'])
    data = {'config': {'queue_url': state['queue_url'], 'allowed_chat_id': args.chat_id, 'accept_from': state['accept_from']},
            'credentials': {'telegram_token': tg_secret, 'queue_access_key_id': creds['consumer']['access_key_id'],
                            'queue_secret_access_key': creds['consumer']['secret_access_key']}}
    run = subprocess.run(['ssh', 'ilya-grid-vm', 'sudo -n /opt/ttar/.venv/bin/python /opt/ttar/deploy/configure_vm.py'],
                         input=json.dumps(data).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if run.returncode:
        raise SystemExit('VM credential installation failed; webhook unchanged. Raw output withheld.')
    if cloud_poll:
        tg.call('deleteWebhook', drop_pending_updates=False)
    else:
        tg.call('setWebhook', url=state['webhook_url'], secret_token=creds['webhook_secret'],
                allowed_updates=['message', 'callback_query'], max_connections=2, drop_pending_updates=False)
    tg.call('setMyCommands', commands=[{'command': c, 'description': desc} for c, desc in [
        ('top', 'Рейтинг'), ('stat', 'Статистика игрока'), ('pair', 'Личные встречи'), ('last', 'Последние партии'), ('help', 'Как пользоваться')]])
    state['allowed_chat_id'] = args.chat_id
    state['bot_username'] = me['username']
    state['webhook_configured'] = not cloud_poll
    checkpoint()
    print(json.dumps({'folder_id': folder, 'function_id': state['function_id'], 'bot_username': me['username'], 'webhook_configured': not cloud_poll}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Provider errors may include secret-bearing request URLs; never emit traceback.
        print(f'Deployment stopped: {type(error).__name__}. No raw credentials or request output shown.', file=sys.stderr)
        raise SystemExit(1)
