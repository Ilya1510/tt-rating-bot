import configparser, json, os, random, tempfile, time
from . import http as requests
from .storage import load_mapping, save_mapping
from .safe_log import safe_print as print

TG_TOKEN = os.environ['TG_TOKEN']
TG_CHAT_ID = os.environ['TG_CHAT_ID']
VK_TOKEN = os.environ['VK_TOKEN']
VK_PEER_ID = int(os.environ['VK_PEER_ID'])
VK_CONFIRM = os.environ['VK_CONFIRM']
TG_BOT_USERNAME = os.environ['TG_BOT_USERNAME']
REQUEST_TIMEOUT = (5, 8)
VK_GROUP_ID = os.environ.get('VK_GROUP_ID', '').strip()
TG_CLIENT_CONFIG = os.environ.get('TG_CLIENT_CONFIG', '/opt/tg-vk-bridge/.tg_client.conf')
TG_CLIENT_SESSION = os.environ.get('TG_CLIENT_SESSION', '/opt/tg-vk-bridge/.tg_client')

VK_REACTIONS = {
    1: '❤️', 2: '🔥', 3: '😂', 4: '👍',
    5: '💩', 7: '😭', 8: '😡', 9: '👎', 10: '👌', 11: '😃',
    12: '🤔', 13: '🙏', 15: '😍', 19: '😮',
    16: '🎉', 18: '🤝', 26: '👏', 27: '😢', 30: '😱', 42: '🤮',
}

def get_vk_name(user_id):
    try:
        r = requests.get('https://api.vk.com/method/users.get', params={
            'access_token': VK_TOKEN,
            'user_ids': user_id,
            'v': '5.199',
        }, timeout=REQUEST_TIMEOUT).json()
        user = r['response'][0]
        return f"{user['first_name']} {user['last_name']}"
    except Exception:
        return str(user_id)

def send_tg(text, photo_url=None, reply_to=None):
    payload = {'chat_id': TG_CHAT_ID}
    if reply_to:
        payload['reply_to_message_id'] = reply_to
    if photo_url:
        payload['caption'] = text
        r = requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendPhoto',
                         json={**payload, 'photo': photo_url}, timeout=REQUEST_TIMEOUT)
    else:
        payload['text'] = text
        r = requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage', json=payload, timeout=REQUEST_TIMEOUT)
    try:
        return r.json()['result']['message_id']
    except Exception:
        raise RuntimeError('Telegram delivery failed') from None

def upload_photo_to_vk(photo_url):
    try:
        server = requests.get('https://api.vk.com/method/photos.getMessagesUploadServer', params={
            'access_token': VK_TOKEN,
            'peer_id': VK_PEER_ID,
            'v': '5.199',
        }, timeout=REQUEST_TIMEOUT).json()['response']['upload_url']
        img_data = requests.get(photo_url, timeout=REQUEST_TIMEOUT).content
        uploaded = requests.post(server, files={'photo': ('photo.jpg', img_data, 'image/jpeg')}, timeout=REQUEST_TIMEOUT).json()
        saved = requests.post('https://api.vk.com/method/photos.saveMessagesPhoto', params={
            'access_token': VK_TOKEN,
            'photo': uploaded['photo'],
            'server': uploaded['server'],
            'hash': uploaded['hash'],
            'v': '5.199',
        }, timeout=REQUEST_TIMEOUT).json()['response'][0]
        return f"photo{saved['owner_id']}_{saved['id']}"
    except Exception as e:
        print("Photo upload error:", e)
        return None

class TelegramFileError(Exception):
    pass

def get_tg_file_url(file_id):
    body = requests.get(
        f'https://api.telegram.org/bot{TG_TOKEN}/getFile',
        params={'file_id': file_id},
        timeout=REQUEST_TIMEOUT,
    ).json()
    if not body.get('ok'):
        raise TelegramFileError(body.get('description') or 'Telegram getFile failed')
    file_path = body['result']['file_path']
    return f'https://api.telegram.org/file/bot{TG_TOKEN}/{file_path}'

