"""Resolve chapter numbers from VK album names emitted by gallery-dl."""
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def chapter_number(title):
    value = html.unescape(str(title)).replace('\u00a0', ' ').strip()
    number = r'(\d{1,5}(?:[.,]\d{1,3})?)'
    pure = re.fullmatch(r'№?\s*' + number, value)
    if pure:
        return pure[1].replace(',', '.')
    matches = list(re.finditer(r'(?:глава|chapter|ch\.)\s*№?\s*' + number + r'(?!\d)', value, re.I))
    if len(matches) == 1 and not re.match(r'^\s*[-–—]\s*\d', value[matches[0].end():]):
        return matches[0][1].replace(',', '.')
    # Accept a chapter number followed by a subtitle, but never guess a range.
    leading = re.match(r'^№?\s*' + number + r'(?:\s*[:—–-]\s*|\s+|\s*\()', value)
    if leading and not re.match(r'^\s*[-–—]\s*\d', value[leading.end(1):]):
        return leading[1].replace(',', '.')
    raise ValueError('Не удалось однозначно определить номер главы из названия альбома. Публикация не начата.')


def title_from_dump(raw, key):
    owner, album = key.rsplit('_', 1)
    titles = set()
    for message in json.loads(raw):
        if not isinstance(message, list) or len(message) < 2:
            continue
        data = message[-1]
        if not isinstance(data, dict):
            continue
        info = data.get('album', {})
        user = data.get('user', {})
        if not isinstance(info, dict) or not isinstance(user, dict):
            continue
        if str(info.get('id', '')) != album or str(user.get('id', '')) != owner:
            continue
        name = info.get('name') or info.get('title')
        if name:
            titles.add(str(name).strip())
    if len(titles) != 1:
        raise ValueError('ВК не вернул однозначное название нужного альбома. Проверьте доступ и авторизацию.')
    return titles.pop()


def read_album_title(url, key, cookies=None, browser=None):
    command = [sys.executable, '-m', 'gallery_dl', '--config-ignore', '--no-input',
               '--dump-json', '--range', '1', '--retries', '3', '--http-timeout', '30']
    if browser:
        command += ['--cookies-from-browser', browser]
    elif cookies:
        command += ['--cookies', str(Path(cookies).resolve())]
    command.append(url)
    print('Получаю название альбома ВК...', flush=True)
    result = subprocess.run(command, stdout=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    if result.returncode:
        raise ValueError('Не удалось получить название альбома. Проверьте доступ к ВК; сообщения не опубликованы.')
    return title_from_dump(result.stdout, key)


def resolve(folder, manifest, root, browser=None):
    import download_album as downloader
    title = manifest.get('album_title')
    if not title:
        title = read_album_title(manifest['source'], manifest['album'],
                                 root / 'cookies.txt' if (root / 'cookies.txt').exists() else None, browser)
    number = chapter_number(title)
    manifest['album_title'] = title
    downloader.atomic_json(folder / 'manifest.json', manifest)
    return number
