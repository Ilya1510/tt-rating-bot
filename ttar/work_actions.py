"""Owner-only data changes proposed by Codex and committed by the controller."""
import hashlib
import json
import time

from .operations import OWNER_ID
from .core import valid_score


ACTION_SCHEMA = {'type': 'object', 'additionalProperties': False,
    'properties': {
        'kind': {'type': 'string', 'enum': ['edit_game', 'cancel_photo']},
        'photo_id': {'type': 'integer', 'minimum': 1},
        'game_number': {'type': ['integer', 'null'], 'minimum': 1},
        'score_a': {'type': ['integer', 'null'], 'minimum': 0, 'maximum': 200},
        'score_b': {'type': ['integer', 'null'], 'minimum': 0, 'maximum': 200}},
    'required': ['kind', 'photo_id', 'game_number', 'score_a', 'score_b']}


def photo_state(store, photo):
    state = {key: photo[key] for key in
             ('id', 'message_id', 'occurred_at', 'status', 'revision')}
    state['games'] = [dict(r) for r in store.db.execute(
        'SELECT id,ordinal+1 AS number,a,b,score_a,score_b,active FROM games '
        'WHERE photo_id=? ORDER BY ordinal', (photo['id'],))]
    state['proposal'] = json.loads(photo['proposal'])
    if photo['status'] == 'draft':
        state['games'] = [dict(id=None, number=i, a=a, b=b, score_a=sa, score_b=sb, active=1)
                          for i, (a, b, sa, sb) in enumerate(state['proposal'], 1)]
    state['token'] = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
    return state


def complete_data_action(store, op, result, snapshot, allowed_chat):
    """Mutation, audit, card refresh and final reply share one transaction."""
    from jsonschema import validate, ValidationError
    action = result.get('action')
    with store.transaction():
        current = store.db.execute('SELECT * FROM operations WHERE id=?', (op['id'],)).fetchone()
        if not current or current['kind'] != 'work':
            raise ValueError('Задача /work не найдена.')
        if current['status'] == 'done':
            return  # Recovery/repeated dispatch cannot apply an action twice.
        store.db.execute('SAVEPOINT data_action')
        try:
            if current['actor'] != OWNER_ID or current['chat_id'] != allowed_chat:
                raise ValueError('Исправлять партии и отменять фото может только Илья.')
            if current['status'] != 'running':
                raise ValueError('Задача уже завершена; повторное изменение не выполнено.')
            validate(action, ACTION_SCHEMA)
            photo = store.photo(action['photo_id'])
            expected = next((p for p in snapshot.get('photos', []) if p['id'] == photo['id']), None)
            if photo['chat_id'] != allowed_chat or not expected:
                raise ValueError('Этого фото нет в снимке нашей группы. Уточни фотографию.')
            if photo_state(store, photo)['token'] != expected['token']:
                raise ValueError('Фото изменилось, пока выполнялась задача. Повтори запрос по актуальному списку.')
            if photo['status'] not in ('draft', 'confirmed'):
                raise ValueError('Эта фотография уже отменена.')
            if action['kind'] == 'cancel_photo':
                if any(action[k] is not None for k in ('game_number', 'score_a', 'score_b')):
                    raise ValueError('Некорректная команда отмены фотографии.')
                count = store.db.execute('SELECT count(*) FROM games WHERE photo_id=? AND active=1',
                                         (photo['id'],)).fetchone()[0]
                store.db.execute('UPDATE games SET active=0 WHERE photo_id=?', (photo['id'],))
                store.db.execute("UPDATE photos SET status='cancelled',revision=revision+1 WHERE id=?", (photo['id'],))
                summary = (f"Фото #{photo['id']} отменено. Исключено партий: {count}. Elo и статистика пересчитаны."
                           if photo['status'] == 'confirmed' else
                           f"Черновик фото #{photo['id']} отменён. Рейтинг не изменён.")
            else:
                number = action['game_number']
                game = next((g for g in expected['games'] if g['number'] == number and g['active']), None)
                if not game:
                    raise ValueError('Не нашёл активную партию с таким номером на фото.')
                sa, sb = action['score_a'], action['score_b']
                valid_score(sa, sb, store.setting('unit'))
                proposal = json.loads(photo['proposal'])
                proposal[number - 1] = [game['a'], game['b'], sa, sb]
                store.db.execute('UPDATE photos SET proposal=? WHERE id=?',
                                 (json.dumps(proposal), photo['id']))
                if photo['status'] == 'confirmed':
                    store.db.execute('UPDATE games SET score_a=?,score_b=? WHERE id=?', (sa, sb, game['id']))
                else:
                    store.db.execute('UPDATE photos SET revision=revision+1 WHERE id=?', (photo['id'],))
                    store.db.execute('DELETE FROM confirm_votes WHERE photo_id=?', (photo['id'],))
                    # Reuse the existing messages with fresh confirmation buttons.
                    store.db.execute('UPDATE photo_cards SET revision=? WHERE photo_id=?',
                                     (photo['revision'] + 1, photo['id']))
                summary = (f"Фото #{photo['id']}, партия №{number}: "
                           f"{store.name(game['a'])} — {store.name(game['b'])} {sa}:{sb}. "
                           + ('Elo и статистика пересчитаны.' if photo['status'] == 'confirmed' else
                              'Черновик исправлен; нужны два новых подтверждения.'))
            store.recalculate()
            store.log(current['actor'], 'work_data_action', str(current['id']), expected,
                      {'action': action, 'photo': photo_state(store, store.photo(photo['id']))})
            for row in store.db.execute('SELECT id FROM photos WHERE chat_id=?', (allowed_chat,)).fetchall():
                store.update_cards(row['id'], f"work-action:{current['id']}")
            status = 'done'
        except (ValueError, ValidationError) as error:
            store.db.execute('ROLLBACK TO data_action')
            status = 'failed'
            summary = 'Некорректная команда изменения данных.' if isinstance(error, ValidationError) else str(error)
        store.db.execute('RELEASE data_action')
        stored = {'status': status, 'summary': summary, 'action': action, 'commit': None}
        store.db.execute('UPDATE operations SET status=?,finished_at=?,result=? WHERE id=?',
                         (status, time.time(), json.dumps(stored, ensure_ascii=False), current['id']))
        store.send(f"operation-result:{current['id']}", 'sendMessage',
                   {'chat_id': current['chat_id'], 'text': f"/work #{current['id']}: {summary}"})
    return stored