def get_vk_doc_upload_url():
    return requests.get(
        'https://api.vk.com/method/docs.getMessagesUploadServer',
        params={
            'access_token': VK_TOKEN,
            'peer_id': VK_PEER_ID,
            'type': 'doc',
            'v': '5.199',
        },
        timeout=REQUEST_TIMEOUT,
    ).json()['response']['upload_url']

def save_vk_uploaded_doc(uploaded, filename):
    saved = requests.post(
        'https://api.vk.com/method/docs.save',
        data={
            'access_token': VK_TOKEN,
            'file': uploaded['file'],
            'title': filename,
            'v': '5.199',
        },
        timeout=(5, 20),
    ).json()['response']['doc']
    return f"doc{saved['owner_id']}_{saved['id']}"

def upload_video_file_to_vk(path, filename='telegram-video.mp4', mime_type='video/mp4'):
    try:
        upload_url = get_vk_doc_upload_url()
        with open(path, 'rb') as video_file:
            uploaded = requests.post(
                upload_url,
                files={'file': (filename, video_file, mime_type)},
                timeout=(10, 120),
            ).json()
        return save_vk_uploaded_doc(uploaded, filename)
    except Exception as e:
        print("Video file upload error:", e)
        return None

def upload_video_url_to_vk(video_url, filename='telegram-video.mp4', mime_type='video/mp4'):
    try:
        upload_url = get_vk_doc_upload_url()
        with tempfile.NamedTemporaryFile() as tmp:
            with requests.get(video_url, stream=True, timeout=(5, 60)) as download:
                download.raise_for_status()
                for chunk in download.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        tmp.write(chunk)
            tmp.seek(0)
            upload = requests.post(
                upload_url,
                files={'file': (filename, tmp, mime_type)},
                timeout=(10, 120),
            ).json()
        return save_vk_uploaded_doc(upload, filename)
    except Exception as e:
        print("Video URL upload error:", e)
        return None

def download_tg_media_with_client(chat_id, message_id, output_dir):
    if not os.path.exists(TG_CLIENT_CONFIG) or not os.path.exists(f'{TG_CLIENT_SESSION}.session'):
        print("TG client fallback unavailable")
        return None

    try:
        from telethon.sync import TelegramClient
    except Exception as e:
        print("TG client import error:", repr(e))
        return None

    cfg = configparser.ConfigParser()
    cfg.read(TG_CLIENT_CONFIG)
    api_id = int(cfg['telegram']['api_id'])
    api_hash = cfg['telegram']['api_hash']

    try:
        with TelegramClient(TG_CLIENT_SESSION, api_id, api_hash, use_ipv6=True) as client:
            message = client.get_messages(int(chat_id), ids=int(message_id))
            if not message or not message.media:
                print("TG client fallback: no media")
                return None
            return client.download_media(message, file=output_dir)
    except Exception as e:
        print("TG client fallback error:", repr(e))
        return None

def send_vk(text, attachment=None, reply_to=None):
    params = {
        'access_token': VK_TOKEN,
        'peer_id': VK_PEER_ID,
        'random_id': random.randint(0, 2**31),
        'v': '5.199',
    }
    if text:
        params['message'] = text
    if attachment:
        params['attachment'] = attachment
    if reply_to:
        params['forward'] = json.dumps({
            'peer_id': VK_PEER_ID,
            'conversation_message_ids': [int(reply_to)],
            'is_reply': True,
        })
    print("SEND_VK params:", {k: v for k, v in params.items() if k != 'access_token'})
    r = requests.post('https://api.vk.com/method/messages.send', data=params, timeout=REQUEST_TIMEOUT)
    print("SEND_VK response:", r.text)
    try:
        msg_id = r.json()['response']
        # получаем conversation_message_id по обычному id
        info = requests.get('https://api.vk.com/method/messages.getById', params={
            'access_token': VK_TOKEN,
            'message_ids': msg_id,
            'v': '5.199',
        }, timeout=REQUEST_TIMEOUT).json()
        cmid = info['response']['items'][0]['conversation_message_id']
        return cmid
    except Exception:
        raise RuntimeError('VK delivery failed') from None

