"""Telegram and VK long polling, with a single durable delivery worker."""
import fcntl
import json
import os
import signal
import threading
import requests

from . import storage
from .safe_log import safe_print as log

STOP = threading.Event()


def vk(method, **params):
    response = requests.post('https://api.vk.com/method/' + method,
        data=dict(access_token=os.environ['VK_TOKEN'], v='5.199', **params), timeout=(5, 15))
    response.raise_for_status()
    body = response.json()
    if 'error' in body:
        raise RuntimeError('VK API error ' + str(body['error'].get('error_code')))
    return body['response']


def receive_telegram():
    while not STOP.is_set():
        try:
            params = {'timeout': 25, 'allowed_updates': ['message', 'message_reaction']}
            offset = storage.get_state('tg_cursor')
            if offset is not None:
                params['offset'] = offset
            response = requests.post('https://api.telegram.org/bot' + os.environ['TG_TOKEN'] + '/getUpdates',
                                     json=params, timeout=(5, 35))
            body = response.json()
            if not body.get('ok'):
                raise RuntimeError('Telegram API error ' + str(body.get('error_code')))
            updates = body['result']
            if updates:
                storage.persist_batch('tg', updates, max(u['update_id'] for u in updates) + 1)
        except Exception as error:
            log('Telegram receive failed:', type(error).__name__)
            STOP.wait(3)


def receive_vk():
    server = None
    while not STOP.is_set():
        try:
            if server is None:
                server = vk('groups.getLongPollServer', group_id=os.environ['VK_GROUP_ID'])
            ts = storage.get_state('vk_cursor', server['ts'])
            response = requests.get(server['server'], params={
                'act': 'a_check', 'key': server['key'], 'ts': ts, 'wait': 25}, timeout=(5, 35)).json()
            failed = response.get('failed')
            if failed == 1:
                storage.set_state('vk_cursor', response['ts'])
                log('VK reported expired history; cursor refreshed')
            elif failed in (2, 3):
                server = None
                if failed == 3:
                    server = vk('groups.getLongPollServer', group_id=os.environ['VK_GROUP_ID'])
                    storage.set_state('vk_cursor', server['ts'])
                    log('VK long poll history reset')
            elif failed:
                raise RuntimeError('VK long poll error')
            else:
                storage.persist_batch('vk', response['updates'], response['ts'])
        except Exception as error:
            log('VK receive failed:', type(error).__name__)
            STOP.wait(3)


def main():
    from . import legacy
    lock = open(os.environ['BRIDGE_DATABASE'] + '.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    threads = [threading.Thread(target=f) for f in (receive_telegram, receive_vk)]
    for thread in threads:
        thread.start()
    log('Local bridge started')
    try:
        while not STOP.is_set():
            job = storage.next_job()
            if not job:
                STOP.wait(.2)
                continue
            try:
                legacy.handler({'body': job['body']}, None)
                storage.finish(job['id'])
                log('Delivered', job['source'], job['id'])
            except Exception as error:
                storage.retry(job['id'])
                log('Delivery deferred', job['id'], type(error).__name__)
    finally:
        STOP.set()
        for thread in threads:
            thread.join()
        lock.close()


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    main()
