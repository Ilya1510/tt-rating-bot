#!/usr/bin/env python3
"""Install isolated TTAR ingress on the existing cloud VM, preserving bridge."""
import json
import subprocess
import sys

from deploy_cloud import PRIVATE, ROOT, STATE, save, yc
from ttar.telegram import TelegramError


def cloud_bot_call(method, **payload):
    """Admin metadata/menu calls using credentials that stay on the cloud VM."""
    import shlex
    if method not in ('getMe', 'getWebhookInfo', 'setMyCommands', 'getMyCommands'):
        raise ValueError('Cloud admin method is not allowed')
    code = '''import json,sys,subprocess
sys.path.insert(0,'/opt/ttar-poller')
from ttar.telegram import Telegram
p=json.load(sys.stdin)
try:
 assert p['method'] in ('getMe','getWebhookInfo','setMyCommands','getMyCommands')
 r=subprocess.run(['systemd-creds','decrypt','--name=credentials.json','/etc/credstore.encrypted/ttar-poller.json','-'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True)
 credentials=json.loads(r.stdout)
 result=Telegram(credentials['telegram_token'],'149.154.167.220').call(p['method'],**p['payload'])
 print(json.dumps({'ok':True,'result':result}))
except Exception:
 print(json.dumps({'ok':False}))
'''
    response = json.loads(remote('sudo -n python3 -c ' + shlex.quote(code),
                                 json.dumps({'method': method, 'payload': payload}).encode()))
    if not response['ok']:
        raise TelegramError('cloud_admin')
    return response['result']


class CloudTelegramAdmin:
    """Run deployment checks on the cloud host too; token travels on SSH stdin."""
    def __init__(self, token):
        self.token = token

    def call(self, method, **kwargs):
        import shlex
        code = '''import json,sys
p=json.load(sys.stdin)
namespace={}
exec(p['source'],namespace)
try:
 result=namespace['Telegram'](p['token'],'149.154.167.220').call(p['method'],**p['kwargs'])
 print(json.dumps({'ok':True,'result':result}))
except Exception:
 print(json.dumps({'ok':False}))
'''
        payload = {'source': (ROOT / 'ttar/telegram.py').read_text(),
                   'token': self.token, 'method': method, 'kwargs': kwargs}
        response = json.loads(remote('python3 -c ' + shlex.quote(code), json.dumps(payload).encode()))
        if not response['ok']:
            raise TelegramError('cloud_admin')
        return response['result']


REMOTE_INSTALL = r'''
import json,os,pathlib,pwd,subprocess,sys
def run(args, data=None):
    p=subprocess.run(args,input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if p.returncode: raise RuntimeError('Remote installation step failed: '+args[0])
    return p.stdout
p=json.load(sys.stdin)
try: pwd.getpwnam('ttar-poller')
except KeyError: run(['useradd','--system','--home-dir','/var/lib/ttar-poller','--create-home','--shell','/usr/sbin/nologin','ttar-poller'])
root=pathlib.Path('/opt/ttar-poller')
root.mkdir(mode=0o755,exist_ok=True)
for name,content in p['files'].items():
    if name not in ('ttar/__init__.py','ttar/cloud_poller.py','ttar/webhook.py','ttar/telegram.py'):
        raise RuntimeError('Unexpected file')
    target=root/name
    target.parent.mkdir(mode=0o755,exist_ok=True)
    target.write_text(content)
    os.chmod(target,0o644)
config=pathlib.Path('/etc/ttar-poller')
config.mkdir(mode=0o700,exist_ok=True)
(config/'config.json').write_text(json.dumps(p['config']))
os.chmod(config,0o755)
os.chmod(config/'config.json',0o644)
store=pathlib.Path('/etc/credstore.encrypted')
store.mkdir(mode=0o700,exist_ok=True)
credential=store/'ttar-poller.json'
encrypted=run(['systemd-creds','encrypt','--name=credentials.json','--with-key=host','-','-'],json.dumps(p['credentials']).encode())
credential.write_bytes(encrypted)
os.chmod(credential,0o600)
if not (root/'.venv/bin/python').exists():
    run(['python3','-m','venv',str(root/'.venv')])
run([str(root/'.venv/bin/python'),'-m','pip','install','--disable-pip-version-check','boto3==1.43.107'])
unit=pathlib.Path('/etc/systemd/system/ttar-poller.service')
unit.write_text(p['unit'])
os.chmod(unit,0o644)
run(['systemctl','daemon-reload'])
print('isolated_poller_installed')
'''


def remote(command, data=None):
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', 'tg-vk-poller', command],
                            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('Cloud VM command failed; raw output withheld')
    return result.stdout.decode().strip()


def main():
    import shlex
    state = json.loads(STATE.read_text())
    credentials = json.loads((PRIVATE / 'cloud-secrets.json').read_text())
    token = json.loads((PRIVATE / 'secrets.json').read_text())['telegram_token']
    telegram = CloudTelegramAdmin(token)
    if telegram.call('getMe').get('username') != 'tt_chatgpt_rating_bot':
        raise RuntimeError('Wrong bot')
    webhook = telegram.call('getWebhookInfo').get('url', '')
    if webhook and webhook != state.get('gateway_url'):
        raise RuntimeError('Unexpected webhook; no changes made')
    bridge_before = remote('systemctl show tg-vk-poller.service --property=ActiveEnterTimestamp --value')
    names = ('ttar/__init__.py', 'ttar/cloud_poller.py', 'ttar/webhook.py', 'ttar/telegram.py')
    payload = {'files': {name: (ROOT / name).read_text() for name in names},
               'unit': (ROOT / 'deploy/ttar-poller.service').read_text(),
               'config': {'chat_id': state['allowed_chat_id'], 'queue_url': state['queue_url'],
                          'accept_from': state.get('accept_from', 0)},
               'credentials': {'telegram_token': token,
                   'queue_access_key_id': credentials['producer']['access_key_id'],
                   'queue_secret_access_key': credentials['producer']['secret_access_key']}}
    remote('sudo -n python3 -c ' + shlex.quote(REMOTE_INSTALL), json.dumps(payload).encode())
    yc('serverless', 'trigger', 'pause', state['poll_timer_id'])
    if webhook:
        telegram.call('deleteWebhook', drop_pending_updates=False)
    try:
        remote('sudo -n systemctl enable ttar-poller.service')
        remote('sudo -n systemctl restart ttar-poller.service')
        active = remote('systemctl is-active ttar-poller.service')
        if active != 'active':
            raise RuntimeError('Poller not active')
    except Exception:
        # Do not start another consumer automatically after an ambiguous start.
        raise RuntimeError('Poller start requires inspection; cloud timer stays paused') from None
    bridge_after = remote('systemctl show tg-vk-poller.service --property=ActiveEnterTimestamp --value')
    if bridge_before != bridge_after:
        raise RuntimeError('Bridge start timestamp changed unexpectedly')
    state = json.loads(STATE.read_text())
    state.update(ingress_mode='cloud_vm_poll', poll_timer_paused=True,
                 webhook_configured=False, telegram_on_vm=False,
                 cloud_poller_host='tg-vk-poller', cloud_poller_instance_id='fhm853o1u3qil8f9sdgh')
    save(STATE, state)
    info = telegram.call('getWebhookInfo')
    print(json.dumps({'mode': state['ingress_mode'], 'service': active,
                      'webhook_empty': not info.get('url'),
                      'pending_updates': info.get('pending_update_count', 0),
                      'bridge_start_unchanged': True, 'telegram_on_worker_vm': False}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Cloud poller deployment failed: ' + type(error).__name__ + '; raw output withheld')
        raise SystemExit(1)