def poll_telegram():
    mapping = load_mapping()
    params = {
        'timeout': 0,
        'limit': 20,
        'allowed_updates': json.dumps(['message', 'message_reaction']),
    }
    if mapping.get('telegram_offset'):
        params['offset'] = mapping['telegram_offset']

    try:
        r = requests.get(
            f'https://api.telegram.org/bot{TG_TOKEN}/getUpdates',
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        updates = r.json().get('result', [])
    except Exception as e:
        print("TG_POLL getUpdates error:", repr(e))
        return {'statusCode': 200, 'body': json.dumps({'updates': 0, 'processed': 0, 'error': 'getUpdates failed'})}
    print("TG_POLL updates:", len(updates))

    processed = 0
    for update in updates:
        update_id = update.get('update_id')
        if update_id is None:
            continue
        try:
            handler({'body': json.dumps(update)}, None)
            processed += 1
            mapping = load_mapping()
            mapping['telegram_offset'] = update_id + 1
            save_mapping(mapping)
        except Exception as e:
            print("TG_POLL update error:", update_id, repr(e))
            break

    return {'statusCode': 200, 'body': json.dumps({'updates': len(updates), 'processed': processed})}

def handler(event, context):
    handler_started = time.time()
    body_raw = event.get('body', '{}')
    if event.get('isBase64Encoded'):
        import base64
        body_raw = base64.b64decode(body_raw).decode('utf-8')
    try:
        body = json.loads(body_raw)
    except Exception:
        return {'statusCode': 400, 'body': 'bad json'}
    print("BODY keys:", list(body.keys()))

    if body.get('mode') == 'telegram_poll':
        return poll_telegram()

    mapping = load_mapping()

    # защита от дублей Telegram
    update_id = body.get('update_id')
    if update_id:
        if update_id in mapping.get('processed_updates', []):
            print("DUPLICATE update_id:", update_id)
            return {'statusCode': 200, 'body': 'ok'}
        mapping.setdefault('processed_updates', []).append(update_id)

    # VK Callback API
    if 'type' in body:
        if body['type'] == 'confirmation':
            return {'statusCode': 200, 'body': VK_CONFIRM}

        if body['type'] == 'message_new':
            msg = body.get('object', {}).get('message', {})
            if msg.get('peer_id') == VK_PEER_ID:
                from_id = msg.get('from_id', 0)
                text = msg.get('text', '')
                attachments = msg.get('attachments', [])
                vk_msg_id = msg.get('conversation_message_id') or msg.get('cmid') or msg.get('id')
                reply_info = msg.get('reply_message')

                if from_id > 0:
                    # Callback and long-poll may overlap briefly during migration.
                    if vk_msg_id and str(vk_msg_id) in mapping['vk_to_tg']:
                        return {'statusCode': 200, 'body': 'already delivered'}
                    name = get_vk_name(from_id)

                    tg_reply_to = None
                    if reply_info:
                        reply_cmid = str(reply_info.get('conversation_message_id') or reply_info.get('cmid') or reply_info.get('id', ''))
                        tg_reply_to = mapping['vk_to_tg'].get(reply_cmid)

                    tg_msg_id = None
                    for att in attachments:
                        if att.get('type') == 'photo':
                            sizes = att['photo']['sizes']
                            photo_url = sorted(sizes, key=lambda x: x['width'])[-1]['url']
                            tg_msg_id = send_tg(f'[VK] {name}:', photo_url=photo_url, reply_to=tg_reply_to)
                    if text:
                        tg_msg_id = send_tg(f'[VK] {name}: {text}', reply_to=tg_reply_to)

                    if tg_msg_id and vk_msg_id:
                        mapping['tg_to_vk'][str(tg_msg_id)] = str(vk_msg_id)
                        mapping['vk_to_tg'][str(vk_msg_id)] = tg_msg_id
                        save_mapping(mapping)

        if body['type'] == 'message_reaction_event':
            obj = body.get('object', {})
            from_id = obj.get('reacted_id', 0)
            reaction_id = obj.get('reaction_id', 0)
            cmid = str(obj.get('cmid') or obj.get('conversation_message_id', ''))
            emoji = VK_REACTIONS.get(reaction_id, f'реакция #{reaction_id}')
            if from_id > 0:
                name = get_vk_name(from_id)
                tg_reply_to = mapping['vk_to_tg'].get(cmid)
                send_tg(f'[VK] {name} поставил {emoji}', reply_to=tg_reply_to)

        return {'statusCode': 200, 'body': 'ok'}

    # Telegram реакции
    if 'message_reaction' in body:
        reaction = body['message_reaction']
        if reaction.get('date'):
            print("TG_LAG seconds:", round(handler_started - reaction['date'], 3))
        if str(reaction.get('chat', {}).get('id', '')) == TG_CHAT_ID:
            from_user = reaction.get('user', {}).get('first_name', 'Кто-то')
            new_reactions = reaction.get('new_reaction', [])
            tg_msg_id = str(reaction.get('message_id', ''))
            if new_reactions:
                emoji = new_reactions[0].get('emoji', '👍')
                vk_reply_to = mapping['tg_to_vk'].get(tg_msg_id)
                send_vk(f'[TG] {from_user} поставил {emoji}', reply_to=vk_reply_to)

    # Telegram сообщения
    if 'message' in body:
        msg = body['message']
        if msg.get('date'):
            print("TG_LAG seconds:", round(handler_started - msg['date'], 3))
        if str(msg.get('chat', {}).get('id', '')) != TG_CHAT_ID:
            return {'statusCode': 200, 'body': 'ok'}
        if msg.get('from', {}).get('is_bot'):
            return {'statusCode': 200, 'body': 'ok'}

        text = msg.get('text', '') or ''
        caption = msg.get('caption', '') or ''
        entities = msg.get('entities', []) or msg.get('caption_entities', [])
        from_user = msg.get('from', {}).get('first_name', 'Кто-то')
        last_name = msg.get('from', {}).get('last_name', '')
        full_name = f"{from_user} {last_name}".strip()
        photo = msg.get('photo')
        video = msg.get('video')
        content = text or caption
        tg_msg_id = str(msg.get('message_id', ''))

        reply_info = msg.get('reply_to_message')
        vk_reply_to = None
        if reply_info:
            reply_tg_id = str(reply_info.get('message_id', ''))
            vk_reply_to = mapping['tg_to_vk'].get(reply_tg_id)
            print("TG REPLY:", reply_tg_id, "→ VK:", vk_reply_to)

        # если упомянут бот — не пересылаем
        bot_mentioned = False
        for ent in entities:
            if ent.get('type') == 'mention':
                mention = content[ent['offset']:ent['offset']+ent['length']]
                if mention.lstrip('@').lower() == TG_BOT_USERNAME.lower():
                    bot_mentioned = True
                    break

        if bot_mentioned:
            return {'statusCode': 200, 'body': 'ok'}

        vk_msg_id = None
        if photo:
            file_id = photo[-1]['file_id']
            photo_url = get_tg_file_url(file_id)
            attachment = upload_photo_to_vk(photo_url)
            vk_msg_id = send_vk(f'[TG] {full_name}: {content}', attachment=attachment, reply_to=vk_reply_to)
        elif video:
            file_id = video['file_id']
            filename = video.get('file_name') or f"telegram-video-{tg_msg_id}.mp4"
            mime_type = video.get('mime_type') or 'video/mp4'
            attachment = None
            try:
                video_url = get_tg_file_url(file_id)
                attachment = upload_video_url_to_vk(video_url, filename=filename, mime_type=mime_type)
            except TelegramFileError as e:
                print("TG getFile video error:", str(e))
                with tempfile.TemporaryDirectory() as tmp_dir:
                    local_video = download_tg_media_with_client(TG_CHAT_ID, tg_msg_id, tmp_dir)
                    if local_video:
                        attachment = upload_video_file_to_vk(local_video, filename=filename, mime_type=mime_type)
            if not attachment:
                content = f"{content}\n[видео не удалось скачать из Telegram]".strip()
            vk_msg_id = send_vk(f'[TG] {full_name}: {content}', attachment=attachment, reply_to=vk_reply_to)
        elif content:
            vk_msg_id = send_vk(f'[TG] {full_name}: {content}', reply_to=vk_reply_to)

        if vk_msg_id and tg_msg_id:
            mapping['tg_to_vk'][tg_msg_id] = str(vk_msg_id)
            mapping['vk_to_tg'][str(vk_msg_id)] = int(tg_msg_id)
            save_mapping(mapping)

    return {'statusCode': 200, 'body': 'ok'}
