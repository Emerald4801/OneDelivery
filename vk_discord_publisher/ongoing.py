# VK_CARD_STATUS_UPDATE_1
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
import publication_state
from worker_status import StatusWriter, safe_error
from publish_control import LocalControl, ControlError
from publisher_controller import Controller
from vk_source import Album, SourceError, VKSource

MOSCOW = timezone(timedelta(hours=3))
POLL = 1800
OBSERVE = 300
STABLE = 600
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
    ready = snapshot.ready and previous['samples'] >= 3 and now - previous['first'] >= STABLE
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
                or thread.get('type') not in runtime.PUBLIC_THREAD_TYPES):
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
        pending = {}
        for path in self.directory.glob('*.json'):
            if not publication_state.RECEIPT_NAME.fullmatch(path.name):
                continue
            receipt = read_json(path, {})
            if (not receipt.get('complete') and receipt.get('card_id')
                    and str(receipt.get('bot_id')) == self.api.bot_id
                    and receipt.get('publication_origin') in publication_state.ORIGINS):
                pending[str(receipt['card_id'])] = (path, receipt)
        for message in self.api.messages(self.channel):
            if str(message.get('author', {}).get('id')) != self.api.bot_id:
                continue
            info = chapter_card(message)
            if info:
                found.append((info[0], info[1], message))
            if len(found) >= 30:
                break
        latest = None
        for number, count, message in sorted(found, key=lambda item: item[0], reverse=True):
            if latest is not None and str(message['id']) not in pending:
                continue
            if self.complete_thread(message, count):
                # Recover a confirmed send if the process died between Discord's
                # final response and the local completion write. No message edits.
                recovered = pending.get(str(message['id']))
                if recovered:
                    path, receipt = recovered
                    final_text = publisher.card_text(str(number), count, self.guild, str(message['id']))
                    if message.get('content') != final_text:
                        self.api.request('PATCH', f'/channels/{self.channel}/messages/{message["id"]}',
                                         {'content': final_text, 'allowed_mentions': {'parse': []},
                                          'embeds': [], 'flags': 4})
                    publication_state.finish(receipt)
                    download.atomic_json(path, receipt)
                if latest is None:
                    latest = number
        return latest

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

    def publish(self, snapshot, folder, *, origin='manual'):
        # A deleted publication can be reposted only when it is the expected chapter.
        # Preserve the former receipt rather than letting V3's complete flag suppress it.
        path = self.state_path(snapshot.album)
        state = read_json(path, {})
        if state.get('complete'):
            history = self.directory / 'receipts'
            history.mkdir(exist_ok=True)
            path.replace(history / f'{snapshot.album.key}_{time.time_ns()}.json')
        with redirect_stdout(io.StringIO()):
            publisher.publish(self.api, self.config, folder, str(snapshot.album.chapter), root=self.root, origin=origin)
        result = read_json(path, {})
        if not result.get('complete'):
            raise runtime.AppError('Полная отправка главы не подтверждена. Сохранён прогресс для продолжения.')


