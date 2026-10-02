"""Cloud entry point: authenticate, filter, then persist to YMQ before HTTP 200."""
import base64
import hmac
import hashlib
import json
import os
import re
import urllib.error
import urllib.request

import boto3
from botocore.config import Config


def chat_of(update):
    if 'callback_query' in update:
        return update['callback_query'].get('message', {}).get('chat', {}).get('id')
    return update.get('message', {}).get('chat', {}).get('id')


def accept(event, secret, chat_id, publish, accept_from=0):
    headers = {k.lower(): str(v) for k, v in event.get('headers', {}).items()}
    if event.get('httpMethod') != 'POST':
        return {'statusCode': 405, 'body': 'POST required'}
    supplied = headers.get('x-telegram-bot-api-secret-token', '')
    if not secret or not supplied.isascii() or not hmac.compare_digest(supplied, secret):
        return {'statusCode': 403, 'body': 'Forbidden'}
    try:
        body = event.get('body', '')
        if event.get('isBase64Encoded'):
            body = base64.b64decode(body, validate=True).decode()
        if len(body.encode()) > 64000:
            return {'statusCode': 413, 'body': 'Too large'}
        update = json.loads(body)
        if type(update.get('update_id')) is not int:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        return {'statusCode': 400, 'body': 'Bad update'}
    if chat_of(update) != chat_id:
        return {'statusCode': 200, 'body': 'Ignored'}
    message = update.get('message', {})
    if message and message.get('date', 0) < accept_from:
        return {'statusCode': 200, 'body': 'Old message ignored'}
    text = message.get('text', '').strip()
    plain_command = re.fullmatch(r'(?:посчитать\s+)?стат[ау](?:\s+\d+)?|подтвердить', text, re.I)
    if not ('callback_query' in update or 'photo' in message or text.startswith('/') or plain_command):
        return {'statusCode': 200, 'body': 'Ignored'}
    try:
        publish(json.dumps(update, ensure_ascii=False))
    except Exception:
        # Never log SDK exceptions, headers, payload or credential-bearing URLs.
        return {'statusCode': 503, 'body': 'Queue unavailable'}
    return {'statusCode': 200, 'body': 'Saved'}


def handler(event, context):
    client = boto3.client('sqs', endpoint_url='https://message-queue.api.cloud.yandex.net',
                          region_name='ru-central1', config=Config(connect_timeout=2, read_timeout=3, retries={'max_attempts': 1}))
    return accept(event, os.environ['WEBHOOK_SECRET'], int(os.environ['ALLOWED_CHAT_ID']),
                  lambda body: client.send_message(QueueUrl=os.environ['QUEUE_URL'], MessageBody=body),
                  int(os.environ.get('ACCEPT_FROM', '0')))


def accept_trigger(update, chat_id, publish, accept_from=0):
    """Internal ingestion from the trusted cloud poller."""
    response = accept({'httpMethod': 'POST',
                       'headers': {'x-telegram-bot-api-secret-token': 'internal'},
                       'body': json.dumps(update)}, 'internal', chat_id, publish, accept_from)
    # Raw invocation treats a returned HTTP 503 dictionary as a successful call.
    # Raising instead activates the trigger's bounded retries and dead-letter queue.
    if response['statusCode'] >= 500:
        raise RuntimeError('Queue persistence failed')
    if response['statusCode'] != 200:
        raise ValueError('Invalid Telegram update')
    return response


def poll_updates(call, publish, chat_id, accept_from=0, max_batches=3):
    """Acknowledge Telegram only after the complete batch is durably in YMQ.

    No local offset is necessary: Telegram removes updates when an offset higher
    than their update_id is passed. The response to that call remains unacknowledged
    until its own batch has been persisted. A crash can repeat saved updates.
    """
    options = {'limit': 10, 'timeout': 0, 'allowed_updates': ['message', 'callback_query']}
    updates = call('getUpdates', **options)
    received = saved = 0
    for _ in range(max_batches):
        if not updates:
            break
        ids = []
        for update in updates:
            if type(update.get('update_id')) is not int:
                raise ValueError('Invalid Telegram update ID')
            result = accept_trigger(update, chat_id, publish, accept_from)
            saved += result['body'] == 'Saved'
            received += 1
            ids.append(update['update_id'])
        # The next response is deliberately left unacknowledged if the limit or
        # execution timeout ends this invocation. Telegram retains it for next run.
        updates = call('getUpdates', offset=max(ids) + 1, **options)
    return {'received': received, 'saved': saved}


