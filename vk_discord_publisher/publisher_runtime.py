"""VK -> Discord, manual import v1. No Gateway, slash commands or scheduler yet."""
import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import getpass
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
import time

import requests
import download_album as downloader

ROOT = Path(__file__).resolve().parent
API = 'https://discord.com/api/v10'


class AppError(RuntimeError):
    pass


class ApiError(AppError):
    def __init__(self, status, code=None):
        self.status, self.code = status, code
        hints = {401: 'Токен не принят. Запустите SETUP.cmd и введите актуальный токен.',
                 403: 'Нет доступа или нужного права в канале/ветке.',
                 404: 'Канал, ветка или сообщение не найдены либо недоступны.',
                 413: 'Discord отклонил размер вложения. Исходник не уменьшен.'}
        super().__init__(f'Discord HTTP {status}, code={code}. ' + hints.get(status, 'Проверьте настройки и права.'))


class UnknownResult(AppError):
    pass


def wait(seconds):
    seconds = max(0.0, float(seconds))
    if seconds > 1:
        print(f'Ожидание {seconds:.1f} с...', flush=True)
    while seconds > 0:
        step = min(seconds, 30)
        time.sleep(step)
        seconds -= step


def protect(data, decrypt=False):
    if os.name != 'nt':
        raise AppError('Сохранение токена рассчитано на Windows. Используйте DISCORD_BOT_TOKEN в окружении.')
    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise AppError('Windows не смог обработать сохранённый токен. Повторите SETUP.cmd под своей учётной записью.')
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))


def read_token(root=ROOT):
    token = os.environ.get('DISCORD_BOT_TOKEN')
    if not token:
        path = root / 'token.dpapi'
        if path.exists():
            try:
                token = protect(path.read_bytes(), decrypt=True).decode('utf-8')
            except AppError:
                print('Сохранённый токен нельзя расшифровать в этой учётной записи. Введите его локально.')
        if not token:
            token = getpass.getpass('Токен Discord-бота (ввод скрыт, только на этот запуск): ')
    token = token.strip().removeprefix('Bot ').strip()
    if not token or not token.isascii() or any(c.isspace() for c in token):
        raise AppError('Некорректный токен. Повторите SETUP.cmd.')
    return token


class Discord:
    def __init__(self, token):
        self.session = requests.Session()
        self.session.headers.update({'Authorization': 'Bot ' + token,
                                     'User-Agent': 'VKAlbumPublisher (local personal tool, 1.0)'})
        self.bot_id = None
        self.ready_at = 0.0

    def close(self):
        self.session.close()

    def request(self, method, route, payload=None, attachment=None, params=None):
        if not route.startswith('/'):
            raise AppError('Некорректный маршрут API.')
        for attempt in range(5):
            wait(self.ready_at - time.monotonic())
            kwargs = {'timeout': (20, 120), 'allow_redirects': False, 'params': params}
            stream = None
            try:
                if attachment:
                    stream = Path(attachment).open('rb')
                    kwargs['data'] = {'payload_json': json.dumps(payload, ensure_ascii=False)}
                    kwargs['files'] = {'files[0]': (Path(attachment).name, stream,
                                                   mimetypes.guess_type(str(attachment))[0] or 'application/octet-stream')}
                elif payload is not None:
                    kwargs['json'] = payload
                response = self.session.request(method, API + route, **kwargs)
            except requests.RequestException:
                if method == 'POST':
                    raise UnknownResult('Не получен ответ Discord на отправку. Повторите PUBLISH_ALBUM.cmd: сначала будет сверена история.') from None
                if attempt == 4:
                    raise AppError('Discord недоступен. Проверьте подключение и повторите запуск.') from None
                wait(2 ** attempt)
                continue
            finally:
                if stream:
                    stream.close()
            try:
                body = response.json() if response.content else {}
            except ValueError:
                body = {}
            if response.status_code == 429:
                delay = float(body.get('retry_after', response.headers.get('Retry-After', 5)))
                self.ready_at = time.monotonic() + max(delay, 1) + 0.2
                continue
            if response.headers.get('X-RateLimit-Remaining') == '0':
                self.ready_at = time.monotonic() + float(response.headers.get('X-RateLimit-Reset-After', 1)) + 0.2
            if response.status_code >= 500:
                if method == 'POST':
                    raise UnknownResult('Discord вернул ошибку сервера после отправки. Повторный запуск сначала сверит историю.')
                if attempt < 4:
                    wait(2 ** attempt)
                    continue
            if not 200 <= response.status_code < 300:
                raise ApiError(response.status_code, body.get('code') if isinstance(body, dict) else None)
            return body
        raise AppError('Лимит запросов Discord не снят после нескольких попыток. Повторите запуск позже.')

    def messages(self, channel):
        before = None
        for _ in range(100):
            params = {'limit': 100}
            if before:
                params['before'] = before
            batch = self.request('GET', f'/channels/{channel}/messages', params=params)
            for message in batch:
                yield message
            if len(batch) < 100:
                return
            before = batch[-1]['id']
        raise AppError('История превышает 10000 сообщений. Используйте отдельный канал для глав; автоматическая повторная отправка остановлена.')

    def post_message(self, channel, content, identity, attachment=None):
        payload = {'content': content, 'allowed_mentions': {'parse': []},
                   'nonce': hashlib.sha256(identity.encode()).hexdigest()[:24], 'enforce_nonce': True,
                   'flags': 4096}
        if attachment:
            payload['attachments'] = [{'id': 0, 'filename': Path(attachment).name}]
        return self.request('POST', f'/channels/{channel}/messages', payload, attachment)