class Watcher:
    def __init__(self, root, config, source, pipeline, log=None, report=None):
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
        self.report = report
        self.publications = publication_state.PublicationIndex(pipeline.directory)

    def save(self):
        download.atomic_json(self.path, self.state)

    def stage(self, text):
        self.state['stage'] = text
        if self.report:
            self.report.update(phase=text, expected=self.state.get('expected'),
                               last_vk_check=self.state.get('last_vk_check'))

    def status(self, text):
        self.stage(text)
        if text != self.last_status:
            self.log.info(text)
            self.last_status = text

    def albums(self, full=False):
        self.stage('VK: проверка списка альбомов.')
        result = self.source.albums(full=full)
        self.state['last_vk_check'] = time.time()
        if self.report:
            self.report.update(last_vk_check=self.state['last_vk_check'])
        return result

    def snapshot(self, album):
        self.stage(f'VK: проверка альбома главы {album.chapter}.')
        result = self.source.snapshot(album)
        self.state['last_vk_check'] = time.time()
        if self.report:
            self.report.update(last_vk_check=self.state['last_vk_check'])
        return result

    def cooldown(self, now):
        summary = self.publications.summary(self.source.owner, self.pipeline.api.bot_id)
        self.state.update(summary)
        if self.report:
            self.report.update(**summary)
        deadline = summary['cooldown_until']
        if deadline and now < deadline:
            self.state.pop('observation', None)
            self.state.pop('candidate', None)
            self.state['next_discovery'] = 0
            until = datetime.fromtimestamp(deadline, MOSCOW).strftime('%d.%m %H:%M')
            self.status(f'Пауза после автоматической публикации до {until} МСК.')
            return deadline if discovery_open(deadline) else next_discovery(deadline)
        return None

    def discover(self, expected, full=False):
        albums = self.albums(full=full)
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
            snapshot = self.snapshot(album)
            if snapshot.album.chapter != expected:
                raise SourceError('Номер главы изменился во время проверки.')
            if snapshot.declared_count or snapshot.photos:
                snapshots.append(snapshot)
        if len(snapshots) > 1:
            raise SourceError('Для ожидаемой главы найдено несколько непустых альбомов. Выбор остановлен.')
        return snapshots[0] if snapshots else None

    def cycle(self, now, *, origin='automatic', target=None, on_expected=None,
              can_publish=lambda: True, expected_override=None):
        if origin not in publication_state.ORIGINS:
            raise runtime.AppError('Неизвестный способ запуска проверки.')
        manual = origin == 'manual'
        self.state['cycle_result'] = 'waiting'
        recovering_override = expected_override and self.state.get('override_attempt') == expected_override
        if not manual and not recovering_override:
            paused = self.cooldown(now)
            if paused is not None:
                return paused
        self.stage('Discord: проверка последней опубликованной главы.')
        current = self.pipeline.latest()
        self.state['last_discord_check'] = time.time()
        if recovering_override:
            for path in self.pipeline.directory.glob('*.json'):
                if not publication_state.RECEIPT_NAME.fullmatch(path.name):
                    continue
                receipt = read_json(path, {})
                plan = receipt.get('group_plan') or []
                if (receipt.get('complete') and str(receipt.get('chapter')) == str(expected_override['chapter'])
                        and plan and self.pipeline.existing(expected_override['chapter'], plan[-1][1])):
                    self.state.update(expected=max(current or 0, expected_override['chapter']) + 1, cycle_result='published')
                    self.state.pop('observation', None)
                    self.state.pop('candidate', None)
                    self.status(f"Глава {expected_override['chapter']}: полная публикация подтверждена после перезапуска.")
                    return now + POLL
        # latest() may have recovered a just-completed automatic publication.
        if not manual:
            paused = self.cooldown(now)
            if paused is not None:
                if current is not None and not expected_override:
                    self.state['expected'] = current + 1
                return paused
        else:
            self.state.update(self.publications.summary(self.source.owner, self.pipeline.api.bot_id))
        if current is None and not expected_override:
            # An empty channel must never trigger sending hundreds of old chapters.
            if 'empty_baseline' not in self.state:
                if not manual and not discovery_open(now):
                    self.status('Discord пуст. Начальная проверка VK запланирована на 12:00 МСК.')
                    return next_discovery(now)
                albums = self.albums(full=True)
                for album in sorted(albums, key=lambda a: a.chapter, reverse=True):
                    if album.count == 0:
                        continue
                    snapshot = self.snapshot(album)
                    if snapshot.ready:
                        self.state['empty_baseline'] = album.chapter
                        break
                if 'empty_baseline' not in self.state:
                    raise SourceError('Начальная глава не определена: Discord пуст, готовые альбомы не найдены.')
                self.status(f"Discord пуст. Начальная точка: {self.state['empty_baseline']}; архив не отправляется.")
            current = self.state['empty_baseline']
        expected = expected_override['chapter'] if expected_override else current + 1
        if target is not None:
            if not expected_override and current >= target:
                self.state.update(expected=expected, cycle_result='existing')
                self.status(f'Глава {target} уже опубликована. Ручная проверка завершена.')
                return now + 1
            if expected != target:
                raise runtime.AppError('Последняя глава в Discord изменилась. Запустите ручную проверку заново.')
        if self.state.get('expected') != expected:
            self.state.update(expected=expected, known=[], next_discovery=0)
            self.state.pop('observation', None)
            self.state.pop('candidate', None)
        if on_expected:
            on_expected(expected)  # durable target BEFORE any possible Discord POST
        active = bool(self.state.get('candidate'))
        if not manual and not discovery_open(now) and not active:
            self.status(f'Последняя глава: {current}. Поиск {expected} продолжится в 12:00 МСК.')
            return next_discovery(now)
        known = [Album(**a) for a in self.state.get('known', [])]
        if now >= self.state.get('next_discovery', 0) or not known:
            known = self.discover(expected)
            self.state['next_discovery'] = now + POLL
        snapshot = self.select(known, expected)
        if snapshot is None:
            self.state['cycle_result'] = 'not_found'
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
            self.state['cycle_result'] = ('published' if expected_override and
                self.state.get('override_attempt') == expected_override else 'existing')
            self.status(f'Глава {expected} уже опубликована.')
            return now + (POLL if expected_override else 1)
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
            self.state['cycle_result'] = 'existing'
            self.status(f'Глава {expected} уже опубликована.')
            return now + 1
        if not can_publish():
            self.state.pop('observation', None)
            self.status('Автоматика на ручной паузе. Отправка не начиналась.')
            return now + OBSERVE
        self.status(f'Глава {expected}: скачивание {snapshot.declared_count} страниц.')
        folder = self.pipeline.download(snapshot)
        final = self.snapshot(snapshot.album)
        if final.album.chapter != expected or final.signature != snapshot.signature or not final.ready:
            self.state.pop('observation', None)
            self.status(f'Глава {expected}: альбом изменился; ожидание начато заново.')
            return time.time() + OBSERVE
        if not self.pipeline.existing(expected, snapshot.declared_count):
            if not can_publish():
                self.state.pop('observation', None)
                self.status('Автоматика на ручной паузе. Отправка не начиналась.')
                return now + OBSERVE
            self.status(f'Глава {expected}: публикация.')
            if expected_override:
                self.state['override_attempt'] = expected_override
                self.save()  # durable before the first possible Discord POST
            self.pipeline.publish(snapshot, folder, origin=origin)
        self.state.pop('observation', None)
        self.state['next_discovery'] = 0
        self.state.pop('candidate', None)
        self.state['expected'] = max(current or 0, expected) + 1
        self.state['cycle_result'] = 'published'
        self.status(f'Глава {expected} опубликована.')
        if manual:
            self.state.update(self.publications.summary(self.source.owner, self.pipeline.api.bot_id))
            return time.time() + POLL
        return self.cooldown(time.time()) or time.time() + 1


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
    report = None
    failed = False
    controller = None
    control = None
    try:
        config = read_json(root / 'config.json')
        if not config:
            raise runtime.AppError('Не найдены настройки. Один раз выполните SETUP.cmd.')
        with channel_lock(config):
            if read_json(root / 'config.json') != config:
                raise runtime.AppError('Место публикации изменилось во время запуска. Запустите обработчик заново.')
            # Diagnostic commands must not overwrite the live worker's status.
            if not args.diagnose:
                report = StatusWriter(root, config)
                report.update(controller_version=3)
            verify_dependencies(root)
            if not args.diagnose:
                control = LocalControl(root, config)
                control.initialise()
                if control.snapshot().get('maintenance'):
                    return 0
                if report:
                    report.update(auto_paused=control.snapshot()['paused'])
            api = publisher.Discord(noninteractive_token(root))
            while True:
                try:
                    if report:
                        report.update(phase='Discord: проверка подключения и прав.')
                    with redirect_stdout(io.StringIO()):
                        runtime.check(api, config)
                    if report:
                        report.update(**getattr(api, 'destination', {}), bot_id=api.bot_id,
                                      error=None, action_required=False)
                    break
                except runtime.ApiError:
                    raise
                except runtime.AppError:
                    if args.diagnose or args.once:
                        raise
                    log.warning('Discord недоступен при запуске. Повтор через минуту.')
                    if report:
                        report.update(phase='Discord недоступен. Повтор через минуту.',
                                      error='Нет подключения к Discord.', next_check=time.time() + 60)
                    for _ in range(60):
                        if control and control.snapshot().get('maintenance'):
                            return 0
                        time.sleep(1)
            source = VKSource(root, config)
            pipeline = Pipeline(root, config, api)
            if args.diagnose:
                diagnose(source, pipeline, log)
                return 0
            worker = Watcher(root, config, source, pipeline, log, report=report)
            if report:
                report.update(expected=worker.state.get('expected'), last_vk_check=worker.state.get('last_vk_check'))
            log.info('Запущено. Поиск: 12:00–24:00 МСК; интервал 30 минут, наблюдение каждые 5 минут; готовность после 10 минут стабильности.')
            controller = Controller(worker, control, next_discovery, log, report,
                                    exclusive=lambda: runtime.exclusive(root))
            while True:
                now = time.time()
                try:
                    controller.tick(now)
                    if controller.stop_requested:
                        log.info('Обработчик безопасно остановлен для смены места публикации.')
                        return 0
                    if args.once:
                        return 0
                except runtime.ApiError as exc:
                    if exc.status in (401, 403):
                        raise
                    text = safe_error(str(exc))
                    log.error('%s Повтор автоматической проверки через 5 минут.', text)
                    controller.fail(text, time.time() + OBSERVE)
                    if args.once:
                        return 1
                except (SourceError, runtime.AppError, ControlError, ValueError, OSError, RuntimeError) as exc:
                    text = str(exc) if isinstance(exc, (SourceError, runtime.AppError, ControlError, publication_state.ReceiptError)) else type(exc).__name__
                    text = safe_error(text)
                    log.error('%s', text)
                    controller.fail(text, time.time() + OBSERVE)
                    if args.once:
                        return 1
                time.sleep(1)
    except KeyboardInterrupt:
        log.info('Остановлено. Прогресс сохранён.')
        return 0
    except Exception as exc:
        failed = True
        text = str(exc) if isinstance(exc, (SourceError, runtime.AppError, ControlError, publication_state.ReceiptError)) else type(exc).__name__
        log.error('%s', re.sub(r'https?://\S+', '[адрес скрыт]', text))
        if control and report:
            try:
                job = control.snapshot().get('manual') or {}
                if job.get('status') in ('queued', 'running'):
                    control.update_manual(job['id'], status='failed', finished_at=time.time(), message=safe_error(text))
            except (ControlError, OSError):
                pass
        if report:
            report.update(error=safe_error(text), action_required=True, next_check=None)
        return 1
    finally:
        if report:
            report.close(failed=failed)
        if source:
            source.close()
        if api:
            api.close()


if __name__ == '__main__':
    sys.exit(main())
