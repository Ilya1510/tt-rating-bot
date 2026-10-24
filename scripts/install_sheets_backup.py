#!/usr/bin/env python3
"""Enable a pre-created Google history sheet; secrets travel over SSH stdin only."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ttar.sheets_backup import SheetsClient, TITLE

INSTALL = r'''
import json,os,pathlib,subprocess,sys
p=json.load(sys.stdin)
if not pathlib.Path('/opt/ttar/ttar/sheets_backup.py').exists():
 raise SystemExit('Deploy sheets_backup module first')
r=subprocess.run(['systemd-creds','encrypt','--with-key=host','--name=google-backup.json','-','-'],
 input=json.dumps(p['credentials']).encode(),capture_output=True)
if r.returncode: raise SystemExit('Credential encryption failed')
target=pathlib.Path('/etc/credstore.encrypted/ttar-google-backup.json')
target.write_bytes(r.stdout);target.chmod(0o600)
drop=pathlib.Path('/etc/systemd/system/ttar-worker.service.d');drop.mkdir(exist_ok=True)
(drop/'google-backup.conf').write_text('[Service]\nLoadCredentialEncrypted=google-backup.json:/etc/credstore.encrypted/ttar-google-backup.json\n')
path=pathlib.Path('/etc/ttar/config.json');config=json.loads(path.read_text())
config['google_backup']=p['config']
staged=path.with_suffix('.next');staged.write_text(json.dumps(config));staged.chmod(path.stat().st_mode & 0o777);staged.replace(path)
for args in (['systemctl','daemon-reload'],['systemctl','restart','ttar-worker.service']):
 r=subprocess.run(args,capture_output=True)
 if r.returncode: raise SystemExit('Service configuration failed')
print('Google backup enabled; verify the first successful synchronization')
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--spreadsheet-id', required=True)
    parser.add_argument('--credentials', type=Path, required=True)
    parser.add_argument('--host', default='ilya-grid-vm')
    args = parser.parse_args()
    credentials = json.loads(args.credentials.read_text())
    client = SheetsClient(credentials)
    meta = client.call(args.spreadsheet_id, '?fields=sheets.properties')
    sheets = [s['properties'] for s in meta['sheets'] if s['properties']['title'] == TITLE]
    if len(sheets) != 1:
        raise ValueError('Expected one history tab')
    config = {'spreadsheet_id': args.spreadsheet_id, 'sheet_id': sheets[0]['sheetId']}
    # Refuse installation if the server cannot authenticate/reach this exact sheet.
    probe = "import sys,json;sys.path.insert(0,'/opt/ttar');from ttar.sheets_backup import SheetsClient;p=json.load(sys.stdin);SheetsClient(p['credentials']).call(p['config']['spreadsheet_id'],'?fields=spreadsheetId');print('Google access verified')"
    import shlex
    payload = json.dumps({'credentials': credentials, 'config': config}).encode()
    for code in (probe, INSTALL):
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', args.host,
            'sudo -n /opt/ttar/.venv/bin/python -c ' + shlex.quote(code)],
            input=payload, capture_output=True, timeout=300)
        if result.returncode:
            raise RuntimeError('Remote setup failed')
        print(result.stdout.decode().strip())


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Google backup setup failed: ' + type(error).__name__ + '; raw diagnostics withheld')
        raise SystemExit(1) from None
