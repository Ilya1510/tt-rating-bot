#!/usr/bin/env python3
"""Install the fixed server controller. No secrets in argv, Git, or output."""
import json
import shlex
import subprocess

from deploy_cloud import ROOT, PRIVATE, STATE

ROOT_INSTALL = r'''
import json,os,pathlib,pwd,shutil,subprocess,sys
from pathlib import Path
def run(argv,data=None):
 r=subprocess.run(argv,input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 if r.returncode: raise RuntimeError('Installation step failed: '+argv[0])
 return r.stdout
p=json.load(sys.stdin)
if not shutil.which('bwrap'): raise RuntimeError('Install bubblewrap before enabling the controller')
try: who=pwd.getpwnam('ttar-code')
except KeyError:
 run(['useradd','--system','--create-home','--home-dir','/var/lib/ttar-code','--shell','/usr/sbin/nologin','ttar-code'])
 who=pwd.getpwnam('ttar-code')
home=Path('/var/lib/ttar-code');home.mkdir(exist_ok=True);os.chown(home,0,0);home.chmod(0o755)
(home/'jobs').mkdir(exist_ok=True);os.chown(home/'jobs',0,0);(home/'jobs').chmod(0o755)
auth=home/'.codex';auth.mkdir(exist_ok=True);os.chown(auth,who.pw_uid,who.pw_gid);auth.chmod(0o700)
if not (auth/'auth.json').exists():
 shutil.copyfile('/var/lib/ttar-ocr/.codex/auth.json',auth/'auth.json')
 os.chown(auth/'auth.json',who.pw_uid,who.pw_gid);(auth/'auth.json').chmod(0o600)
release=Path('/var/lib/ttar-release');release.mkdir(exist_ok=True);release.chmod(0o711)
if not (release/'github_ed25519').exists(): raise RuntimeError('GitHub deploy key is not installed')
shutil.copyfile('/root/.ssh/known_hosts',release/'known_hosts');(release/'known_hosts').chmod(0o644)
root=Path('/opt/ttar-control');root.mkdir(exist_ok=True);root.chmod(0o755)
for name,source in p['files'].items():
 if not name.startswith('ttar/') or not name.endswith('.py') or '..' in name: raise RuntimeError('Unexpected control file')
 target=root/name;target.parent.mkdir(exist_ok=True);target.parent.chmod(0o755);target.write_text(source);target.chmod(0o644)
(root/'release_entry.py').write_text("import runpy\nrunpy.run_module('ttar.release',run_name='__main__')\n")
(root/'release_entry.py').chmod(0o644)
config=Path('/etc/ttar/maintenance.json');config.write_text(json.dumps(p['config']));config.chmod(0o600)
encrypted=run(['systemd-creds','encrypt','--with-key=host','--name=maintenance.json','-','-'],json.dumps(p['credentials']).encode())
target=Path('/etc/credstore.encrypted/ttar-maintenance.json');target.write_bytes(encrypted);target.chmod(0o600)
unit=Path('/etc/systemd/system/ttar-maintenance.service');unit.write_text(p['unit']);unit.chmod(0o644)
run(['systemctl','daemon-reload'])
env=dict(os.environ,GIT_SSH_COMMAND='ssh -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/var/lib/ttar-release/known_hosts -i /var/lib/ttar-release/github_ed25519')
if not (release/'repo').exists():
 r=subprocess.run(['git','clone','git@github.com:Ilya1510/tt-rating-bot.git',str(release/'repo')],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 if r.returncode: raise RuntimeError('Repository clone failed')
for parent,dirs,files in os.walk(release/'repo'):
 os.chmod(parent,0o755)
 for name in files:os.chmod(Path(parent)/name,0o644)
print(json.dumps({'control_installed':True,'calendar_credential':bool(p['credentials']['calendar_token']),'git_checkout':True}))
'''


def main():
    state = json.loads(STATE.read_text())
    data = {'files': {str(p.relative_to(ROOT)): p.read_text() for p in (ROOT/'ttar').glob('*.py')},
            'unit': (ROOT/'deploy/ttar-maintenance.service').read_text(),
            'config': {'database': '/var/lib/ttar/history.sqlite3', 'allowed_chat_id': state['allowed_chat_id'],
                       'function_id': state['function_id'], 'booking_schedule_enabled': True,
                       'work': {'deploy_command': ['/opt/ttar/.venv/bin/python', '/opt/ttar-control/release_entry.py']}},
            'credentials': {'release_cloud_key': json.loads((PRIVATE/'release-cloud-key.json').read_text())},
            'installer': ROOT_INSTALL}
    parent = r'''import json,os,subprocess,sys
p=json.load(sys.stdin)
token=os.environ.get('YANDEX_CALENDAR_TOKEN')
if not token:raise SystemExit('Calendar credential is not configured')
p['credentials']['calendar_token']=token
installer=p.pop('installer')
r=subprocess.run(['sudo','-n','python3','-c',installer],input=json.dumps(p).encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
if r.returncode:raise SystemExit('Controller installation failed; raw output withheld')
sys.stdout.buffer.write(r.stdout)
'''
    shell = 'set -a; source ~/.stefania/customize.env >/dev/null 2>&1; set +a; python3 -c ' + shlex.quote(parent)
    run = subprocess.run(['ssh', '-o', 'BatchMode=yes', 'ilya-grid-vm', 'bash -lc ' + shlex.quote(shell)],
                          input=json.dumps(data).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if run.returncode:
        raise RuntimeError('Controller setup failed; raw output withheld')
    print(run.stdout.decode().strip())


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Controller setup failed: ' + type(error).__name__ + '; raw output withheld')
        raise SystemExit(1)
