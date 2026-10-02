import json
import re
import html

from .core import balance_reached

from .webhook import chat_of
from .photo import fingerprints
from .recognizer import PARSER_VERSION

ROSTER = [('М', 'Максим'), ('И', 'Илья'), ('Р', 'Рома'), ('В', 'Валя')]
HELP = 'Пришли фото, проверь список и нажми «Подтвердить». Нужны 2 разных участника.\n/stat N — последние N партий. По умолчанию N = 1000.'


def parse_window(value, default=1000):
    if not value:
        return default
    if not value.isdigit() or not 1 <= int(value) <= 100000:
        raise ValueError('Укажи число партий: /stat 1000')
    return int(value)


def stats_button(store):
    return {'inline_keyboard': [[{'text': 'Посчитать статистику',
                                 'callback_data': 'stats:' + store.setting('stats_window')}]]}


def draft_footer(store, pid):
    photo = store.photo(pid)
    games = json.loads(photo['proposal'])
    votes = store.vote_count(pid)
    buttons = [[{'text': f'Подтвердить · {votes}/2',
                 'callback_data': f"confirm:{pid}:{photo['revision']}"}]]
    if photo['status'] == 'confirmed':
        text = f'Учтено партий: {len(games)}.'
        if votes:
            text += f' Подтвердили: {votes}/2.'
    else:
        text = f'Подтвердили: {votes}/2.\nЕсли всё верно, нажми «Подтвердить» ниже. Нужны 2 разных участника.'
        if not games:
            text = 'Завершённых партий на фото не нашёл.\n\n' + text
    buttons.extend(stats_button(store)['inline_keyboard'])
    return text, {'inline_keyboard': buttons}


def game_lines(store, pid):
    games = json.loads(store.photo(pid)['proposal'])
    lines = []
    for i, (a, b, sa, sb) in enumerate(games, 1):
        na, nb = html.escape(store.name(a)), html.escape(store.name(b))
        if sa > sb:
            na = f'<u>{na}</u>'
        else:
            nb = f'<u>{nb}</u>'
        lines.append(f'{i}. {na} — {nb} {sa}:{sb}')
    return lines


def draft(store, pid):
    lines = game_lines(store, pid)
    footer, keyboard = draft_footer(store, pid)
    return '\n'.join(lines + ['\n' + footer]), keyboard


def draft_pages(store, pid):
    # Reserve space for the changing footer so voting cannot move game rows
    # between messages near Telegram's length limit.
    lines = game_lines(store, pid)
    pages = split_message('\n'.join(lines), limit=3700) if lines else ['']
    footer, keyboard = draft_footer(store, pid)
    pages[-1] = (pages[-1] + '\n\n' + footer).lstrip('\n')
    return pages, keyboard


def queue_draft(store, pid, key):
    pages, keyboard = draft_pages(store, pid)
    photo = store.photo(pid)
    for i, page in enumerate(pages):
        payload = {'chat_id': photo['chat_id'], 'text': page, 'parse_mode': 'HTML'}
        if i == len(pages) - 1:
            payload.update(reply_markup=keyboard,
                           _draft_id=pid, _draft_revision=photo['revision'])
        store.send(f'{key}:{i}', 'sendMessage', payload)


def card_update(store, card):
    photo = store.photo(card['photo_id'])
    if photo['revision'] != card['revision']:
        text, keyboard = 'Список обновлён. Подтверди последний ответ бота.', stats_button(store)
    else:
        pages, keyboard = draft_pages(store, card['photo_id'])
        text = pages[-1]
    # Only the final message carries the vote button for a long list.
    text = split_message(text)[-1]
    return 'editMessageText', {
        'chat_id': card['chat_id'], 'message_id': card['message_id'], 'text': text,
        'parse_mode': 'HTML', 'reply_markup': keyboard}


