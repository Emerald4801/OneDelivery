"""Read-only official VK photos API. Secrets and server error bodies are never logged."""
import os
from pathlib import Path
import time

import requests
import publisher_runtime as runtime
from failure_policy import should_retry, failure_message

METHODS = {'photos.getAlbums', 'photos.get'}


class VKAPIError(RuntimeError):
    def __init__(self, text, code=None, *, retryable=False):
        self.code = code
        self.retryable = retryable
        super().__init__(text)


def validate_token(token):
    token = token.strip()
    if not token or not token.isascii() or any(c.isspace() for c in token):
        raise VKAPIError('Некорректный ключ VK API.')
    return token


def read_token(root):
    value = os.environ.get('VK_API_TOKEN')
    if value:
        return validate_token(value)
    path = Path(root) / 'vk_api_token.dpapi'
    if not path.exists():
        return None
    try:
        return validate_token(runtime.protect(path.read_bytes(), decrypt=True).decode('utf-8'))
    except (runtime.AppError, UnicodeError):
        raise VKAPIError('Windows не смог прочитать сохранённый ключ VK API. Выполните PROBE_VK_API.cmd под своей учётной записью.') from None


def save_token(root, token):
    path = Path(root) / 'vk_api_token.dpapi'
    temporary = path.with_suffix('.tmp')
    try:
        encrypted = runtime.protect(validate_token(token).encode('utf-8'))
    except runtime.AppError:
        raise VKAPIError('Windows не смог сохранить ключ VK API под этой учётной записью. Проверка API завершена, ключ на диск не записан.') from None
    temporary.write_bytes(encrypted)
    temporary.replace(path)


def largest_image(photo):
    candidates = list(photo.get('sizes') or [])
    if isinstance(photo.get('orig_photo'), dict):
        candidates.append(photo['orig_photo'])
    valid = [p for p in candidates if isinstance(p, dict) and
             isinstance(p.get('url'), str) and p['url'].startswith('https://') and
             type(p.get('width')) is int and type(p.get('height')) is int and
             p['width'] > 0 and p['height'] > 0]
    if not valid:
        raise VKAPIError('API не вернул доступный размер фотографии.')
    return max(valid, key=lambda p: p['width'] * p['height'])


class Client:
    def __init__(self, token, session=None):
        self.token = validate_token(token)
        self.session = session or requests.Session()
        self.owns_session = session is None
        self.ready_at = 0

    def close(self):
        if self.owns_session:
            self.session.close()

    def call(self, method, parameters):
        if method not in METHODS:
            raise VKAPIError('Разрешены только методы чтения альбомов и фотографий.')
        for attempt in range(4):
            delay = self.ready_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.ready_at = time.monotonic() + 0.4
            try:
                response = self.session.post('https://api.vk.com/method/' + method,
                    data={**parameters, 'access_token': self.token, 'v': '5.199'},
                    timeout=(10, 30), allow_redirects=False)
                if getattr(response, 'is_redirect', False):
                    raise VKAPIError('API VK перенаправил запрос; ключ не отправлен по новому адресу.')
                response.raise_for_status()
                payload = response.json()
            except requests.RequestException as exc:
                transient = should_retry(exc)
                if transient and attempt < 3:
                    self.ready_at = time.monotonic() + 2 ** attempt
                    continue
                raise VKAPIError('API VK: ' + failure_message(exc), retryable=transient) from None
            except ValueError:
                raise VKAPIError('API VK вернул нечитаемый ответ.') from None
            if not isinstance(payload, dict):
                raise VKAPIError('Неожиданный формат ответа API VK.')
            if 'error' in payload:
                error = payload['error']
                code = error.get('error_code') if isinstance(error, dict) else None
                code = code if type(code) is int else None
                if code == 6 and attempt < 3:
                    self.ready_at = time.monotonic() + 2 ** attempt
                    continue
                hints = {5: 'Ключ не принят или истёк.', 6: 'Ограничение частоты запросов.',
                         9: 'VK ограничил обращения; частые повторы остановлены.',
                         14: 'VK требует проверку пользователя.', 15: 'Нет доступа к этим данным.',
                         27: 'Метод недоступен ключу сообщества.',
                         28: 'Метод недоступен сервисному ключу.',
                         100: 'VK не принял параметры запроса.',
                         1051: 'VK отклонил этот метод для данного доступа/приложения.'}
                raise VKAPIError(f'{method}: ошибка VK API {code}. ' + hints.get(code, 'Доступ к методу не подтверждён.'), code, retryable=code == 6)
            body = payload.get('response')
            if (not isinstance(body, dict) or type(body.get('count')) is not int or body['count'] < 0
                    or not isinstance(body.get('items'), list)):
                raise VKAPIError('API VK не вернул ожидаемый список объектов.')
            return body
        raise VKAPIError('API VK продолжает ограничивать запросы.', retryable=True)

    def albums(self, owner, full=False):
        output, offset, seen = [], 0, set()
        for _ in range(100):
            body = self.call('photos.getAlbums', {'owner_id': owner, 'need_system': 0, 'count': 100, 'offset': offset})
            items = body['items']
            for item in items:
                if (not isinstance(item, dict) or str(item.get('owner_id')) != str(owner)
                        or type(item.get('id')) is not int or type(item.get('size')) is not int or item['size'] < 0):
                    raise VKAPIError('API вернул некорректные сведения об альбоме.')
                if item['id'] in seen:
                    raise VKAPIError('Список альбомов изменился во время чтения. Нужна повторная проверка.', retryable=True)
                seen.add(item['id'])
                output.append(item)
            offset += len(items)
            if offset >= body['count'] or not full:
                return output
            if not items:
                raise VKAPIError('API вернул неполный список альбомов.', retryable=True)
        raise VKAPIError('Превышен предел обхода альбомов.')

    def album(self, owner, ident):
        body = self.call('photos.getAlbums', {'owner_id': owner, 'album_ids': str(ident), 'need_system': 0})
        matches = [p for p in body['items'] if str(p.get('id')) == str(ident) and str(p.get('owner_id')) == str(owner)]
        if len(matches) != 1 or type(matches[0].get('size')) is not int:
            raise VKAPIError('Нужный альбом не найден или недоступен через API.')
        return matches[0]

    def photos(self, owner, ident, rev=0):
        output, seen, offset, expected_count = [], set(), 0, None
        for _ in range(10):
            body = self.call('photos.get', {'owner_id': owner, 'album_id': str(ident), 'rev': rev,
                                         'photo_sizes': 1, 'count': 1000, 'offset': offset})
            if expected_count is not None and body['count'] != expected_count:
                raise VKAPIError('Число фотографий изменилось во время чтения API.', retryable=True)
            expected_count = body['count']
            for photo in body['items']:
                if (not isinstance(photo, dict) or str(photo.get('owner_id')) != str(owner)
                        or str(photo.get('album_id')) != str(ident) or type(photo.get('id')) is not int):
                    raise VKAPIError('API вернул фотографию из другого альбома или некорректный объект.')
                if photo['id'] in seen:
                    raise VKAPIError('API вернул повтор фотографии; порядок или полнота не подтверждены.')
                seen.add(photo['id'])
                image = largest_image(photo)
                output.append({'id': str(photo['id']), 'url': image['url'],
                               'width': image['width'], 'height': image['height']})
            offset += len(body['items'])
            if offset == expected_count:
                return output
            if not body['items'] or offset > expected_count:
                raise VKAPIError('API вернул неполный или противоречивый список фотографий.', retryable=True)
        raise VKAPIError('Слишком большой альбом для публикации главы.')
