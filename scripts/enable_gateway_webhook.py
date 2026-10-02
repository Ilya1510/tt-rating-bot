#!/usr/bin/env python3
"""Instant Telegram ingress through our gateway, retaining a private VM relay.

Deploy the function with enable_cloud_only.py first. Only our own timer and
webhook are changed. Pending Telegram updates are preserved on every switch.
"""
import json
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from deploy_cloud import PRIVATE, STATE, save, yc
from ttar.telegram import Telegram


def specification(state):
    return {'openapi': '3.0.0', 'info': {'title': 'TTAR Telegram webhook', 'version': '1'},
            'paths': {'/telegram': {'post': {'operationId': 'telegramWebhook',
                'responses': {'200': {'description': 'Persisted update'}},
                'x-yc-apigateway-integration': {'type': 'cloud_functions',
                    'function_id': state['function_id'], 'tag': '$latest',
                    'service_account_id': state['producer_id'], 'payload_format_version': '0.1'}}}}}


def probe(url, secret=None):
    # Wrong chat: proves authentication/function routing without creating a job.
    headers = {'Content-Type': 'application/json'}
    if secret:
        headers['X-Telegram-Bot-Api-Secret-Token'] = secret
    request = urllib.request.Request(url, data=json.dumps({
        'update_id': 0, 'message': {'chat': {'id': 0}, 'text': '/stat'}}).encode(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read(100)
    except urllib.error.HTTPError as error:
        return error.code, error.read(100)
    except Exception:
        raise RuntimeError('Gateway probe failed; raw output withheld') from None


def main():
    state = json.loads(STATE.read_text())
    token = json.loads((PRIVATE/'secrets.json').read_text())['telegram_token']
    secret = json.loads((PRIVATE/'cloud-secrets.json').read_text())['webhook_secret']
    tg = Telegram(token, '149.154.167.220')
    if tg.call('getMe').get('username') != 'tt_chatgpt_rating_bot':
        raise RuntimeError('Wrong bot')
    current = tg.call('getWebhookInfo').get('url', '')
    if current and current != state.get('gateway_url'):
        raise RuntimeError('Another webhook is active; no changes made')
    with tempfile.TemporaryDirectory(prefix='ttar-gateway-') as temp:
        path = Path(temp)/'spec.json'
        path.write_text(json.dumps(specification(state)))
        if state.get('gateway_id'):
            gateway = yc('serverless', 'api-gateway', 'update', state['gateway_id'],
                         '--spec', str(path), '--no-logging', '--execution-timeout', '15s')
        else:
            existing = yc('serverless', 'api-gateway', 'list', '--folder-id', state['folder_id'])
            if any(item.get('name') == 'ttar-telegram' for item in existing):
                raise RuntimeError('Gateway exists without deployment state; inspect before adopting')
            gateway = yc('serverless', 'api-gateway', 'create', '--name', 'ttar-telegram',
                         '--folder-id', state['folder_id'], '--spec', str(path), '--no-logging',
                         '--execution-timeout', '15s')
        state['gateway_id'] = gateway['id']
        state['gateway_url'] = 'https://' + gateway['domain'].removeprefix('https://').rstrip('/') + '/telegram'
        save(STATE, state)
    url = state['gateway_url']
    if probe(url)[0] != 403 or probe(url, secret) != (200, b'Ignored'):
        raise RuntimeError('Gateway authentication/routing check failed; polling unchanged')
    yc('serverless', 'trigger', 'pause', state['poll_timer_id'])
    state['poll_timer_paused'] = True
    save(STATE, state)
    try:
        tg.call('setWebhook', url=url, secret_token=secret, drop_pending_updates=False,
                max_connections=4, allowed_updates=['message', 'callback_query'])
        info = tg.call('getWebhookInfo')
        if info.get('url') != url:
            raise RuntimeError('Webhook URL mismatch')
    except Exception:
        # Restore ingress only if this is our webhook; never discard updates.
        active = tg.call('getWebhookInfo').get('url', '')
        if active == url:
            tg.call('deleteWebhook', drop_pending_updates=False)
            active = ''
        if not active:
            yc('serverless', 'trigger', 'resume', state['poll_timer_id'])
            state.update(poll_timer_paused=False, ingress_mode='cloud_only', webhook_configured=False)
            save(STATE, state)
        raise RuntimeError('Webhook setup failed; fallback attempted; raw output withheld') from None
    state.update(ingress_mode='cloud_gateway', webhook_configured=True, telegram_on_vm=False,
                 webhook_url=url)
    save(STATE, state)
    print(json.dumps({'mode': state['ingress_mode'], 'gateway_id': state['gateway_id'],
                      'poll_timer_paused': True, 'pending_updates': info.get('pending_update_count', 0),
                      'telegram_on_vm': False}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Gateway deployment failed: ' + type(error).__name__ + '; raw output withheld')
        raise SystemExit(1)