def matrix_values(store, window):
    ids = []
    for initial, _ in ROSTER:
        row = store.db.execute('SELECT player_id FROM aliases WHERE alias=?', (initial.casefold(),)).fetchone()
        ids.append(row[0] if row else None)
    index = {pid: i for i, pid in enumerate(ids) if pid is not None}
    wins = [[0] * 4 for _ in range(4)]
    balance = [0] * 4
    games = store.recent_games(window)
    for game in games:
        if game['a'] not in index or game['b'] not in index:
            continue
        winner, loser = (game['a'], game['b']) if game['score_a'] > game['score_b'] else (game['b'], game['a'])
        w, l = index[winner], index[loser]
        wins[w][l] += 1
        balance[w] += 1
        if not balance_reached(game['score_a'], game['score_b']):
            balance[l] -= 1
    ratings = []
    for i, pid in enumerate(ids):
        wins[i][i] = balance[i]
        row = store.db.execute('SELECT rating FROM ratings WHERE player_id=?', (pid,)).fetchone()
        ratings.append(row[0] if row else 1000.)
    return wins, ratings, len(games)


def statistics(store, window=1000):
    values, ratings, count = matrix_values(store, window)
    names = [name for _, name in ROSTER]
    cells = [[''] + names] + [[name] + [str(n) for n in row] for name, row in zip(names, values)]
    widths = [max(len(row[col]) for row in cells) for col in range(5)]
    table = '\n'.join(' '.join(cell.ljust(widths[i]) if i == 0 else cell.rjust(widths[i])
                              for i, cell in enumerate(row)).rstrip() for row in cells)
    text = f'Последние {window} партий · учтено {count}\n<pre>{html.escape(table)}</pre>'
    text += '\nСтрока — победитель, столбец — проигравший. Диагональ — баланс.'
    text += '\n\nElo · вся история\n' + '\n'.join(f'{name}: {rating:.0f}' for name, rating in zip(names, ratings))
    return text, None


def split_message(text, limit=3900):
    parts, lines = [], []
    for line in text.splitlines():
        if len(line) > limit:
            raise ValueError('Слишком длинная строка результата')
        if lines and len('\n'.join(lines + [line])) > limit:
            parts.append('\n'.join(lines))
            lines = []
        lines.append(line)
    if lines:
        parts.append('\n'.join(lines))
    return parts or ['Завершённых партий на фото не нашёл.']


