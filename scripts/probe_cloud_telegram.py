#!/usr/bin/env python3
"""Compare Telegram routes inside our private function, with safe diagnostics."""
import json
import subprocess
import tempfile
import zipfile
from pathlib import Path

from deploy_cloud import ROOT, STATE, save, yc

SOURCE = '''import http.client, json, os, socket, time
def handler(event, context):
    host = 'api.telegram.org'
    records = socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)
    routes = []
    for family, _, _, _, address in records:
        item = (family, address[0])
        if item not in routes: routes.append(item)
    routes = routes[:3]
    if (socket.AF_INET, '149.154.167.220') not in routes:
        routes.append((socket.AF_INET, '149.154.167.220'))
    result = []
    for family, address in routes:
        started = time.monotonic()
        connection = http.client.HTTPSConnection(host, timeout=4)
        def connect(target, timeout, source_address=None, **kwargs):
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            try: sock.connect((address, 443))
            except Exception:
                sock.close()
                raise
            return sock
        connection._create_connection = connect
        item = {'family': 'IPv4' if family == socket.AF_INET else 'IPv6', 'address': address}
        try:
            connection.request('POST', '/bot' + os.environ['TG_TOKEN'] + '/getMe',
                               body=b'{}', headers={'Content-Type': 'application/json'})
            response = connection.getresponse()
            body = json.loads(response.read(131072))
            item.update(status=response.status, ok=body.get('ok', False))
            if body.get('ok'): item['bot_id'] = body['result']['id']
        except Exception as error: item['error'] = type(error).__name__
        finally: connection.close()
        item['seconds'] = round(time.monotonic() - started, 2)
        result.append(item)
    return {'routes': result}
'''


def main():
    state = json.loads(STATE.read_text())
    if state.get('poll_timer_id') and not state.get('poll_timer_paused'):
        yc('serverless', 'trigger', 'pause', state['poll_timer_id'])
        state['poll_timer_paused'] = True
        save(STATE, state)
    with tempfile.TemporaryDirectory(prefix='ttar-network-') as temp:
        path = Path(temp)/'function.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('index.py', SOURCE)
        version = yc('serverless', 'function', 'version', 'create',
                     '--function-id', state['function_id'], '--runtime', 'python312',
                     '--entrypoint', 'index.handler', '--memory', '256m',
                     '--execution-timeout', '30s', '--service-account-id', state['producer_id'],
                     '--source-path', str(path), '--no-logging', '--secret',
                     f"environment-variable=TG_TOKEN,id={state['secret_id']},version-id={state['poll_secret_version_id']},key=TG_TOKEN")
        state['network_probe_version_id'] = version['id']
        state['function_version_id'] = version['id']
        state['ingress_mode'] = 'network_probe'
        save(STATE, state)
    run = subprocess.run(['yc', 'serverless', 'function', 'invoke', state['function_id'],
                          '--data', '{}'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if run.returncode:
        print('Probe invocation failed; raw output withheld')
        return
    result = json.loads(run.stdout)
    print(json.dumps(result, ensure_ascii=False))  # handler returns only safe route metadata


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Cloud probe failed: ' + type(error).__name__ + '; raw output withheld')
        raise SystemExit(1)
