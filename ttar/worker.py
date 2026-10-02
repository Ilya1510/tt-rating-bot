import fcntl
import json
import logging
import os
import signal
import threading
import time
from pathlib import Path

import boto3
from botocore.config import Config

from .bot import Bot, card_update
from .core import Store
from .recognizer import remote_recognize
from .telegram import TelegramError
from .cloud_telegram import CloudTelegram
from .webhook import chat_of

LOG = logging.getLogger('ttar')
STOP = threading.Event()


def receive(config, credentials):
    store = Store(config['database'])
    queue = boto3.client('sqs', endpoint_url='https://message-queue.api.cloud.yandex.net', region_name='ru-central1',
                         aws_access_key_id=credentials['queue_access_key_id'],
                         aws_secret_access_key=credentials['queue_secret_access_key'],
                         config=Config(connect_timeout=5, read_timeout=30, retries={'max_attempts': 2}))
    while not STOP.is_set():
        try:
            messages = queue.receive_message(QueueUrl=config['queue_url'], WaitTimeSeconds=20,
                                             VisibilityTimeout=60, MaxNumberOfMessages=10).get('Messages', [])
            for message in messages:
                update = json.loads(message['Body'])
                if type(update.get('update_id')) is not int:
                    raise ValueError('Invalid update')
                if chat_of(update) == config['allowed_chat_id']:
                    store.ingest(update)
                # After SQLite FULL commit the job survives queue deletion and process restart.
                queue.delete_message(QueueUrl=config['queue_url'], ReceiptHandle=message['ReceiptHandle'])
        except Exception as error:
            LOG.warning('Queue receive failed: %s', type(error).__name__)
            STOP.wait(5)
    store.db.close()


def flush_outbox(store, telegram):
    row = store.db.execute("SELECT * FROM outbox WHERE status='pending' AND ready_at<=? ORDER BY id LIMIT 1", (time.time(),)).fetchone()
    if row is None:
        return False
    payload = json.loads(row['payload'])
    draft_id = payload.pop('_draft_id', None)
    revision = payload.pop('_draft_revision', None)
    method = row['method']
    if method == 'updateDraftCard':
        # Resolve the current count when sending, including delayed retries.
        method, payload = card_update(store, payload)
    try:
        result = telegram.call(method, **payload)
    except TelegramError as error:
        if error.not_modified:
            result = {}
        else:
            attempts = row['attempts'] + 1
            # Expired callback acknowledgement is terminal; keep the card update separately.
            terminal = error.code in (400, 403, 404) or attempts >= 12
            delay = max(error.retry_after, min(3600, 5 * 2 ** min(attempts, 9)))
            with store.transaction():
                store.db.execute('UPDATE outbox SET attempts=?,ready_at=?,status=? WHERE id=?',
                                 (attempts, time.time() + delay, 'failed' if terminal else 'pending', row['id']))
            LOG.warning('Telegram outbox %s: code=%s attempts=%s', row['id'], error.code, attempts)
            return True
    with store.transaction():
        store.db.execute("UPDATE outbox SET status='sent' WHERE id=?", (row['id'],))
        if draft_id and isinstance(result, dict) and result.get('message_id'):
            store.register_card(draft_id, revision, payload['chat_id'], result['message_id'], 'text')
            if store.vote_count(draft_id) or store.photo(draft_id)['revision'] != revision:
                store.update_cards(draft_id, f"sent:{row['id']}")
    return True


def process_job(store, bot, job, chat_id):
    try:
        bot.handle(job)
        LOG.info('Job %s complete', job['id'])
    except Exception as error:
        store.retry(job['id'], type(error).__name__)
        LOG.warning('Job %s failed attempt %s: %s', job['id'], job['attempts'], type(error).__name__)
        if job['attempts'] >= 3:
            with store.transaction():
                store.send(f"failure:{job['id']}", 'sendMessage', {
                    'chat_id': chat_id,
                    'text': f"Не удалось обработать update #{job['id']} после 3 попыток. Рейтинг не изменён. Администратор может проверить журнал и повторить задание."})


def process_photos(config, credentials):
    # Separate SQLite connection and cloud adapter; only the main thread sends
    # the outbox, so concurrent OCR cannot duplicate or hold up card updates.
    store = Store(config['database'])
    telegram = CloudTelegram(config['telegram_cloud_url'], credentials['cloud_function_key'])
    bot = Bot(store, telegram, lambda data: remote_recognize(data, config['recognizer_socket']), config['allowed_chat_id'])
    try:
        while not STOP.is_set():
            job = store.claim(photos=True)
            if job:
                process_job(store, bot, job, config['allowed_chat_id'])
            else:
                STOP.wait(.2)
    finally:
        store.db.close()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = json.loads(Path(os.environ.get('TTAR_CONFIG', '/etc/ttar/config.json')).read_text())
    credential_dir = Path(os.environ.get('CREDENTIALS_DIRECTORY', '/etc/ttar'))
    secret_path = credential_dir/'credentials.json'
    # Deploy can safely start this unit before credentials/cloud configuration exist.
    while not STOP.is_set():
        config = json.loads(Path(os.environ.get('TTAR_CONFIG', '/etc/ttar/config.json')).read_text())
        if secret_path.exists() and config.get('queue_url') and config.get('allowed_chat_id'):
            break
        LOG.info('Waiting for cloud/queue configuration; no photos processed')
        STOP.wait(60)
    if STOP.is_set():
        return
    credentials = json.loads(secret_path.read_text())
    lock = open(str(config['database']) + '.worker.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    store = Store(config['database'])
    store.recover()
    telegram = CloudTelegram(config['telegram_cloud_url'], credentials['cloud_function_key'])
    bot = Bot(store, telegram, lambda data: remote_recognize(data, config['recognizer_socket']), config['allowed_chat_id'])
    thread = threading.Thread(target=receive, args=(config, credentials), daemon=True)
    thread.start()
    photos = threading.Thread(target=process_photos, args=(config, credentials))
    photos.start()
    LOG.info('Worker started, rating unit=%s; confirmations required', store.setting('unit'))
    while not STOP.is_set():
        job = store.claim(photos=False)
        if job:
            process_job(store, bot, job, config['allowed_chat_id'])
        if not flush_outbox(store, telegram) and not job:
            STOP.wait(.2)
    photos.join()  # Let an in-flight OCR finish before releasing the worker lock.
    thread.join(timeout=35)
    store.db.close()
    lock.close()


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    main()