class Bot:
    def __init__(self, store, telegram, recognize, allowed_chat):
        self.store, self.telegram, self.recognize, self.allowed_chat = store, telegram, recognize, allowed_chat

    def handle(self, job):
        update = json.loads(job['payload'])
        if chat_of(update) != self.allowed_chat:
            with self.store.transaction():
                self.store.db.execute("UPDATE jobs SET status='done' WHERE id=?", (job['id'],))
            return
        callback = update.get('callback_query')
        message = callback.get('message', {}) if callback else update.get('message', {})
        actor = (callback or message).get('from', {}).get('id', 0)
        text = message.get('text', '') if not callback else ''
        command = text.split(maxsplit=1)[0].split('@')[0].lower() if text.startswith('/') else ''
        if re.fullmatch(r'(?:посчитать\s+)?стат[ау](?:\s+\d+)?', text.strip(), re.I):
            command = '/stat'
            numbers = re.findall(r'\d+', text)
            text = '/stat' + (' ' + numbers[0] if numbers else '')
        if text.strip().casefold() == 'подтвердить':
            command, text = '/confirm', '/confirm'
        # Resolve membership using trusted Telegram API, never message claims.
        stats_callback = callback and callback.get('data', '').startswith('stats:')
        needs_member = (callback and callback.get('data', '').startswith('confirm:')) or command == '/confirm'
        member = (not (callback or message).get('from', {}).get('is_bot', False)
                  and self.telegram.member(self.allowed_chat, actor)) if needs_member else False
        image = None
        raw = None
        if message.get('photo') and not callback:
            photo = max(message['photo'], key=lambda p: p.get('file_size', p['width'] * p['height']))
            old = self.store.db.execute('SELECT * FROM photos WHERE chat_id=? AND file_unique_id=?',
                (self.allowed_chat, photo['file_unique_id'])).fetchone()
            refresh = old and old['status'] == 'draft' and json.loads(old['raw']).get('_parser_version', 0) < PARSER_VERSION
            if not old or refresh:
                image = self.telegram.download(photo['file_id'])
                digest, pixel_digest = fingerprints(image)
                by_hash = self.store.db.execute('SELECT * FROM photos WHERE chat_id=? AND (sha256=? OR pixel_sha256=?)',
                    (self.allowed_chat, digest, pixel_digest)).fetchone()
                old = old or by_hash
                refresh = old and old['status'] == 'draft' and json.loads(old['raw']).get('_parser_version', 0) < PARSER_VERSION
                if not old or refresh:
                    raw = self.recognize(image)
                    raw['_parser_version'] = PARSER_VERSION
        with self.store.transaction():
            try:
                keyboard = None
                if stats_callback:
                    window = callback['data'].split(':')[-1]
                    response, keyboard = statistics(self.store, parse_window(window))
                    self.store.send(f"callback:{job['id']}", 'answerCallbackQuery', {'callback_query_id': callback['id']})
                elif callback:
                    match = re.fullmatch(r'confirm:(\d+):(\d+)', callback.get('data', ''))
                    if not match:
                        raise ValueError('Неизвестная кнопка')
                    pid, revision = int(match[1]), int(match[2])
                    self.store.confirm(pid, revision, actor, member)
                    if message.get('message_id'):
                        self.store.register_card(pid, revision, self.allowed_chat, message['message_id'], 'text')
                    self.store.update_cards(pid, f"vote:{job['id']}")
                    status, _ = draft_footer(self.store, pid)
                    self.store.send(f"callback:{job['id']}", 'answerCallbackQuery',
                                    {'callback_query_id': callback['id'], 'text': status.splitlines()[0]})
                    response = None
                elif message.get('photo'):
                    if old:
                        pid, new = old[0], False
                        if raw is not None:
                            self.store.replace_recognition(pid, raw)
                    else:
                        pid, new = self.store.put_photo(self.allowed_chat, message['message_id'], photo['file_unique_id'], digest,
                                                       message['date'], actor, raw, pixel_digest)
                    self.store.update_cards(pid, f"refresh:{job['id']}")
                    queue_draft(self.store, pid, f"reply:{job['id']}")
                    response = None
                else:
                    response, keyboard = self.command(command, text, actor, member, f"command:{job['id']}")
            except (ValueError, IndexError) as error:
                response, keyboard = str(error), None
                if callback:
                    self.store.send(f"callback:{job['id']}", 'answerCallbackQuery', {'callback_query_id': callback['id'], 'text': response[:190], 'show_alert': True})
                    response = None
            # Save mutation + outbox + completion atomically. Telegram retries cannot repeat a mutation.
            parts = split_message(response) if response is not None else []
            for i, part in enumerate(parts):
                payload = {'chat_id': self.allowed_chat, 'text': part}
                if stats_callback or command == '/stat':
                    payload['parse_mode'] = 'HTML'
                if i == len(parts) - 1 and keyboard:
                    payload['reply_markup'] = keyboard
                key = f"reply:{job['id']}" + (f':{i}' if i else '')
                self.store.send(key, 'sendMessage', payload)
            self.store.db.execute("UPDATE jobs SET status='done',last_error=NULL WHERE id=?", (job['id'],))

    def command(self, command, text, actor, member, key='command'):
        args = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ''
        if command == '/stat':
            return statistics(self.store, parse_window(args, int(self.store.setting('stats_window'))))
        if command == '/confirm':
            if args:
                pid, revision = map(int, args.split())
            else:
                photo = self.store.db.execute("SELECT * FROM photos WHERE status='draft' AND chat_id=? ORDER BY id DESC LIMIT 1", (self.allowed_chat,)).fetchone()
                if photo is None:
                    raise ValueError('Нет неподтверждённых партий.')
                pid, revision = photo['id'], photo['revision']
            self.store.confirm(pid, revision, actor, member)
            self.store.update_cards(pid, key)
            return draft_footer(self.store, pid)
        return HELP, None
