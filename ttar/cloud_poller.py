"""Dedicated ingress on a cloud VM; Telegram credentials never reach the worker."""
import fcntl
import json
import os
import signal
import threading
from pathlib import Path

import boto3
from botocore.config import Config

from .telegram import Telegram, TelegramError
from .webhook import accept_trigger


class PollTelegram(Telegram):
    def _read(self, path, payload=None, limit=1000000, timeout=25):
        return super()._read(path, payload, limit, max(timeout, 35))


def run(stop, telegram, publish, chat_id, accept_from=0):
    offset = None
    backoff = 1
    while not stop.is_set():
        try:
            options = {'limit': 100, 'timeout': 25,
                       'allowed_updates': ['message', 'callback_query']}
            if offset is not None:
                options['offset'] = offset
            updates = telegram.call('getUpdates', **options)
            ids = []
            saved = 0
            for update in updates:
                if type(update.get('update_id')) is not int:
                    raise ValueError('Invalid update ID')
                result = accept_trigger(update, chat_id, publish, accept_from)
                saved += result['body'] == 'Saved'
                ids.append(update['update_id'])
            # Only the NEXT request acknowledges this batch, after all writes.
            # Restart/retry may repeat a batch; the worker deduplicates update_id.
            if ids:
                offset = max(ids) + 1
                print(json.dumps({'received': len(ids), 'saved': saved}), flush=True)
            backoff = 1
        except TelegramError as error:
            if error.code in (401, 409):
                print('Polling stopped: credential or competing-consumer conflict', flush=True)
                return 78
            print('Telegram temporarily unavailable', flush=True)
            stop.wait(max(backoff, min(error.retry_after or 0, 300)))
            backoff = min(backoff * 2, 30)
        except Exception:
            # SDK exceptions can contain credentials or request payloads.
            print('Polling or persistence temporarily unavailable', flush=True)
            stop.wait(backoff)
            backoff = min(backoff * 2, 30)
    return 0


def main():
    config = json.loads(Path('/etc/ttar-poller/config.json').read_text())
    credentials = json.loads((Path(os.environ['CREDENTIALS_DIRECTORY']) / 'credentials.json').read_text())
    lock = open('/run/ttar-poller/worker.lock', 'w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    telegram = PollTelegram(credentials['telegram_token'], '149.154.167.220')
    if telegram.call('getMe').get('username') != 'tt_chatgpt_rating_bot':
        raise RuntimeError('Wrong bot')
    if telegram.call('getWebhookInfo').get('url'):
        raise RuntimeError('Webhook is still active; no automatic deletion')
    client = boto3.client('sqs', endpoint_url='https://message-queue.api.cloud.yandex.net',
        region_name='ru-central1',
        aws_access_key_id=credentials['queue_access_key_id'],
        aws_secret_access_key=credentials['queue_secret_access_key'],
        config=Config(connect_timeout=2, read_timeout=3, retries={'max_attempts': 1}))
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *args: stop.set())
    return run(stop, telegram,
        lambda body: client.send_message(QueueUrl=config['queue_url'], MessageBody=body),
        int(config['chat_id']), int(config.get('accept_from', 0)))


if __name__ == '__main__':
    try:
        result = main()
    except Exception:
        print('Poller startup failed; raw output withheld', flush=True)
        result = 78
    raise SystemExit(result)
