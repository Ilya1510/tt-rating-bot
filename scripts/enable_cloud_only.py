#!/usr/bin/env python3
"""Existing private function handles every Telegram operation; no new resources."""
import json
import subprocess
import tempfile
import zipfile
from pathlib import Path

from deploy_cloud import PRIVATE, ROOT, STATE, save, yc
import sys
sys.path.insert(0, str(ROOT))
from ttar.telegram import Telegram
from deploy_cloud_poller import cloud_bot_call


def main():
    state = json.loads(STATE.read_text())
    cloud_vm_poll = state.get('ingress_mode') == 'cloud_vm_poll'
    if cloud_vm_poll:
        call = cloud_bot_call
    else:
        token = json.loads((PRIVATE/'secrets.json').read_text())['telegram_token']
        call = Telegram(token, '149.154.167.220').call
    if call('getMe').get('username') != 'tt_chatgpt_rating_bot':
        raise RuntimeError('Wrong Telegram bot; no cloud changes made')
    webhook_url = call('getWebhookInfo').get('url', '')
    gateway_active = bool(state.get('gateway_url') and webhook_url == state['gateway_url'])
    if webhook_url and not gateway_active:
        raise RuntimeError('Unexpected active webhook; no cloud changes made')
    creds = json.loads((PRIVATE/'cloud-secrets.json').read_text())
    function, producer, consumer = state['function_id'], state['producer_id'], state['consumer_id']
    yc('serverless', 'function', 'deny-unauthenticated-invoke', function)
    yc('serverless', 'function', 'add-access-binding', function,
       '--role', 'functions.functionInvoker', '--service-account-id', consumer)
    key_path = PRIVATE/'cloud-function-key.json'
    if not key_path.exists():
        with tempfile.TemporaryDirectory(prefix='ttar-key-') as temp:
            output = Path(temp)/'key.json'
            yc('iam', 'key', 'create', '--service-account-id', consumer,
               '--description', 'TTAR private cloud transport only', '--output', str(output))
            key = json.loads(output.read_text())
            save(key_path, key)
            state['worker_function_key_id'] = key['id']
            save(STATE, state)
    key = json.loads(key_path.read_text())
    with tempfile.TemporaryDirectory(prefix='ttar-cloud-only-') as temp:
        source = Path(temp)/'function.zip'
        with zipfile.ZipFile(source, 'w') as package:
            package.write(ROOT/'ttar/webhook.py', 'index.py')
            package.write(ROOT/'ttar/telegram.py', 'ttar/telegram.py')
            package.write(ROOT/'ttar/__init__.py', 'ttar/__init__.py')
            package.writestr('requirements.txt', 'boto3==1.43.107\n')
        cmd = ['serverless', 'function', 'version', 'create', '--function-id', function,
               '--runtime', 'python312', '--entrypoint', 'index.cloud_handler', '--memory', '256m',
               '--execution-timeout', '60s', '--service-account-id', producer,
               '--source-path', str(source), '--tags', 'live', '--no-logging', '--environment',
               f"ALLOWED_CHAT_ID={state['allowed_chat_id']},QUEUE_URL={state['queue_url']},ACCEPT_FROM={state['accept_from']},TG_IPV4_ADDRESS=149.154.167.220"]
        for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'TG_TOKEN', 'WEBHOOK_SECRET'):
            cmd += ['--secret', f"environment-variable={name},id={state['secret_id']},version-id={state['poll_secret_version_id']},key={name}"]
        state['function_version_id'] = yc(*cmd)['id']
        state['cloud_only_version_id'] = state['function_version_id']
        save(STATE, state)
    yc('serverless', 'function', 'set-scaling-policy', function, '--tag', '$latest',
       '--zone-instances-limit', '1', '--zone-requests-limit', '2', '--provisioned-instances-count', '0')
    yc('serverless', 'function', 'set-scaling-policy', function, '--tag', 'live',
       '--zone-instances-limit', '1', '--zone-requests-limit', '2', '--provisioned-instances-count', '0')
    endpoint = 'https://functions.yandexcloud.net/' + function + '?integration=raw&tag=live'
    data = {'config': {'queue_url': state['queue_url'], 'allowed_chat_id': state['allowed_chat_id'],
                       'accept_from': state['accept_from'], 'telegram_cloud_url': endpoint},
            'credentials': {'cloud_function_key': key,
                            'queue_access_key_id': creds['consumer']['access_key_id'],
                            'queue_secret_access_key': creds['consumer']['secret_access_key']}}
    run = subprocess.run(['ssh', '-o', 'BatchMode=yes', 'ilya-grid-vm',
                          'sudo -n /opt/ttar/.venv/bin/python /opt/ttar/deploy/configure_vm.py'],
                         input=json.dumps(data).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if run.returncode:
        raise RuntimeError('VM cloud transport configuration failed; raw output withheld')
    state['ingress_mode'] = 'cloud_vm_poll' if cloud_vm_poll else ('cloud_gateway' if gateway_active else 'cloud_only')
    state['telegram_cloud_url'] = endpoint
    state['telegram_on_vm'] = False
    state['webhook_configured'] = gateway_active
    save(STATE, state)
    # Resume only after the worker has no Telegram credential or direct API client.
    yc('serverless', 'trigger', 'pause' if gateway_active or cloud_vm_poll else 'resume', state['poll_timer_id'])
    state['poll_timer_paused'] = gateway_active or cloud_vm_poll
    save(STATE, state)
    call('setMyCommands', commands=[
        {'command': 'confirm', 'description': 'Подтвердить последний список партий'},
        {'command': 'stat', 'description': 'Последние N партий, по умолчанию 1000'},
        {'command': 'work', 'description': 'Вопрос о теннисе; доработки — только Илья'},
        {'command': 'create_booking', 'description': 'Для Ильи: забронировать зал'},
        {'command': 'cancel_booking', 'description': 'Для Ильи: отменить нашу бронь'}])
    print(json.dumps({'mode': state['ingress_mode'], 'function_id': function,
                      'timer_id': state['poll_timer_id'], 'telegram_on_vm': False}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Cloud-only deployment failed: ' + type(error).__name__ + '; raw output withheld')
        raise SystemExit(1)
