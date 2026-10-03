import contextlib
import json
import sqlite3
import time
import unicodedata
from pathlib import Path
from .operations import SCHEMA as OPERATIONS_SCHEMA


def elo(a, b, winner, k=32):
    if winner not in (0, 1) or not 0 < k <= 200:
        raise ValueError('Неверный победитель или K')
    expected = 1 / (1 + 10 ** ((b - a) / 400))
    delta = k * ((1 if winner == 0 else 0) - expected)
    return a + delta, b - delta


def norm(value):
    return unicodedata.normalize('NFKC', value).strip().casefold()


def valid_score(a, b, unit='game'):
    if type(a) is not int or type(b) is not int or min(a, b) < 0 or max(a, b) > 200:
        raise ValueError('Счёт должен состоять из целых чисел 0–200')
    if a == b:
        raise ValueError('Ничья не является завершённой партией')
    high, low = max(a, b), min(a, b)
    if unit == 'game' and not any((high == target and low <= target - 2)
                                  or (high > target and high - low == 2)
                                  for target in (11, 21)):
        raise ValueError(f'{a}:{b} не похоже на завершённую партию до 11 или 21')


def balance_reached(a, b):
    valid_score(a, b)
    high, low = max(a, b), min(a, b)
    target = 21 if high >= 21 else 11
    return low >= target - 1


SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS players(id INTEGER PRIMARY KEY,name TEXT NOT NULL,telegram_id INTEGER);
CREATE TABLE IF NOT EXISTS aliases(alias TEXT NOT NULL,player_id INTEGER REFERENCES players(id),PRIMARY KEY(alias,player_id));
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,ready_at REAL NOT NULL DEFAULT 0,last_error TEXT);
CREATE TABLE IF NOT EXISTS photos(id INTEGER PRIMARY KEY,chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,file_unique_id TEXT NOT NULL,sha256 TEXT NOT NULL,occurred_at INTEGER NOT NULL,author_id INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'draft',raw TEXT NOT NULL,proposal TEXT NOT NULL,ambiguities TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,UNIQUE(chat_id,file_unique_id),UNIQUE(chat_id,sha256));
CREATE TABLE IF NOT EXISTS games(id INTEGER PRIMARY KEY,photo_id INTEGER REFERENCES photos(id),ordinal INTEGER NOT NULL,occurred_at INTEGER NOT NULL,a INTEGER NOT NULL REFERENCES players(id),b INTEGER NOT NULL REFERENCES players(id),score_a INTEGER NOT NULL,score_b INTEGER NOT NULL,active INTEGER NOT NULL DEFAULT 1,rating_a_before REAL,rating_b_before REAL,rating_a_after REAL,rating_b_after REAL,UNIQUE(photo_id,ordinal));
CREATE TABLE IF NOT EXISTS ratings(player_id INTEGER PRIMARY KEY REFERENCES players(id),rating REAL NOT NULL,wins INTEGER NOT NULL,losses INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,at REAL NOT NULL,actor INTEGER NOT NULL,action TEXT NOT NULL,entity TEXT NOT NULL,before_json TEXT,after_json TEXT);
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY,dedupe TEXT UNIQUE NOT NULL,method TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,ready_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS confirm_votes(photo_id INTEGER NOT NULL REFERENCES photos(id),revision INTEGER NOT NULL,user_id INTEGER NOT NULL,at REAL NOT NULL,PRIMARY KEY(photo_id,revision,user_id));
CREATE TABLE IF NOT EXISTS photo_cards(photo_id INTEGER NOT NULL REFERENCES photos(id),revision INTEGER NOT NULL,chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,kind TEXT NOT NULL,PRIMARY KEY(chat_id,message_id));
'''


class Store:
    def __init__(self, path, unit='game', k=32):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(SCHEMA + OPERATIONS_SCHEMA)
        booking_columns = {row[1] for row in self.db.execute('PRAGMA table_info(bookings)')}
        for name, definition in [('create_attempted', 'INTEGER NOT NULL DEFAULT 0'),
                                  ('pending_action', "TEXT NOT NULL DEFAULT 'book'")]:
            if name not in booking_columns:
                self.db.execute(f'ALTER TABLE bookings ADD COLUMN {name} {definition}')
        if 'pixel_sha256' not in {row[1] for row in self.db.execute('PRAGMA table_info(photos)')}:
            self.db.execute('ALTER TABLE photos ADD COLUMN pixel_sha256 TEXT')
        self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS photos_pixel_hash ON photos(chat_id,pixel_sha256) WHERE pixel_sha256 IS NOT NULL')
        for key, val in [('unit', unit), ('k', str(k)), ('configured', 'false'),
                         ('stats_window', '1000'), ('roster_configured', 'false')]:
            self.db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', (key, val))

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def setting(self, key):
        return self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()[0]

    def log(self, actor, action, entity, before=None, after=None):
        self.db.execute('INSERT INTO audit(at,actor,action,entity,before_json,after_json) VALUES (?,?,?,?,?,?)',
                        (time.time(), actor, action, entity, json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False)))

    def player(self, alias, create=False):
        alias = alias.strip()
        if not alias or len(alias) > 60 or any(c in alias for c in '?\n\r'):
            raise ValueError('Неоднозначное обозначение игрока')
        rows = self.db.execute('SELECT player_id FROM aliases WHERE alias=?', (norm(alias),)).fetchall()
        if len(rows) > 1:
            raise ValueError(f'Неоднозначный псевдоним {alias}')
        if rows:
            return rows[0][0]
        if not create:
            raise ValueError(f'Неизвестный игрок: {alias}')
        if len(alias) != 1 or not alias.isalpha():
            raise ValueError('Нового игрока задай одной буквой или добавь через /player')
        pid = self.db.execute('INSERT INTO players(name) VALUES (?)', (alias.upper(),)).lastrowid
        self.db.execute('INSERT INTO aliases VALUES (?,?)', (norm(alias), pid))
        return pid

    def name(self, pid):
        return self.db.execute('SELECT name FROM players WHERE id=?', (pid,)).fetchone()[0]

    def add_player(self, alias, name, telegram_id, actor):
        if not name.strip() or len(name) > 60:
            raise ValueError('Имя должно содержать 1–60 символов')
        rows = self.db.execute('SELECT player_id FROM aliases WHERE alias=?', (norm(alias),)).fetchall()
        if len(rows) > 1:
            raise ValueError('Сначала устрани неоднозначный псевдоним')
        before = None
        if rows:
            pid = rows[0][0]
            before = dict(self.db.execute('SELECT * FROM players WHERE id=?', (pid,)).fetchone())
            self.db.execute('UPDATE players SET name=?,telegram_id=? WHERE id=?', (name, telegram_id, pid))
        else:
            pid = self.db.execute('INSERT INTO players(name,telegram_id) VALUES (?,?)', (name, telegram_id)).lastrowid
            self.db.execute('INSERT INTO aliases VALUES (?,?)', (norm(alias), pid))
        self.db.execute('INSERT OR IGNORE INTO aliases VALUES (?,?)', (norm(name), pid))
        self.log(actor, 'player', str(pid), before, {'name': name, 'alias': alias, 'telegram_id': telegram_id})
        return pid

    def validate_games(self, games):
        if not games or len(games) > 200:
            raise ValueError('Нужно от 1 до 200 партий в явном порядке')
        result = []
        for game in games:
            if len(game) != 4:
                raise ValueError('Формат партии: М И 11:8')
            a, b, sa, sb = game
            valid_score(sa, sb, self.setting('unit'))
            create = self.setting('roster_configured') != 'true'
            pa, pb = self.player(a, create), self.player(b, create)
            if pa == pb:
                raise ValueError('Игрок не может играть с собой')
            result.append([pa, pb, sa, sb])
        return result

    def recognized_games(self, raw):
        warnings = list(raw['ambiguities'])
        if not raw['order_known']:
            warnings.append('Порядок партий не установлен')
        proposal = []
        for block in raw['blocks']:
            for row in block['rows']:
                if row['kind'] == 'uncertain':
                    warnings.append('Не удалось прочитать одну из партий')
                if row['kind'] != 'game':
                    continue
                if row['score_a'] is None or row['score_b'] is None:
                    warnings.append('Нечитаемый счёт')
                    continue
                try:
                    valid_score(row['score_a'], row['score_b'], self.setting('unit'))
                except ValueError:
                    continue  # subtotal/non-game numbers must not invalidate valid games
                game = [row.get('player_a', block['player_a']), row.get('player_b', block['player_b']),
                        row['score_a'], row['score_b']]
                try:
                    proposal.extend(self.validate_games([game]))
                except ValueError as error:
                    warnings.append(str(error))
        if len(proposal) > 200:
            raise ValueError('На одном фото слишком много партий')
        return proposal, list(dict.fromkeys(warnings))

    def put_photo(self, chat_id, message_id, unique_id, digest, at, author, raw, pixel_digest=None):
        old = self.db.execute('SELECT id FROM photos WHERE chat_id=? AND (file_unique_id=? OR sha256=? OR pixel_sha256=?)',
                              (chat_id, unique_id, digest, pixel_digest)).fetchone()
        if old:
            return old[0], False
        proposal, warnings = self.recognized_games(raw)
        pid = self.db.execute('INSERT INTO photos(chat_id,message_id,file_unique_id,sha256,occurred_at,author_id,raw,proposal,ambiguities,pixel_sha256) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (chat_id, message_id, unique_id, digest, at, author, json.dumps(raw, ensure_ascii=False), json.dumps(proposal), json.dumps(warnings, ensure_ascii=False), pixel_digest)).lastrowid
        return pid, True

    def replace_recognition(self, pid, raw):
        photo = self.photo(pid)
        if photo['status'] != 'draft':
            raise ValueError('Учтённую фотографию нельзя перераспознать')
        proposal, warnings = self.recognized_games(raw)
        self.db.execute('UPDATE photos SET raw=?,proposal=?,ambiguities=?,revision=revision+1 WHERE id=?',
                        (json.dumps(raw, ensure_ascii=False), json.dumps(proposal),
                         json.dumps(warnings, ensure_ascii=False), pid))
        self.log(0, 'recognize', str(pid), {'revision': photo['revision']},
                 {'revision': photo['revision'] + 1, 'proposal': proposal})

    def recent_games(self, window=None, player=None, pair=None):
        where, args = ['g.active=1'], []
        if pair:
            a, b = pair
            where.append('((g.a=? AND g.b=?) OR (g.a=? AND g.b=?))')
            args.extend((a, b, b, a))
        elif player:
            where.append('(g.a=? OR g.b=?)')
            args.extend((player, player))
        sql = 'SELECT g.* FROM games g JOIN photos p ON p.id=g.photo_id WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY g.occurred_at DESC,p.message_id DESC,g.ordinal DESC,g.id DESC'
        if window:
            sql += ' LIMIT ?'
            args.append(window)
        return self.db.execute(sql, args).fetchall()

    def photo(self, pid):
        p = self.db.execute('SELECT * FROM photos WHERE id=?', (pid,)).fetchone()
        if p is None:
            raise ValueError('Черновик не найден')
        return p

    def fix_draft(self, pid, games, actor, is_admin, at=None):
        p = self.photo(pid)
        if p['status'] != 'draft':
            raise ValueError('Результат уже подтверждён; используй /edit или /undo')
        if actor != p['author_id'] and not is_admin:
            raise ValueError('Исправлять черновик может автор фотографии или администратор')
        proposal = self.validate_games(games)
        self.db.execute('UPDATE photos SET proposal=?,ambiguities=?,revision=revision+1,occurred_at=? WHERE id=?',
                        (json.dumps(proposal), '[]', at if at is not None else p['occurred_at'], pid))
        self.log(actor, 'fix_draft', str(pid), dict(p), {'proposal': proposal, 'occurred_at': at})

    def vote_count(self, pid, revision=None):
        revision = self.photo(pid)['revision'] if revision is None else revision
        return self.db.execute('SELECT count(*) FROM confirm_votes WHERE photo_id=? AND revision=?', (pid, revision)).fetchone()[0]

    def register_card(self, pid, revision, chat_id, message_id, kind):
        self.db.execute('INSERT OR IGNORE INTO photo_cards VALUES (?,?,?,?,?)', (pid, revision, chat_id, message_id, kind))

    def update_cards(self, pid, key):
        for card in self.db.execute('SELECT * FROM photo_cards WHERE photo_id=?', (pid,)).fetchall():
            self.send(f"card:{key}:{card['chat_id']}:{card['message_id']}", 'updateDraftCard', dict(card))

    def confirm(self, pid, revision, actor, is_member):
        if not self.db.in_transaction:
            with self.transaction():
                return self.confirm(pid, revision, actor, is_member)
        if not is_member or type(actor) is not int or actor <= 0:
            raise ValueError('Подтвердить могут участники этой группы.')
        p = self.photo(pid)
        if p['status'] == 'confirmed':
            return False
        if p['status'] != 'draft' or revision != p['revision']:
            raise ValueError('Черновик изменился; нажми кнопку под последним списком.')
        if self.setting('configured') != 'true':
            raise ValueError('Модель рейтинга ещё не настроена')
        proposal = json.loads(p['proposal'])
        vote = self.db.execute('INSERT OR IGNORE INTO confirm_votes VALUES (?,?,?,?)', (pid, revision, actor, time.time()))
        if vote.rowcount:
            self.log(actor, 'confirm_vote', str(pid), None, {'revision': revision, 'votes': self.vote_count(pid, revision)})
        if self.vote_count(pid, revision) < 2:
            return False
        for ordinal, (a, b, sa, sb) in enumerate(proposal):
            valid_score(sa, sb, self.setting('unit'))
            self.db.execute('INSERT INTO games(photo_id,ordinal,occurred_at,a,b,score_a,score_b) VALUES (?,?,?,?,?,?,?)',
                            (pid, ordinal, p['occurred_at'], a, b, sa, sb))
        self.db.execute("UPDATE photos SET status='confirmed' WHERE id=?", (pid,))
        self.log(actor, 'confirm', str(pid), None, proposal)
        self.recalculate()
        return True

    def recalculate(self):
        values = {r[0]: [1000., 0, 0] for r in self.db.execute('SELECT id FROM players')}
        self.db.execute('DELETE FROM ratings')
        for game in self.db.execute('SELECT g.* FROM games g JOIN photos p ON p.id=g.photo_id WHERE g.active=1 ORDER BY g.occurred_at,p.message_id,g.ordinal,g.id').fetchall():
            a, b = values[game['a']], values[game['b']]
            na, nb = elo(a[0], b[0], 0 if game['score_a'] > game['score_b'] else 1, float(self.setting('k')))
            self.db.execute('UPDATE games SET rating_a_before=?,rating_b_before=?,rating_a_after=?,rating_b_after=? WHERE id=?',
                            (a[0], b[0], na, nb, game['id']))
            a[0], b[0] = na, nb
            a[1 if game['score_a'] > game['score_b'] else 2] += 1
            b[2 if game['score_a'] > game['score_b'] else 1] += 1
        self.db.executemany('INSERT INTO ratings VALUES (?,?,?,?)', [(pid, *val) for pid, val in values.items()])

    def amend(self, gid, actor, is_admin, games=None):
        if not is_admin:
            raise ValueError('Команда доступна только администраторам TT')
        g = self.db.execute('SELECT * FROM games WHERE id=?', (gid,)).fetchone()
        if g is None:
            raise ValueError('Партия не найдена')
        if games is None:
            if not g['active']:
                return False
            self.db.execute('UPDATE games SET active=0 WHERE id=?', (gid,))
            action, after = 'undo', {'active': 0}
        else:
            a, b, sa, sb = self.validate_games([games])[0]
            self.db.execute('UPDATE games SET a=?,b=?,score_a=?,score_b=?,active=1 WHERE id=?', (a, b, sa, sb, gid))
            action, after = 'edit', [a, b, sa, sb]
        self.log(actor, action, str(gid), dict(g), after)
        self.recalculate()
        return True

    def add_alias(self, player_name, alias, actor):
        pid = self.player(player_name)
        existing = self.db.execute('SELECT player_id FROM aliases WHERE alias=?', (norm(alias),)).fetchall()
        if existing and any(row[0] != pid for row in existing):
            raise ValueError('Псевдоним уже занят другим игроком; автоматическое объединение запрещено')
        if not alias.strip() or len(alias) > 60:
            raise ValueError('Псевдоним должен содержать 1–60 символов')
        self.db.execute('INSERT OR IGNORE INTO aliases VALUES (?,?)', (norm(alias), pid))
        self.log(actor, 'alias', str(pid), None, {'alias': alias})

    def ingest(self, update):
        with self.transaction():
            self.db.execute('INSERT OR IGNORE INTO jobs(id,payload) VALUES (?,?)',
                            (update['update_id'], json.dumps(update, ensure_ascii=False)))

    def recover(self):
        # One worker process per DB, protected by an OS flock.
        with self.transaction():
            self.db.execute("UPDATE jobs SET status='pending' WHERE status='processing'")

    def claim(self, photos=None):
        with self.transaction():
            photo_filter = '' if photos is None else (
                " AND json_extract(payload,'$.message.photo') IS NOT NULL" if photos else
                " AND json_extract(payload,'$.message.photo') IS NULL")
            row = self.db.execute("SELECT * FROM jobs WHERE status='pending' AND ready_at<=?" + photo_filter + " ORDER BY id LIMIT 1", (time.time(),)).fetchone()
            if row:
                self.db.execute("UPDATE jobs SET status='processing',attempts=attempts+1 WHERE id=?", (row['id'],))
                return dict(self.db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone())

    def retry(self, jid, error):
        with self.transaction():
            row = self.db.execute('SELECT attempts FROM jobs WHERE id=?', (jid,)).fetchone()
            self.db.execute('UPDATE jobs SET status=?,ready_at=?,last_error=? WHERE id=?',
                            ('failed' if row[0] >= 3 else 'pending', time.time() + 10 * 2 ** row[0], error[:120], jid))

    def send(self, dedupe, method, payload):
        self.db.execute('INSERT OR IGNORE INTO outbox(dedupe,method,payload) VALUES (?,?,?)',
                        (dedupe, method, json.dumps(payload, ensure_ascii=False)))

    def backup(self, target):
        with sqlite3.connect(target) as out:
            self.db.backup(out)
