# VK_CARD_STATUS_UPDATE_1
"""Clean Discord chapter cards, spoiler galleries, automatic VK chapter titles."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import mimetypes
from pathlib import Path
import re
import sys
import time

import requests
import download_album as downloader
import publisher_runtime as runtime
from publisher_runtime import AppError, ApiError, UnknownResult, ROOT, API, wait
from album_info import resolve
import publication_state

ARCHIVE = 10080


def files_list(value):
    if value is None:
        return []
    return [Path(p) for p in value] if isinstance(value, (list, tuple)) else [Path(value)]


class Discord(runtime.Discord):
    def request(self, method, route, payload=None, attachment=None, params=None):
        files = files_list(attachment)
        if len(files) > 10:
            raise AppError('В одном сообщении допускается до 10 вложений.')
        for attempt in range(5):
            wait(self.ready_at - time.monotonic())
            kwargs = {'timeout': (20, 180), 'allow_redirects': False, 'params': params}
            try:
                with ExitStack() as stack:
                    if files:
                        metadata = {int(a['id']): a for a in payload.get('attachments', [])}
                        kwargs['data'] = {'payload_json': json.dumps(payload, ensure_ascii=False)}
                        kwargs['files'] = {f'files[{n}]': (
                            metadata[n]['filename'], stack.enter_context(path.open('rb')),
                            mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
                            for n, path in enumerate(files)}
                    elif payload is not None:
                        kwargs['json'] = payload
                    response = self.session.request(method, API + route, **kwargs)
            except requests.RequestException:
                if method == 'POST':
                    raise UnknownResult('Ответ на отправку не получен. Повторный запуск сначала сверит историю.') from None
                if attempt == 4:
                    raise AppError('Discord недоступен. Повторите запуск позже.') from None
                wait(2 ** attempt)
                continue
            try:
                body = response.json() if response.content else {}
            except ValueError:
                body = {}
            if response.status_code == 429:
                delay = body.get('retry_after', response.headers.get('Retry-After', 5)) if isinstance(body, dict) else response.headers.get('Retry-After', 5)
                self.ready_at = time.monotonic() + max(1, float(delay)) + 0.2
                continue
            if response.headers.get('X-RateLimit-Remaining') == '0':
                self.ready_at = time.monotonic() + float(response.headers.get('X-RateLimit-Reset-After', 1)) + 0.2
            if response.status_code >= 500:
                if method == 'POST':
                    raise UnknownResult('Discord вернул ошибку после отправки. Повторите запуск для сверки истории.')
                if attempt < 4:
                    wait(2 ** attempt)
                    continue
            if not 200 <= response.status_code < 300:
                raise ApiError(response.status_code, body.get('code') if isinstance(body, dict) else None)
            return body
        raise AppError('Discord продолжает ограничивать запросы. Повторите запуск позже.')

    @staticmethod
    def payload(content, files=None):
        body = {'content': content, 'allowed_mentions': {'parse': []}, 'embeds': []}
        if files:
            body['attachments'] = [{'id': n, 'filename': 'SPOILER_' + p.name, 'is_spoiler': True}
                                   for n, p in enumerate(files_list(files))]
        return body

    def post_message(self, channel, content, identity, attachment=None):
        body = self.payload(content, attachment)
        body.update(nonce=hashlib.sha256(identity.encode()).hexdigest()[:24], enforce_nonce=True, flags=(4096 if files_list(attachment) else 0) | 4)
        return self.request('POST', f'/channels/{channel}/messages', body, attachment)


def caption(pair):
    return f'Страницы {pair[0]}-{pair[1]}' if pair[0] != pair[1] else f'Страницы {pair[0]}'


def card_text(chapter, count, guild, thread=None, *, loading=False):
    text = f'**ГЛАВА {chapter}**\nСтраниц: {count}'
    if thread:
        label = 'Глава загружается в' if loading else 'Опубликовано в'
        text += f'\n{label}: <#{thread}>'
    return text


def plain_name(name):
    return str(name).removeprefix('SPOILER_')


def matches_files(message, files):
    attachments = message.get('attachments', [])
    return len(attachments) == len(files) and all(
        plain_name(a.get('filename', '')) == p.name and a.get('size') == p.stat().st_size
        for a, p in zip(attachments, files))


def is_spoiler(attachment):
    return bool(attachment.get('flags', 0) & 8 or attachment.get('is_spoiler')
                or str(attachment.get('filename', '')).startswith('SPOILER_'))


def plan_batches(files, config):
    count = int(config.get('batch_size', 6))
    budget = int(float(config.get('max_batch_mib', 24)) * 1024 * 1024)
    if not 1 <= count <= 10 or not 0 < budget <= 24 * 1024 * 1024:
        raise AppError('Некорректные batch_size или max_batch_mib.')
    plan, start, size = [], 1, 0
    for number, path in enumerate(files, 1):
        current = path.stat().st_size
        if current > budget:
            raise AppError('Отдельное изображение превышает размер пачки.')
        if number > start and (number - start >= count or size + current > budget):
            plan.append([start, number - 1])
            start, size = number, 0
        size += current
    plan.append([start, len(files)])
    return plan


def validate_plan(plan, files):
    next_page = 1
    for pair in plan:
        if not isinstance(pair, list) or len(pair) != 2 or any(type(n) is not int for n in pair):
            raise AppError('Повреждён план пачек.')
        a, b = pair
        if a != next_page or not a <= b <= len(files) or b - a + 1 > 10:
            raise AppError('Нарушен порядок пачек.')
        if sum(p.stat().st_size for p in files[a - 1:b]) > 24 * 1024 * 1024:
            raise AppError('Пачка превышает размер запроса.')
        next_page = b + 1
    if next_page != len(files) + 1:
        raise AppError('План не покрывает все изображения.')
    return plan


def old_marker(key, pair, fp, legacy=False):
    return f'[vk-page:{key}:{pair[0]}:{fp[:20]}]' if legacy else f'[vk-group:{key}:{pair[0]}-{pair[1]}:{fp[:20]}]'


def verify_card(api, card, expected, key, fp):
    if str(card.get('author', {}).get('id')) != api.bot_id:
        raise AppError('Карточка принадлежит другому автору.')
    text = card.get('content', '')
    if f'[vk-album:{key}]' in text:
        if f'Версия: {fp[:20]}' not in text:
            raise AppError('В Discord уже есть другая версия альбома.')
    elif not text.startswith(expected):
        raise AppError('Содержимое карточки изменено. Автоматическое обновление остановлено.')


def recover(history, files, plan, recorded, key, fp, legacy=False):
    result = {}
    for index, pair in enumerate(plan, 1):
        ident = recorded.get(str(index))
        if ident:
            matches = [m for m in history if m['id'] == ident]
        else:
            matches = [m for m in history if (old_marker(key, pair, fp, legacy) in m.get('content', '') or
                       m.get('content') == caption(pair)) and matches_files(m, files[pair[0] - 1:pair[1]])]
        if len(matches) > 1:
            raise AppError('Найдено несколько одинаковых сообщений. Нужна ручная проверка.')
        if not matches:
            if ident:
                raise AppError('Ранее отправленное сообщение удалено или недоступно.')
            continue
        message = matches[0]
        text = message.get('content', '')
        if not (old_marker(key, pair, fp, legacy) in text or text == caption(pair)):
            raise AppError('Подпись сообщения изменена. Обновление остановлено.')
        if not matches_files(message, files[pair[0] - 1:pair[1]]):
            raise AppError('Порядок или состав вложений отличается от исходников.')
        result[str(index)] = message['id']
    if list(result) != [str(n) for n in range(1, len(result) + 1)]:
        raise AppError('В середине главы отсутствует пачка. Нельзя вставить её без нарушения порядка.')
    if [int(x) for x in result.values()] != sorted(int(x) for x in result.values()):
        raise AppError('Нарушен порядок сообщений.')
    return result


def activate(api, thread, title, editing):
    meta = thread.get('thread_metadata', {})
    if meta.get('archived') and not editing:
        return
    payload = {'name': title, 'auto_archive_duration': ARCHIVE}
    if meta.get('archived'):
        payload['archived'] = False
    api.request('PATCH', f'/channels/{thread["id"]}', payload)


def publish(api, config, folder, chapter, root=ROOT, *, origin='manual', progress=None):
    manifest, files, fp = runtime.local_album(folder, config)
    key, guild, channel = manifest['album'], str(config['guild_id']), str(config['channel_id'])
    state_path = root / 'state' / guild / channel / (key + '.json')
    state_path.parent.mkdir(parents=True, exist_ok=True)
    existed = state_path.exists()
    state = json.loads(state_path.read_text(encoding='utf-8')) if existed else {
        'album': key, 'fingerprint': fp, 'bot_id': api.bot_id, 'pages': {}}
    if state.get('fingerprint') != fp or state.get('bot_id') != api.bot_id:
        raise AppError('Изменился альбом или бот. Публикация остановлена.')
    if state.get('complete'):
        print('Эта глава уже опубликована. Существующие сообщения не изменены.')
        return
    if state.get('card_id') and state.get('schema') != 3:
        raise AppError('Есть незавершённая публикация прежней версии. Завершите её прежней программой; сообщения не изменены.')
    resuming = bool(state.get('card_id') or state.get('pending_card'))
    def notify(stage):
        if progress is None:
            return
        confirmed = 0
        try:
            for index, pair in enumerate(state.get('group_plan') or [], 1):
                if str(index) not in state.get('groups', {}):
                    break
                confirmed = pair[1]
            progress(dict(chapter=chapter, confirmed=confirmed, total=len(files),
                          resuming=resuming, stage=stage))
        except Exception:
            pass  # A status observer must never change delivery or recovery decisions.
    notify('checking' if resuming else 'preparing')
    title = 'ГЛАВА ' + chapter
    state.update(chapter=chapter, title=title, album_title=manifest.get('album_title'))
    publication_state.begin(state, origin)
    expected = card_text(chapter, len(files), guild)
    card = None
    if state.get('card_id'):
        card = api.request('GET', f'/channels/{channel}/messages/{state["card_id"]}')
        verify_card(api, card, expected, key, fp)
    else:
        history = runtime.own_messages(api, channel)
        old = [m for m in history if f'[vk-album:{key}]' in m.get('content', '')]
        clean = [m for m in history if m.get('content', '').startswith(expected) and not m.get('attachments')]
        if len(old) > 1 or len(clean) > 1:
            raise AppError('Найдено несколько карточек главы. Нужна ручная проверка.')
        if old:
            print('Публикация прежней версии уже существует. Сообщения не изменены.')
            return
        elif clean:
            if not state.get('pending_card'):
                raise AppError('Найдена чистая карточка без локального состояния. Восстановите папку state; повтор не создан.')
            card = clean[0]
        else:
            state['pending_card'] = True
            downloader.atomic_json(state_path, state)
            card = api.post_message(channel, expected, f'{guild}:{channel}:{key}:card')
    state['schema'] = 3
    state['card_id'] = card['id']
    state.pop('pending_card', None)
    downloader.atomic_json(state_path, state)
    thread_id = card['id']
    try:
        thread = api.request('GET', f'/channels/{thread_id}')
    except ApiError as exc:
        if exc.status != 404:
            raise
        thread = api.request('POST', f'/channels/{channel}/messages/{card["id"]}/threads',
                             {'name': title, 'auto_archive_duration': ARCHIVE})
    if (str(thread.get('parent_id')) != channel or str(thread.get('guild_id')) != guild
            or thread.get('type') not in runtime.PUBLIC_THREAD_TYPES):
        raise AppError('Неверный канал или тип ветки.')
    state['thread_id'] = thread_id
    api.request('PATCH', f'/channels/{channel}/messages/{card["id"]}',
                {'content': card_text(chapter, len(files), guild, thread_id, loading=True),
                 'allowed_mentions': {'parse': []}, 'embeds': [], 'flags': 4})
    history = runtime.own_messages(api, thread_id)
    plan = state.get('group_plan')
    if not plan:
        plan = plan_batches(files, config)
    plan = validate_plan(plan, files)
    groups = recover(history, files, plan, state.get('groups', {}), key, fp)
    state.update(group_plan=plan, groups=groups, layout='grouped', schema=3)
    downloader.atomic_json(state_path, state)
    notify('sending')
    activate(api, thread, title, True)
    lookup = {m['id']: m for m in history}
    for index, pair in enumerate(plan, 1):
        ident = groups.get(str(index))
        if not ident:
            message = api.post_message(thread_id, caption(pair),
                                       f'{guild}:{channel}:{key}:group:{pair[0]}-{pair[1]}:{fp[:20]}', files[pair[0] - 1:pair[1]])
            groups[str(index)] = message['id']
            downloader.atomic_json(state_path, state)
            notify('sending')
        print(f'[{index}/{len(plan)}] {caption(pair)}', flush=True)
    notify('finalizing')
    api.request('PATCH', f'/channels/{channel}/messages/{card["id"]}',
                {'content': card_text(chapter, len(files), guild, thread_id), 'allowed_mentions': {'parse': []},
                 'embeds': [], 'flags': 4})
    publication_state.finish(state)
    downloader.atomic_json(state_path, state)
    notify('complete')
    print(f'Готово: https://discord.com/channels/{guild}/{thread_id}')


def main():
    parser = argparse.ArgumentParser(description='Публикация глав ВК: номер автоматически, изображения под спойлерами.')
    parser.add_argument('mode', choices=['setup', 'check', 'publish'])
    parser.add_argument('--url')
    parser.add_argument('--folder')
    parser.add_argument('--local', action='store_true')
    parser.add_argument('--expected', type=int)
    parser.add_argument('--browser', choices=['firefox', 'chrome', 'edge'])
    parser.add_argument('--title', help=argparse.SUPPRESS)  # v1/v2 command-line compatibility; ignored
    args = parser.parse_args()
    try:
        with runtime.exclusive():
            if args.mode == 'setup':
                runtime.setup()
                return 0
            if not (ROOT / 'config.json').exists():
                raise AppError('Сначала запустите SETUP.cmd.')
            config = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
            api = Discord(runtime.read_token())
            try:
                runtime.check(api, config)
                if args.mode == 'check':
                    return 0
                if args.folder or args.local:
                    location = args.folder or input('Путь к папке с JPG и manifest.json: ').strip().strip('"')
                    if not location:
                        raise AppError('Путь к папке не указан.')
                    folder = Path(location).resolve()
                else:
                    url = args.url or input(f'Ссылка на альбом [{downloader.DEFAULT_URL}]: ').strip() or downloader.DEFAULT_URL
                    key = downloader.album_key(url)
                    if args.expected is None:
                        value = input('Число фотографий в ВК (Enter = не проверять): ').strip()
                        args.expected = int(value) if value else None
                    if args.expected is not None and args.expected <= 0:
                        raise AppError('Количество фотографий должно быть положительным.')
                    downloader.run(argparse.Namespace(url=url, output=str(ROOT / 'downloads'), expected=args.expected,
                                                     cookies=str(ROOT / 'cookies.txt') if not args.browser and (ROOT / 'cookies.txt').exists() else None,
                                                     browser=args.browser))
                    folder = ROOT / 'downloads' / ('album' + key)
                manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
                if args.expected is not None and (args.expected <= 0 or len(manifest['photo_ids']) != args.expected):
                    raise AppError('Число фотографий не совпадает с --expected.')
                if downloader.album_key(manifest['source']) != manifest['album']:
                    raise AppError('Некорректная ссылка в манифесте.')
                state_path = ROOT / 'state' / str(config['guild_id']) / str(config['channel_id']) / (manifest['album'] + '.json')
                if state_path.exists() and json.loads(state_path.read_text(encoding='utf-8')).get('complete'):
                    print('Эта глава уже опубликована. Существующие сообщения не изменены.')
                    return 0
                chapter = resolve(folder, manifest, ROOT, args.browser)
                print('ГЛАВА ' + chapter)
                publish(api, config, folder, chapter)
            finally:
                api.close()
    except KeyboardInterrupt:
        print('\nОстановлено. Повторный запуск сверит историю.')
        return 130
    except (AppError, ValueError) as exc:
        print('\nОшибка: ' + str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print('\nОшибка: ' + type(exc).__name__ + '. Проверьте README_V3.txt.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