def permissions(guild, member, roles, overwrites, bot_id):
    if str(guild.get('owner_id')) == str(bot_id):
        return (1 << 64) - 1
    role_ids = {str(x) for x in member['roles']} | {str(guild['id'])}
    value = 0
    for role in roles:
        if str(role['id']) in role_ids:
            value |= int(role['permissions'])
    if value & (1 << 3):
        return (1 << 64) - 1
    def apply(current, items):
        allow = deny = 0
        for item in items:
            allow |= int(item.get('allow', 0))
            deny |= int(item.get('deny', 0))
        return (current & ~deny) | allow
    value = apply(value, [x for x in overwrites if x['type'] == 0 and str(x['id']) == str(guild['id'])])
    value = apply(value, [x for x in overwrites if x['type'] == 0 and str(x['id']) in role_ids and str(x['id']) != str(guild['id'])])
    return apply(value, [x for x in overwrites if x['type'] == 1 and str(x['id']) == str(bot_id)])


def check(api, config):
    me = api.request('GET', '/users/@me')
    if not me.get('bot'):
        raise AppError('Нужен токен бота Discord.')
    api.bot_id = str(me['id'])
    channel_id, guild_id = str(config['channel_id']), str(config['guild_id'])
    if not channel_id.isdigit() or not guild_id.isdigit():
        raise AppError('ID сервера и канала должны состоять из цифр.')
    channel = api.request('GET', f'/channels/{channel_id}')
    if str(channel.get('guild_id')) != guild_id:
        raise AppError('Канал относится к другому серверу. Исправьте настройки через SETUP.cmd.')
    if channel.get('type') != 0:
        raise AppError('В этой версии нужен обычный текстовый канал сервера, не форум, категория или готовая ветка.')
    guild = api.request('GET', f'/guilds/{guild_id}')
    roles = api.request('GET', f'/guilds/{guild_id}/roles')
    member = api.request('GET', f'/guilds/{guild_id}/members/{api.bot_id}')
    bits = permissions(guild, member, roles, channel.get('permission_overwrites', []), api.bot_id)
    required = {10: 'View Channel', 11: 'Send Messages', 15: 'Attach Files',
                16: 'Read Message History', 35: 'Create Public Threads', 38: 'Send Messages in Threads'}
    missing = [name for bit, name in required.items() if not bits & (1 << bit)]
    if missing:
        raise AppError('Не хватает прав в выбранном канале: ' + ', '.join(missing))
    api.request('GET', f'/channels/{channel_id}/messages', params={'limit': 1})
    print(f"Бот: {me['username']}; сервер: {guild['name']}; канал: #{channel['name']}")
    print('Подключение и основные права проверены. Сообщения при проверке не отправляются.')
    return me, channel


def setup(root=ROOT):
    old = json.loads((root / 'config.json').read_text(encoding='utf-8')) if (root / 'config.json').exists() else {}
    print('Настройка. Данные вводятся только на этом компьютере.')
    token = getpass.getpass('Токен Discord-бота (ввод скрыт; пусто = сохранённый): ').strip()
    if not token:
        token = read_token(root)
    token = token.removeprefix('Bot ').strip()
    guild = input(f"ID сервера [{old.get('guild_id', '')}]: ").strip() or old.get('guild_id', '')
    channel = input(f"ID текстового канала [{old.get('channel_id', '')}]: ").strip() or old.get('channel_id', '')
    config = {**old, 'guild_id': guild, 'channel_id': channel, 'max_file_mib': old.get('max_file_mib', 8)}
    api = Discord(token)
    try:
        check(api, config)
    finally:
        api.close()
    try:
        encrypted = protect(token.encode('utf-8'))
    except AppError:
        (root / 'token.dpapi').unlink(missing_ok=True)
        print('Шифрование Windows недоступно. Токен не сохранён; при запуске его потребуется ввести снова.')
    else:
        temporary = root / 'token.dpapi.tmp'
        temporary.write_bytes(encrypted)
        temporary.replace(root / 'token.dpapi')
    downloader.atomic_json(root / 'config.json', config)
    print('Настройки сохранены. Для первой главы запустите PUBLISH_ALBUM.cmd.')


@contextmanager
def exclusive(root=ROOT):
    import msvcrt
    with (root / 'publisher.lock').open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise AppError('Программа уже запущена из этой папки. Дождитесь завершения.') from None
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def local_album(folder, config):
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    if not re.fullmatch(r'-?\d+_\d+', str(manifest.get('album', ''))) or downloader.album_key(manifest.get('source', '')) != manifest['album']:
        raise AppError('Некорректная ссылка или идентификатор альбома в манифесте.')
    count = len(manifest['photo_ids'])
    if not count:
        raise AppError('Альбом пуст.')
    files = []
    for number in range(1, count + 1):
        item = manifest['files'].get(str(number))
        if not item or not re.fullmatch(r'\d{4,}\.(jpg|png|webp|gif|avif)', item.get('name', '')):
            raise AppError('Неполный или некорректный манифест. Повторите скачивание.')
        path = folder / item['name']
        if not path.is_file() or downloader.checksum(path) != item['sha256']:
            raise AppError(f'Отсутствует или повреждён {path.name}. Повторите скачивание.')
        downloader.inspect_image(path)
        if path.stat().st_size > float(config.get('max_file_mib', 8)) * 1024 * 1024:
            raise AppError(f'{path.name} превышает настроенный предел размера. См. README.txt; изображение не уменьшено.')
        files.append(path)
    fingerprint = hashlib.sha256(json.dumps([manifest['photo_ids'], [downloader.checksum(x) for x in files]], ensure_ascii=False).encode()).hexdigest()
    return manifest, files, fingerprint


def own_messages(api, channel):
    return [m for m in api.messages(channel) if str(m.get('author', {}).get('id')) == api.bot_id]

