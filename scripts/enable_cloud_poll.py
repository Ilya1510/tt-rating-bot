#!/usr/bin/env python3
"""Replace unreliable direct webhook with private Cloud Functions polling.

Uses the existing function, queue and Lockbox secret; a free minute timer invokes
the handler. Telegram credentials enter Lockbox through stdin, never argv/logs.
"""
import json
import sys
import tempfile
import zipfile
from pathlib import Path

from deploy_cloud import PRIVATE, ROOT, STATE, save, yc

sys.path.insert(0, str(ROOT))
from ttar.telegram import Telegram


def main():
    state = json.loads(STATE.read_text())
    if state.get('ingress_mode') in ('cloud_only', 'network_probe'):
        raise SystemExit('Use scripts/enable_cloud_only.py; this legacy poller lacks the verified IPv4 route.')
    token = json.loads((PRIVATE/'secrets.json').read_text())['telegram_token']
    tg = Telegram(token)
    me, webhook = tg.call('getMe'), tg.call('getWebhookInfo')
    if me.get('username') != 'tt_chatgpt_rating_bot':
        raise SystemExit('Wrong bot; no changes made')
    if webhook.get('url') and webhook['url'] != state.get('webhook_url'):
        raise SystemExit('Unexpected webhook; no changes made')
    function, producer, folder = state['function_id'], state['producer_id'], state['folder_id']
    # Revoke public invocation before deploying a polling entry point.
    yc('serverless', 'function', 'deny-unauthenticated-invoke', function)
    yc('serverless', 'function', 'add-access-binding', function,
       '--role', 'functions.functionInvoker', '--service-account-id', producer)
    if not state.get('poll_secret_version_id'):
        version = yc('lockbox', 'secret', 'add-version', state['secret_id'],
                     '--base-version-id', state['secret_version_id'], '--payload', '-',
                     '--description', 'TTAR private cloud poller',
                     payload=[{'key': 'TG_TOKEN', 'text_value': token}])
        state['poll_secret_version_id'] = version['id']
        state['previous_secret_version_id'] = state['secret_version_id']
        state['secret_version_id'] = version['id']
        save(STATE, state)
    if not state.get('poll_function_version_id'):
        with tempfile.TemporaryDirectory(prefix='ttar-poll-') as temp:
            source = Path(temp)/'function.zip'
            with zipfile.ZipFile(source, 'w') as package:
                package.write(ROOT/'ttar/webhook.py', 'index.py')
                package.writestr('requirements.txt', 'boto3==1.43.107\n')
            cmd = ['serverless', 'function', 'version', 'create', '--function-id', function,
                   '--runtime', 'python312', '--entrypoint', 'index.poll_handler', '--memory', '256m',
                   '--execution-timeout', '10s', '--service-account-id', producer,
                   '--source-path', str(source), '--no-logging',
                   '--environment', f"ALLOWED_CHAT_ID={state['allowed_chat_id']},QUEUE_URL={state['queue_url']},ACCEPT_FROM={state['accept_from']}"]
            for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'TG_TOKEN'):
                cmd += ['--secret', f"environment-variable={key},id={state['secret_id']},version-id={state['poll_secret_version_id']},key={key}"]
            state['poll_function_version_id'] = yc(*cmd)['id']
            state['function_version_id'] = state['poll_function_version_id']
            save(STATE, state)
        print('Private cloud polling handler deployed', flush=True)
    if not state.get('poll_timer_id'):
        triggers = yc('serverless', 'trigger', 'list', '--folder-id', folder)
        existing = [x for x in triggers if x.get('name') == 'ttar-telegram-poll']
        if existing:
            raise SystemExit('Timer already exists without deployment state; inspect before adopting')
        timer = yc('serverless', 'trigger', 'create', 'timer', '--name', 'ttar-telegram-poll',
                   '--folder-id', folder, '--cron-expression', '* * * * ? *',
                   '--invoke-function-id', function, '--invoke-function-tag', '$latest',
                   '--invoke-function-service-account-id', producer)
        state['poll_timer_id'] = timer['id']
        save(STATE, state)
        print('Minute timer created', flush=True)
    # Preserve the user's queued photo. No competing getUpdates runs before this.
    tg.call('deleteWebhook', drop_pending_updates=False)
    state['ingress_mode'] = 'cloud_poll'
    state['webhook_configured'] = False
    save(STATE, state)
    print(json.dumps({'mode': 'cloud_poll', 'function_id': function,
                      'timer_id': state['poll_timer_id'], 'pending_updates_preserved': True}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'Cloud poll deployment stopped: {type(error).__name__}; raw output withheld', file=sys.stderr)
        raise SystemExit(1)
