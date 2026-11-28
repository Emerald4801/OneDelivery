"""Read-only VK album discovery and gallery-dl photo snapshots."""
from dataclasses import dataclass
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urljoin, urlparse

import requests
from album_info import chapter_number
import vk_session
import vk_api


class SourceError(RuntimeError):
    def __init__(self, message, *, retryable=False):
        self.retryable = retryable
        super().__init__(message)


@dataclass
class Album:
    key: str
    title: str
    chapter: int
    count: int | None = None
    domain: str = 'vk.ru'

    @property
    def url(self):
        return f'https://{self.domain}/album{self.key}'


@dataclass
class Snapshot:
    album: Album
    declared_count: int
    photos: list

    @property
    def ready(self):
        return self.declared_count > 0 and len(self.photos) == self.declared_count

    @property
    def signature(self):
        import hashlib
        data = [self.album.key, self.declared_count,
                [[p['id'], p.get('width'), p.get('height')] for p in self.photos]]
        return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


def make_album(key, title, count=None, domain='vk.ru'):
    try:
        chapter = chapter_number(title)
        if not chapter.isdigit():
            return None  # the automatic ongoing follows ordinary integer chapters
        return Album(key, str(title).strip(), int(chapter), count, domain)
    except ValueError:
        return None


class IndexParser(HTMLParser):
    def __init__(self, owner, base):
        super().__init__(convert_charrefs=True)
        self.owner, self.base = str(owner), base
        self.items, self.links, self.active = {}, set(), None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'a':
            href = html.unescape(values.get('href', ''))
            match = re.search(r'/album(' + re.escape(self.owner) + r'_\d+)(?:[/?#]|$)', href)
            if match:
                self.active = {'key': match[1], 'hint': values.get('title') or values.get('aria-label') or '',
                               'text': [], 'title': [], 'inside_title': False}
            absolute = urljoin(self.base, href)
            parsed = urlparse(absolute)
            if (parsed.hostname in {'vk.ru', 'vk.com', 'm.vk.ru', 'm.vk.com'} and
                    parsed.path == '/albums' + self.owner and re.search(r'(?:^|&)offset=\d+', parsed.query)):
                self.links.add(absolute)
        if self.active:
            classes = values.get('class', '') + ' ' + values.get('data-testid', '')
            if re.search(r'album.*title|album.*name', classes, re.I):
                self.active['inside_title'] = True
            if tag == 'img' and not self.active['hint']:
                self.active['hint'] = values.get('alt', '')

    def handle_data(self, value):
        if self.active:
            self.active['text'].append(value)
            if self.active['inside_title']:
                self.active['title'].append(value)

    def handle_endtag(self, tag):
        if self.active and tag == 'a':
            item, self.active = self.active, None
            text = ' '.join(' '.join(item['text']).split())
            name = ' '.join(' '.join(item['title']).split()) or item['hint'] or text
            # Count labels are not chapter numbers; strip them before parsing the title.
            name = re.sub(r'\b\d+\s*(?:фотограф\w*|photos?)\b', '', name, flags=re.I).strip(' ·|-')
            match = re.search(r'(\d+)\s*(?:фотограф\w*|photos?)', text, re.I)
            count = int(match[1]) if match else (0 if re.search(r'нет фотограф|no photos', text, re.I) else None)
            previous = self.items.get(item['key'])
            if previous is None or name:
                self.items[item['key']] = (name, count)
        elif self.active and tag in {'div', 'span'}:
            self.active['inside_title'] = False


def parse_index(page, owner, url):
    parser = IndexParser(owner, url)
    parser.feed(page)
    domain = 'vk.com' if '.com' in urlparse(url).hostname else 'vk.ru'
    items = []
    for key, (title, count) in parser.items.items():
        item = make_album(key, title, count, domain)
        if item:
            items.append(item)
    return items, parser.links, len(parser.items)


def parse_snapshot(raw, key, domain='vk.ru'):
    owner, ident = key.rsplit('_', 1)
    metadata = None
    photos, seen = [], set()
    for item in json.loads(raw):
        if not isinstance(item, list) or len(item) < 2 or not isinstance(item[-1], dict):
            continue
        data = item[-1]
        album, user = data.get('album', {}), data.get('user', {})
        if str(album.get('id')) != ident or str(user.get('id')) != owner:
            continue
        if album.get('name') and 'count' in album:
            current = (str(album['name']), int(album['count']))
            if metadata and metadata != current:
                raise SourceError('Название или счётчик альбома изменился во время чтения.', retryable=True)
            metadata = current
        if item[0] != 3 or len(item) < 3:
            continue
        photo_id = str(data.get('id') or data.get('photo_id') or '')
        if not photo_id or photo_id in seen:
            continue
        seen.add(photo_id)
        if not isinstance(item[1], str) or not item[1].startswith('https://'):
            raise SourceError('ВК вернул некорректную ссылку на изображение.')
        photos.append({'id': photo_id, 'url': item[1], 'width': data.get('width'), 'height': data.get('height')})
    if metadata is None:
        raise SourceError('ВК не вернул название и счётчик альбома; полнота не подтверждена.')
    album = make_album(key, metadata[0], metadata[1], domain)
    if not album:
        raise SourceError('Название альбома не соответствует одной номерной главе.')
    if metadata[1] < 0:
        raise SourceError('Некорректный счётчик альбома.')
    return Snapshot(album, metadata[1], photos)


