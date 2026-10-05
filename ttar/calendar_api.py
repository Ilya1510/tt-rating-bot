"""Small, conservative client for the public Yandex Calendar API.

No automatic retries of writes: a lost create response may hide a real event.
The caller persists an attempt BEFORE create and an event id in save_event.
After an uncertain result it must only reconcile; absence is not retry permission.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

BASE_URL = 'https://cloud-api.yandex.net/v1/calendar'
ROOM = 'conf_mm_5_15@yandex-team.ru'
TIMEZONE = 'Europe/Moscow'
SUMMARY = 'Настольный теннис'
PLAYERS = ('klim-roma@yandex-team.ru', 'polyanskiy-mn@yandex-team.ru')


@dataclass(frozen=True)
class BookingResult:
    status: str
    event_id: str | None = None
    url: str | None = None
    reason: str = ''


class CalendarError(RuntimeError):
    """Contains only a numeric status and a fixed safe explanation."""
    def __init__(self, status=0):
        self.status = status
        messages = {401: 'Календарь: токен недействителен.',
                    403: 'Календарь: недостаточно прав.',
                    404: 'Событие или ресурс не найден.',
                    409: 'Календарь: конфликт.',
                    429: 'Календарь: слишком много запросов.'}
        super().__init__(messages.get(status, f'Календарь: ошибка HTTP {status}.' if status else
                                         'Календарь: не удалось получить ответ.'))


def event_url(event_id):
    return 'https://calendar.yandex-team.ru/event/' + urllib.parse.quote(str(event_id), safe='')


def _date(value):
    if isinstance(value, dict):
        raw = value.get('date_time') or value.get('date')
        if not raw:
            raise ValueError('Неизвестный формат времени.')
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo(value.get('time_zone', TIMEZONE)))
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('У времени должно быть смещение UTC.')
    return parsed


def _slot(start, end):
    first, last = _date(start), _date(end)
    if not timedelta(0) < last - first <= timedelta(minutes=150):
        raise ValueError('Длительность брони должна быть от 1 до 150 минут.')
    return first, last


def _edt(value):
    return {'date_time': _date(value).astimezone(ZoneInfo(TIMEZONE)).strftime('%Y-%m-%dT%H:%M:%S'),
            'time_zone': TIMEZONE}


def _marker(key):
    # Stable, short, and safe even when the human-supplied key contains newlines.
    return 'TTAR booking:' + hashlib.sha256(str(key).encode()).hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        # Never forward the OAuth header to a redirect target.
        raise CalendarError(code)


class CalendarClient:
    def __init__(self, token=None, timeout=20, transport=None, ownership_lookup=None):
        self._token = token or os.environ.get('YANDEX_CALENDAR_TOKEN')
        if not self._token and transport is None:
            raise CalendarError(401)
        self.timeout = timeout
        self.transport = transport
        self.ownership_lookup = ownership_lookup

    def _call(self, method, path, body=None, query=None):
        if self.transport is not None:
            return self.transport(method, path, body, query)
        url = BASE_URL + path
        if query:
            url += '?' + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'OAuth ' + self._token,
                     'Content-Type': 'application/json', 'Accept': 'application/json'})
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=self.timeout) as response:
                raw = response.read()
                result = json.loads(raw) if raw else {}
                if not isinstance(result, dict):
                    raise CalendarError()
                return result
        except urllib.error.HTTPError as error:
            raise CalendarError(error.code) from None
        except (OSError, ValueError):
            raise CalendarError() from None

    @staticmethod
    def _path(event_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(event_id)):
            raise ValueError('Некорректный ID встречи.')
        return '/events/' + str(event_id)

    def _items(self, method, path, body=None, query=None):
        result, cursor, seen = [], None, set()
        for _ in range(100):
            params = dict(query or {}, limit=100)
            if cursor:
                params['iteration_key'] = cursor
            page = self._call(method, path, body, params)
            if not isinstance(page.get('items'), list):
                raise CalendarError()
            result.extend(page['items'])
            cursor = page.get('iteration_key')
            if not cursor:
                return result
            if cursor in seen:
                break
            seen.add(cursor)
        # Never infer absence or freedom from an incomplete result.
        raise CalendarError()

    def get_event(self, event_id):
        return self._call('GET', self._path(event_id))

    def participants(self, event_id):
        return self._items('GET', self._path(event_id) + '/participants')

    def _invite_players(self, event_id):
        # The create endpoint may ignore participants. Use the dedicated endpoint
        # once, after persisting the event ID; verification never retries writes.
        present = {p.get('email', '').casefold() for p in self.participants(event_id)}
        missing = [email for email in PLAYERS if email not in present]
        if missing:
            self._call('POST', self._path(event_id) + '/participants',
                       {'items': [{'email': email, 'participation_type': 'ATTENDEE'} for email in missing]})
            present = {p.get('email', '').casefold() for p in self.participants(event_id)}
            if not set(PLAYERS) <= present:
                raise CalendarError()

    def _owned_event(self, event, booking_key):
        # Legacy events may still carry a marker; new ones use the durable ID
        # binding in SQLite and keep their description free of technical text.
        return bool((self.ownership_lookup and
                     self.ownership_lookup(event.get('event_id'), booking_key)) or
                    _marker(booking_key) in str(event.get('description', '')).splitlines())

    def list_events(self, start, end):
        first, last = _slot(start, end)
        return self._items('GET', '/events', query={'from': first.isoformat(), 'to': last.isoformat()})

    def free_busy(self, room, start, end):
        first, last = _slot(start, end)
        return self._items('POST', '/free-busy/users/search',
                           {'user_email': room, 'from': first.isoformat(), 'to': last.isoformat()})

    @staticmethod
    def _same_slot(event, start, end):
        try:
            return _date(event['start']) == _date(start) and _date(event['end']) == _date(end)
        except (ValueError, KeyError, TypeError):
            return False

    def _find_existing(self, start, end, booking_key):
        found = []
        for event in self.list_events(start, end):
            if not self._owned_event(event, booking_key):
                continue
            if not self._same_slot(event, start, end) or event.get('relation_type') != 'ORGANIZER' or event.get('repetition'):
                raise ValueError('Существующая встреча не соответствует этой заявке.')
            found.append(event)
        if len(found) > 1:
            raise ValueError('Найдено несколько встреч этой заявки; автоматические изменения остановлены.')
        return found[0] if found else None

    def reconcile_booking(self, start, end, booking_key):
        _slot(start, end)
        try:
            event = self._find_existing(start, end, booking_key)
            if not event:
                return BookingResult('not_found', reason='Встреча пока не найдена. Повторное создание автоматически запрещено.')
            return self.verify_booking(event['event_id'], start, end)
        except CalendarError as error:
            return BookingResult('unverifiable', reason=str(error))

    def create_booking(self, start, end, booking_key, save_event):
        """Create once; save_event(event_id) MUST commit before returning.

        Caller must durably record a started attempt before invoking this method,
        and must not call it again after uncertain outcome (use reconcile_booking).
        """
        _slot(start, end)
        try:
            old = self._find_existing(start, end, booking_key)
            if old:
                save_event(old['event_id'])
                return self.verify_booking(old['event_id'], start, end)
            for interval in self.free_busy(ROOM, start, end):
                if _date(interval['start']) < _date(end) and _date(interval['end']) > _date(start):
                    return BookingResult('conflict', reason='Зал уже занят в это время.')
        except CalendarError as error:
            return BookingResult('unverifiable', reason=str(error))
        body = {'summary': SUMMARY, 'start': _edt(start), 'end': _edt(end),
                'description': '', 'location': ROOM,
                'participants': [{'email': ROOM, 'participation_type': 'ATTENDEE'}],
                'personal_settings': {'availability': 'BUSY'}}
        try:
            event = self._call('POST', '/events', body)
        except CalendarError as error:
            # Treat even server HTTP errors as possibly committed; no blind retry.
            uncertain = not error.status or error.status >= 500
            return BookingResult('uncertain' if uncertain else 'rejected', reason=str(error))
        event_id = event.get('event_id')
        if not event_id:
            return BookingResult('uncertain', reason='Календарь не вернул ID. Повторное создание запрещено; нужна сверка.')
        # Persistence failure must escape: the caller's started attempt remains.
        save_event(event_id)
        try:
            self._invite_players(event_id)
        except CalendarError:
            return BookingResult('unverifiable', event_id, event_url(event_id),
                                 'Встреча создана, но приглашения Роме и Максиму пока не подтверждены API.')
        return self.verify_booking(event_id, start, end)

    def verify_booking(self, event_id, start, end):
        _slot(start, end)
        url = event_url(event_id)
        try:
            event = self.get_event(event_id)
            if not self._same_slot(event, start, end):
                return BookingResult('unverifiable', event_id, url, 'Время встречи отличается от заявки.')
            people = self.participants(event_id)
            resources = [p for p in people if p.get('email', '').casefold() == ROOM.casefold()]
            if len(resources) != 1:
                return BookingResult('unverifiable', event_id, url, 'Календарь не показывает участие ресурса зала.')
            decision = resources[0].get('decision')
            if decision == 'DECLINED':
                return BookingResult('rejected', event_id, url, 'Зал отклонил приглашение.')
            if decision in ('NEEDS_ACTION', 'TENTATIVE'):
                return BookingResult('pending', event_id, url, 'Ожидается подтверждение зала.')
            if decision != 'ACCEPTED':
                return BookingResult('unverifiable', event_id, url, 'Неизвестный ответ зала.')
            busy = self.free_busy(ROOM, start, end)
            if any(b.get('event_id') == event_id and _date(b['start']) <= _date(start)
                   and _date(b['end']) >= _date(end) for b in busy):
                return BookingResult('accepted', event_id, url, 'Зал принял приглашение; занятость подтверждена.')
            return BookingResult('unverifiable', event_id, url, 'Зал принял приглашение, но занятость этой встречей не подтверждена.')
        except CalendarError as error:
            return BookingResult('unverifiable', event_id, url, str(error))
        except (KeyError, TypeError):
            return BookingResult('unverifiable', event_id, url, 'Календарь вернул неполные данные.')

    def cancel_booking(self, event_id, booking_key, start, end):
        _slot(start, end)
        url = event_url(event_id)
        try:
            try:
                event = self.get_event(event_id)
            except CalendarError as error:
                if error.status == 404:
                    return BookingResult('cancelled', event_id, url, 'Встреча уже отсутствует.')
                raise
            if (event.get('relation_type') != 'ORGANIZER' or event.get('repetition')
                    or not self._owned_event(event, booking_key)
                    or not self._same_slot(event, start, end)):
                raise ValueError('Отмена разрешена только для собственной одиночной встречи этой заявки.')
            self._call('DELETE', self._path(event_id))
            # A successful delete response alone is not proof; verify absence.
            try:
                self.get_event(event_id)
            except CalendarError as error:
                if error.status == 404:
                    return BookingResult('cancelled', event_id, url, 'Бронь отменена.')
                raise
            return BookingResult('pending', event_id, url, 'Отмена отправлена, встреча пока видна в календаре.')
        except CalendarError as error:
            return BookingResult('unverifiable', event_id, url, str(error))
        except (KeyError, TypeError):
            return BookingResult('unverifiable', event_id, url, 'Календарь вернул неполные данные.')