def telegram_call(method, **payload):
    request = urllib.request.Request(f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/{method}",
                                     data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            body = response.read(1000001)
        if len(body) > 1000000:
            raise ValueError()
        result = json.loads(body)
        if not result.get('ok'):
            raise ValueError()
        return result['result']
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'Telegram polling failed: HTTP {error.code}') from None
    except urllib.error.URLError as error:
        raise RuntimeError('Telegram polling failed: ' + type(error.reason).__name__) from None
    except Exception as error:
        # Never let urllib expose the secret-bearing Bot API URL in exceptions.
        raise RuntimeError('Telegram polling failed: ' + type(error).__name__) from None


def poll_handler(event, context):
    # This entry point must be private and invoked only by the IAM timer.
    client = boto3.client('sqs', endpoint_url='https://message-queue.api.cloud.yandex.net',
                         region_name='ru-central1', config=Config(connect_timeout=2, read_timeout=3, retries={'max_attempts': 1}))
    return poll_updates(telegram_call,
                        lambda body: client.send_message(QueueUrl=os.environ['QUEUE_URL'], MessageBody=body),
                        int(os.environ['ALLOWED_CHAT_ID']), int(os.environ.get('ACCEPT_FROM', '0')))


def relay_action(event, telegram, chat_id):
    """Only required bot operations, reachable behind Cloud IAM authentication."""
    from ttar.telegram import TelegramError
    action, payload = event.get('telegram_action'), event.get('payload', {})
    if not isinstance(payload, dict):
        return {'ok': False, 'code': 'invalid_payload'}
    if action not in ('sendMessage', 'editMessageText', 'answerCallbackQuery', 'getChatMember', 'download'):
        return {'ok': False, 'code': 'forbidden_method'}
    if action in ('sendMessage', 'editMessageText', 'getChatMember') and payload.get('chat_id') != chat_id:
        return {'ok': False, 'code': 'forbidden_chat'}
    try:
        if action == 'download':
            data = telegram.download(payload['file_id'])
            offset = payload.get('offset', 0)
            if type(offset) is not int or not 0 <= offset < len(data):
                return {'ok': False, 'code': 'invalid_offset'}
            # 1 MiB chunks stay below Cloud Functions' 3.5 MB JSON limit.
            result = {'data': base64.b64encode(data[offset:offset + 1024 * 1024]).decode(),
                      'total': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        else:
            result = telegram.call(action, **payload)
        return {'ok': True, 'result': result}
    except TelegramError as error:
        result = {'ok': False, 'code': error.code, 'retry_after': error.retry_after}
        if error.not_modified:
            result['not_modified'] = True
        return result
    except Exception:
        return {'ok': False, 'code': 'invalid_request'}


def cloud_handler(event, context):
    """Private IAM-only endpoint for timer ingestion and the VM's cloud adapter."""
    from ttar.telegram import Telegram
    telegram = Telegram(os.environ['TG_TOKEN'], os.environ['TG_IPV4_ADDRESS'])
    chat_id = int(os.environ['ALLOWED_CHAT_ID'])
    # integration=raw sends request bytes rather than an HTTP envelope.
    if isinstance(event, (bytes, str)):
        event = json.loads(event)
    if 'telegram_action' in event:
        return relay_action(event, telegram, chat_id)
    client = boto3.client('sqs', endpoint_url='https://message-queue.api.cloud.yandex.net',
                         region_name='ru-central1', config=Config(connect_timeout=2, read_timeout=3, retries={'max_attempts': 1}))
    return poll_updates(telegram.call,
                        lambda body: client.send_message(QueueUrl=os.environ['QUEUE_URL'], MessageBody=body),
                        chat_id, int(os.environ.get('ACCEPT_FROM', '0')))