class VKSource:
    def __init__(self, root, config):
        self.root, self.config = Path(root), config
        self.url = config.get('vk_albums_url', 'https://vk.ru/albums-334424')
        parsed = urlparse(self.url)
        match = re.fullmatch(r'/albums(-?\d+)', parsed.path)
        if parsed.scheme != 'https' or parsed.hostname not in {'vk.ru', 'vk.com', 'm.vk.ru', 'm.vk.com'} or not match:
            raise SourceError('В настройках нужна HTTPS-ссылка на список альбомов ВК.')
        self.owner = match[1]
        mode = config.get('vk_mode', 'web')
        if mode not in {'api', 'web'}:
            raise SourceError('Неизвестный режим источника VK. Допустимы api и web.')
        try:
            self.api_token = vk_api.read_token(self.root) if mode == 'api' else None
        except vk_api.VKAPIError as exc:
            raise SourceError(str(exc), retryable=exc.retryable) from None
        if config.get('vk_mode') == 'api' and not self.api_token:
            raise SourceError('Для режима API нужен проверенный ключ. Запустите PROBE_VK_API.cmd.')
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                                     'Accept-Language': 'ru-RU,ru;q=0.9'})
        if not self.api_token and config.get('vk_browser'):
            raise SourceError('Автоматический режим не читает профиль браузера. Используйте отдельный экспорт cookies ВК.')
        if not self.api_token:
            for cookie in vk_session.load(self.root):
                self.session.cookies.set_cookie(cookie)
        self.api_client = vk_api.Client(self.api_token, self.session) if self.api_token else None
        self.last_scan_complete = False

    def close(self):
        self.session.close()

    def albums(self, full=False):
        self.last_scan_complete = False
        # The source has fewer than a thousand albums. Complete discovery is simpler
        # and avoids assuming the expected chapter is among the first hundred.
        return self.api_albums(self.api_token, full=True) if self.api_token else self.web_albums(full)

    def api_albums(self, token, full=False):
        try:
            client = self.api_client or vk_api.Client(token, self.session)
            items = client.albums(self.owner, full)
        except vk_api.VKAPIError as exc:
            raise SourceError(str(exc), retryable=exc.retryable) from None
        self.last_scan_complete = full
        output = []
        for data in items:
            item = make_album(f"{self.owner}_{data['id']}", data.get('title', ''), data['size'])
            if item:
                output.append(item)
        return output

    def api_snapshot(self, album):
        owner, ident = album.key.rsplit('_', 1)
        if owner != self.owner:
            raise SourceError('Альбом относится к другому источнику.')
        try:
            before = self.api_client.album(owner, ident)
            photos = self.api_client.photos(owner, ident, int(self.config.get('vk_api_rev', 0)))
            after = self.api_client.album(owner, ident)
        except vk_api.VKAPIError as exc:
            raise SourceError(str(exc), retryable=exc.retryable) from None
        if any(before.get(k) != after.get(k) for k in ('title', 'size', 'updated')):
            raise SourceError('Альбом изменился во время чтения API.', retryable=True)
        result = make_album(album.key, after.get('title', ''), after['size'], album.domain)
        if result is None:
            raise SourceError('API не подтвердил название или полноту альбома.')
        if len(photos) != after['size']:
            raise SourceError('API не подтвердил полноту альбома.', retryable=True)
        return Snapshot(result, after['size'], photos)

    def web_albums(self, full=False):
        queue, visited, output, recognized = [self.url], set(), {}, 0
        while queue:
            if len(visited) >= 30:
                raise SourceError('Список альбомов слишком велик для веб-обхода; нужен API ВК.')
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            try:
                response = self.session.get(url, timeout=(15, 40))
                response.raise_for_status()
            except requests.RequestException as exc:
                raise SourceError('Страница альбомов VK: ' + vk_api.failure_message(exc), retryable=vk_api.should_retry(exc)) from None
            target = urlparse(response.url)
            if target.hostname not in {'vk.ru', 'vk.com', 'm.vk.ru', 'm.vk.com'} or target.path != '/albums' + self.owner:
                raise SourceError('ВК перенаправил со списка альбомов на вход или проверку. Нужна локальная сессия ВК или VK_API_TOKEN.')
            items, links, found = parse_index(response.text, self.owner, response.url)
            recognized += found
            for item in items:
                if item.key in output and output[item.key].chapter != item.chapter:
                    raise SourceError('Название альбома изменилось во время обхода.')
                output[item.key] = item
            queue.extend(link for link in sorted(links) if link not in visited)
            if not full:
                break
            if queue:
                time.sleep(0.5)
        if not recognized:
            raise SourceError('Страница ВК не содержит читаемого списка альбомов. Нужна локальная сессия ВК или VK_API_TOKEN.')
        if not output:
            raise SourceError('В списке не распознаны номерные главы. Нельзя подтвердить отсутствие новых глав.')
        # No pagination links does not prove a JS-driven index was fully read.
        # Callers use targeted snapshots and do not infer archive completeness.
        self.last_scan_complete = full and not queue
        return list(output.values())

    def snapshot(self, album):
        if self.api_client:
            return self.api_snapshot(album)
        command = [sys.executable, str(Path(__file__).with_name('snapshot_worker.py')), album.url]
        cookies = self.session.cookies.get_dict(domain='.' + album.domain)
        cookies.update(self.session.cookies.get_dict(domain=album.domain))
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    input=json.dumps({'cookies': cookies}),
                                    encoding='utf-8', errors='replace', timeout=600,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                                    env={**{k: v for k, v in os.environ.items() if k not in {'DISCORD_BOT_TOKEN', 'VK_API_TOKEN'}},
                                         'PYTHONIOENCODING': 'utf-8'})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SourceError('Не удалось прочитать фотографии альбома ВК.', retryable=isinstance(exc, subprocess.TimeoutExpired)) from None
        if result.returncode:
            raise SourceError('ВК не отдал фотографии альбома. Проверьте доступ или срок действия сессии.')
        return parse_snapshot(result.stdout, album.key, album.domain)
