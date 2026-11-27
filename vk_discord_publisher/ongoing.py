"""Small unattended VK -> Discord worker. No Gateway, inbound port, or browser scraping."""
import argparse
from contextlib import contextmanager, redirect_stdout
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import sys
import time

import requests
import download_album as download
import publisher_runtime as runtime
import publish_album_v3 as publisher
from vk_source import Album, SourceError, VKSource

MOSCOW = timezone(timedelta(hours=3))
POLL = 1800
OBSERVE = 300
STABLE = 900
MAX_GAP = 660


def read_json(path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        raise runtime.AppError('Не удалось прочитать локальное состояние. Файл сохранён для проверки.') from None


def discovery_open(now):
    return datetime.fromtimestamp(now, MOSCOW).hour >= 12


def next_discovery(now):
    local = datetime.fromtimestamp(now, MOSCOW)
    noon = local.replace(hour=12, minute=0, second=0, microsecond=0)
    if local < noon:
        return noon.timestamp()
    target = (math.floor(now / POLL) + 1) * POLL
    if not discovery_open(target):
        return (noon + timedelta(days=1)).timestamp()
    return target


def observe(previous, snapshot, now):
    """Only successful, regularly spaced observations count as evidence of stability."""
    if (not previous or previous.get('signature') != snapshot.signature
            or not 0 <= now - previous.get('last', now) <= MAX_GAP):
        previous = {'signature': snapshot.signature, 'first': now, 'last': now, 'samples': 1}
    elif now - previous['last'] >= OBSERVE:
        previous = {**previous, 'last': now, 'samples': previous['samples'] + 1}
    ready = snapshot.ready and previous['samples'] >= 4 and now - previous['first'] >= STABLE
    return previous, ready


def chapter_card(message):
    text = message.get('content', '')
    match = re.match(r'^\*\*ГЛАВА\s+(\d+)\*\*(?:\s*\n|$)', text)
    count = re.search(r'(?:^|\n)(?:Страниц|Фотографий):\s*(\d+)(?:\s*\n|$)', text)
    if not match or not count:
        return None
    return int(match[1]), int(count[1])


class Pipeline:
    def __init__(self, root, config, api):
        self.root, self.config, self.api = Path(root), config, api
        self.guild, self.channel = str(config['guild_id']), str(config['channel_id'])
        self.directory = self.root / 'state' / self.guild / self.channel
        self.directory.mkdir(parents=True, exist_ok=True)

    def state_path(self, album):
        return self.directory / (album.key + '.json')

    def complete_thread(self, card, count):
        thread_id = str(card['id'])
        try:
            thread = self.api.request('GET', '/channels/' + thread_id)
        except runtime.ApiError as exc:
            if exc.status == 404:
                return False
            raise
        if (str(thread.get('parent_id')) != self.channel or str(thread.get('guild_id')) != self.guild
                or thread.get('type') != 11):
            return False
        messages = sorted(runtime.own_messages(self.api, thread_id), key=lambda m: int(m['id']))
        pages = []
        for message in messages:
            # User comments and other bot messages are not chapter pages.
            if not message.get('attachments'):
                continue
            caption = re.fullmatch(r'Страницы (\d+)(?:[-–](\d+))?(?:\s*/\s*(\d+))?', message.get('content', '').split('\n')[0])
            if not caption:
                return False
            attachments = message['attachments']
            start, end = len(pages) + 1, len(pages) + len(attachments)
            if (int(caption[1]) != start or int(caption[2] or caption[1]) != end
                    or (caption[3] and int(caption[3]) != count)):
                return False
            for attachment in attachments:
                match = re.fullmatch(r'(?:SPOILER_)?(\d{4,})\.(?:jpg|png|webp|gif|avif)', attachment.get('filename', ''))
                if not match or not attachment.get('size'):
                    return False
                pages.append(int(match[1]))
        return count > 0 and pages == list(range(1, count + 1))

    def latest(self):
        """Main channel is the source of the expected chapter, including after restart."""
        found = []
        for message in self.api.messages(self.channel):
            if str(message.get('author', {}).get('id')) != self.api.bot_id:
                continue
            info = chapter_card(message)
            if info:
                found.append((info[0], info[1], message))
            if len(found) >= 30:
                break
        for number, count, message in sorted(found, key=lambda item: item[0], reverse=True):
            if self.complete_thread(message, count):
                return number
        return None

    def existing(self, number, count):
        matches = []
        for message in self.api.messages(self.channel):
            if str(message.get('author', {}).get('id')) != self.api.bot_id:
                continue
            info = chapter_card(message)
            if info and info[0] == number:
                matches.append((message, info[1]))
        if len(matches) > 1:
            raise runtime.AppError('В Discord несколько карточек одной главы. Автоматический выбор остановлен.')
        if not matches:
            return False
        card, remote_count = matches[0]
        state = None
        for path in self.directory.glob('*.json'):
            item = read_json(path, {})
            if item.get('card_id') == card['id'] and not item.get('complete'):
                state = item
                break
        if state:
            return False  # the publisher owns resuming this exact incomplete card
        if remote_count != count or not self.complete_thread(card, count):
            raise runtime.AppError('Карточка главы есть, но полнота ветки не подтверждена. Повторная копия не создана.')
        return True

    def download(self, snapshot):
        folder = self.root / 'downloads' / ('album' + snapshot.album.key) / snapshot.signature[:20]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'manifest.json'
        manifest = download.load_manifest(path, snapshot.album.url, snapshot.photos)
        manifest['album_title'] = snapshot.album.title
        download.atomic_json(path, manifest)
        max_bytes = int(float(self.config.get('max_file_mib', 8)) * 1024 * 1024)
        with requests.Session() as session:
            session.headers.update({'User-Agent': 'VKDiscordPublisher/5', 'Referer': snapshot.album.url})
            for number, photo in enumerate(snapshot.photos, 1):
                old = manifest['files'].get(str(number), {})
                name = old.get('name', '')
                if name and not re.fullmatch(r'\d{4,}\.(?:jpg|png|webp|gif|avif)', name):
                    raise runtime.AppError('Некорректное имя в манифесте загрузки.')
                previous = folder / name
                if name and previous.is_file() and download.checksum(previous) == old.get('sha256'):
                    continue
                temporary = folder / f'{number:04}.part'
                extension, (width, height) = download.download_one(session, photo['url'], temporary, max_bytes)
                target = folder / f'{number:04}.{extension}'
                temporary.replace(target)
                manifest['files'][str(number)] = {'name': target.name, 'sha256': download.checksum(target),
                                                  'width': width, 'height': height}
                download.atomic_json(path, manifest)
        return folder

    def publish(self, snapshot, folder):
        # A deleted publication can be reposted only when it is the expected chapter.
        # Preserve the former receipt rather than letting V3's complete flag suppress it.
        path = self.state_path(snapshot.album)
        state = read_json(path, {})
        if state.get('complete'):
            history = self.directory / 'receipts'
            history.mkdir(exist_ok=True)
            path.replace(history / f'{snapshot.album.key}_{time.time_ns()}.json')
        with redirect_stdout(io.StringIO()):
            publisher.publish(self.api, self.config, folder, str(snapshot.album.chapter), root=self.root)
        result = read_json(path, {})
        if not result.get('complete'):
            raise runtime.AppError('Полная отправка главы не подтверждена. Сохранён прогресс для продолжения.')


class Watcher:
    def __init__(self, root, config, source, pipeline, log=None):
        self.root, self.config, self.source, self.pipeline = Path(root), config, source, pipeline
        self.path = pipeline.directory / ('watch_' + source.owner + '.json')
        self.state = read_json(self.path, {})
        scope = [source.owner, pipeline.guild, pipeline.channel, pipeline.api.bot_id]
        if self.state.get('scope', scope) != scope:
            raise runtime.AppError('Изменился источник, канал или бот. Состояние относится к другой настройке.')
        self.state['scope'] = scope
        # A restart breaks continuous observation; never count offline time as stability.
        self.state.pop('observation', None)
        self.state['next_discovery'] = 0
        self.log = log or logging.getLogger('ongoing')
        self.last_status = None

    def save(self):
        download.atomic_json(self.path, self.state)

    def status(self, text):
        if text != self.last_status:
            self.log.info(text)
            self.last_status = text

    def discover(self, expected, full=False):
        albums = self.source.albums(full=full)
        # Retain known candidates, since an older album may be filled later.
        # A complete fresh index supersedes cached candidates (including deleted albums).
        known = {} if getattr(self.source, 'last_scan_complete', False) else {
            a['key']: a for a in self.state.get('known', []) if a['chapter'] == expected}
        known.update({a.key: asdict(a) for a in albums if a.chapter == expected})
        self.state['known'] = list(known.values())
        return [Album(**a) for a in known.values()]

    def select(self, albums, expected):
        snapshots = []
        for album in albums:
            # Even a cached zero count may have changed since the index was read.
            snapshot = self.source.snapshot(album)
            if snapshot.album.chapter != expected:
                raise SourceError('Номер главы изменился во время проверки.')
            if snapshot.declared_count or snapshot.photos:
                snapshots.append(snapshot)
        if len(snapshots) > 1:
            raise SourceError('Для ожидаемой главы найдено несколько непустых альбомов. Выбор остановлен.')
        return snapshots[0] if snapshots else None

    def cycle(self, now):
        current = self.pipeline.latest()
        if current is None:
            # An empty channel must never trigger sending hundreds of old chapters.
            if 'empty_baseline' not in self.state:
                if not discovery_open(now):
                    self.status('Discord пуст. Начальная проверка VK запланирована на 12:00 МСК.')
                    return next_discovery(now)
                albums = self.source.albums(full=True)
                for album in sorted(albums, key=lambda a: a.chapter, reverse=True):
                    if album.count == 0:
                        continue
                    snapshot = self.source.snapshot(album)
                    if snapshot.ready:
                        self.state['empty_baseline'] = album.chapter
                        break
                if 'empty_baseline' not in self.state:
                    raise SourceError('Начальная глава не определена: Discord пуст, готовые альбомы не найдены.')
                self.status(f"Discord пуст. Начальная точка: {self.state['empty_baseline']}; архив не отправляется.")
            current = self.state['empty_baseline']
        expected = current + 1
        if self.state.get('expected') != expected:
            self.state.update(expected=expected, known=[], next_discovery=0)
            self.state.pop('observation', None)
            self.state.pop('candidate', None)
        active = bool(self.state.get('candidate'))
        if not discovery_open(now) and not active:
            self.status(f'Последняя глава: {current}. Поиск {expected} продолжится в 12:00 МСК.')
            return next_discovery(now)
        known = [Album(**a) for a in self.state.get('known', [])]
        if now >= self.state.get('next_discovery', 0) or not known:
            known = self.discover(expected)
            self.state['next_discovery'] = now + POLL
        snapshot = self.select(known, expected)
        if snapshot is None:
            self.state.pop('observation', None)
            self.state.pop('candidate', None)
            self.status(f'Ожидается глава {expected}: непустой альбом пока не найден.')
            return next_discovery(now)
        self.state['candidate'] = asdict(snapshot.album)
        if not snapshot.ready:
            self.state.pop('observation', None)
            self.status(f'Глава {expected}: альбом наполняется ({len(snapshot.photos)}/{snapshot.declared_count}).')
            # Continue observing a nonempty, currently incomplete album after midnight too.
            self.state['observation'] = {'signature': None, 'first': now, 'last': now, 'samples': 0}
            return now + OBSERVE
        if self.pipeline.existing(expected, snapshot.declared_count):
            self.state.pop('observation', None)
            return now + 1
        observation, ready = observe(self.state.get('observation'), snapshot, now)
        self.state['observation'] = observation
        self.status(f'Глава {expected}: {snapshot.declared_count} страниц. Наблюдение за готовностью.')
        if not ready:
            return now + OBSERVE
        # Full available index is read only before sending, not every five minutes.
        candidates = self.discover(expected, full=True)
        confirmed = self.select(candidates, expected)
        if not confirmed or confirmed.signature != snapshot.signature:
            self.state.pop('observation', None)
            return now + OBSERVE
        if self.pipeline.existing(expected, snapshot.declared_count):
            self.state.pop('observation', None)
            return now + 1
        self.status(f'Глава {expected}: скачивание {snapshot.declared_count} страниц.')
        folder = self.pipeline.download(snapshot)
        final = self.source.snapshot(snapshot.album)
        if final.album.chapter != expected or final.signature != snapshot.signature or not final.ready:
            self.state.pop('observation', None)
            self.status(f'Глава {expected}: альбом изменился; ожидание начато заново.')
            return time.time() + OBSERVE
        if not self.pipeline.existing(expected, snapshot.declared_count):
            self.status(f'Глава {expected}: публикация.')
            self.pipeline.publish(snapshot, folder)
        self.state.pop('observation', None)
        self.state['next_discovery'] = 0
        self.status(f'Глава {expected} опубликована.')
        return time.time() + 1


@contextmanager
def channel_lock(config):
    """Protect one Discord destination even if two installation folders are run."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    key = hashlib.sha256(f"{config['guild_id']}:{config['channel_id']}".encode()).hexdigest()[:24]
    ctypes.set_last_error(0)
    handle = kernel.CreateMutexW(None, False, 'Local\\VKDiscordPublisher_' + key)
    if not handle:
        raise runtime.AppError('Не удалось установить защиту от параллельного запуска.')
    duplicate = ctypes.get_last_error() == 183
    try:
        if duplicate:
            raise runtime.AppError('Для этого Discord-канала программа уже запущена.')
        yield
    finally:
        kernel.CloseHandle(handle)


def logger(root):
    logs = root / 'logs'
    logs.mkdir(exist_ok=True)
    result = logging.getLogger('ongoing')
    result.setLevel(logging.INFO)
    if not result.handlers:
        file = RotatingFileHandler(logs / 'ongoing.log', maxBytes=1024 * 1024, backupCount=3, encoding='utf-8')
        console = logging.StreamHandler(sys.stdout) if sys.stdout is not None else None
        formatter = logging.Formatter('%(asctime)s %(message)s')
        formatter.converter = lambda timestamp: datetime.fromtimestamp(timestamp, MOSCOW).timetuple()
        for handler in [h for h in (file, console) if h is not None]:
            handler.setFormatter(formatter)
            result.addHandler(handler)
    return result


def noninteractive_token(root):
    if not os.environ.get('DISCORD_BOT_TOKEN') and not (root / 'token.dpapi').is_file():
        raise runtime.AppError('Токен не сохранён. Один раз выполните SETUP.cmd.')
    # read_token's manual fallback must never block a hidden worker on getpass.
    if os.environ.get('DISCORD_BOT_TOKEN'):
        return runtime.read_token(root)
    try:
        return runtime.protect((root / 'token.dpapi').read_bytes(), decrypt=True).decode().strip()
    except (runtime.AppError, UnicodeError):
        raise runtime.AppError('Сохранённый токен недоступен этой учётной записи. Нужен SETUP.cmd.') from None


def verify_dependencies(root):
    from importlib.metadata import version, PackageNotFoundError
    try:
        for line in (root / 'requirements.lock').read_text().splitlines():
            if not line.strip() or line.startswith('#'):
                continue
            name, expected = line.strip().split('==')
            if version(name) != expected:
                raise runtime.AppError('Версии библиотек отличаются от проверенных. Выполните prepare_env.ps1.')
    except (OSError, PackageNotFoundError, ValueError):
        raise runtime.AppError('Зависимости не подготовлены. Выполните prepare_env.ps1.') from None


def diagnose(source, pipeline, log):
    log.info('Источник VK: %s.', 'официальный API' if source.api_client else 'веб-страница')
    albums = source.albums()
    log.info('VK: список доступен; распознано номерных альбомов: %s.', len(albums))
    latest = pipeline.latest()
    log.info('Discord: последняя подтверждённая глава: %s.', latest if latest is not None else 'нет')
    if latest is not None:
        log.info('Следующая ожидаемая глава: %s.', latest + 1)
    candidates = [a for a in albums if a.chapter == latest + 1] if latest else sorted(albums, key=lambda a: a.chapter, reverse=True)[:1]
    if not candidates and albums:
        candidates = sorted(albums, key=lambda a: a.chapter, reverse=True)[:1]
    for album in candidates:
        snapshot = source.snapshot(album)
        log.info('VK: глава %s; получено страниц %s/%s.', snapshot.album.chapter, len(snapshot.photos), snapshot.declared_count)
    log.info('Диагностика завершена. Публикаций не выполнялось.')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--diagnose', action='store_true', help='read VK and Discord without publishing')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    log = logger(root)
    api = source = None
    try:
        verify_dependencies(root)
        config = read_json(root / 'config.json')
        if not config:
            raise runtime.AppError('Не найдены настройки. Один раз выполните SETUP.cmd.')
        with channel_lock(config), runtime.exclusive(root):
            api = publisher.Discord(noninteractive_token(root))
            while True:
                try:
                    with redirect_stdout(io.StringIO()):
                        runtime.check(api, config)
                    break
                except runtime.ApiError:
                    raise
                except runtime.AppError:
                    if args.diagnose or args.once:
                        raise
                    log.warning('Discord недоступен при запуске. Повтор через минуту.')
                    time.sleep(60)
            source = VKSource(root, config)
            pipeline = Pipeline(root, config, api)
            if args.diagnose:
                diagnose(source, pipeline, log)
                return 0
            worker = Watcher(root, config, source, pipeline, log)
            log.info('Запущено. Поиск: 12:00–24:00 МСК; интервал 30 минут, наблюдение 5 минут.')
            last_error = None
            next_run = 0
            last_clock = time.time()
            while True:
                now = time.time()
                if now < last_clock - 60:
                    worker.state.pop('observation', None)
                    next_run = 0
                last_clock = now
                if now < next_run:
                    time.sleep(min(30, next_run - now))
                    continue
                try:
                    next_run = worker.cycle(now)
                    last_error = None
                    worker.state['last_successful_check'] = time.time()
                    worker.state.pop('error', None)
                except runtime.ApiError as exc:
                    if exc.status in (401, 403):
                        raise
                    text = str(exc)
                    worker.state.pop('observation', None)
                    next_run = time.time() + OBSERVE
                    if text != last_error:
                        log.error(text)
                    last_error = text
                    worker.state['error'] = text
                except (SourceError, runtime.AppError, ValueError, OSError, RuntimeError) as exc:
                    # Known exception type strings may still contain signed URLs: redact them.
                    text = str(exc) if isinstance(exc, (SourceError, runtime.AppError)) else type(exc).__name__
                    text = re.sub(r'https?://\S+', '[адрес скрыт]', text)
                    worker.state.pop('observation', None)
                    next_run = time.time() + OBSERVE
                    if text != last_error:
                        log.error('%s Повтор через 5 минут.', text)
                    last_error = text
                    worker.state['error'] = text
                worker.state['next_check'] = next_run
                worker.save()
                if args.once:
                    return 1 if last_error else 0
    except KeyboardInterrupt:
        log.info('Остановлено. Прогресс сохранён.')
        return 0
    except Exception as exc:
        text = str(exc) if isinstance(exc, (SourceError, runtime.AppError)) else type(exc).__name__
        log.error('%s', re.sub(r'https?://\S+', '[адрес скрыт]', text))
        return 1
    finally:
        if source:
            source.close()
        if api:
            api.close()


if __name__ == '__main__':
    sys.exit(main())
