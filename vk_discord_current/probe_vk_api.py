"""Local, read-only acceptance check before enabling official VK API access."""
import argparse
import getpass
import json
import os
from pathlib import Path
import tempfile

import requests
import download_album as download
import vk_api
import publisher_runtime as runtime

OWNER = '-334424'
ALBUM = '310875314'
KEY = OWNER + '_' + ALBUM


def activate_api(root):
    """Select API explicitly using the completed local acceptance check; no network."""
    root = Path(root)
    receipt = root / 'diagnostics' / 'vk_api_probe.json'
    try:
        report = json.loads(receipt.read_text(encoding='utf-8'))
        image = report.get('first_image', {})
        valid = (report.get('source') == 'official_vk_api' and report.get('owner') == OWNER
                 and report.get('album') == KEY and report.get('api_read_verified') is True
                 and report.get('token_saved') is True and report.get('order_matches_reference') is True
                 and type(report.get('photo_count')) is int and report['photo_count'] > 0
                 and type(image.get('width')) is int and image['width'] > 0
                 and type(image.get('height')) is int and image['height'] > 0)
    except (OSError, ValueError, AttributeError):
        valid = False
    if not valid or not (root / 'vk_api_token.dpapi').is_file():
        raise vk_api.VKAPIError('Нет полной успешной проверки API. Выполните PROBE_VK_API.cmd; ключ повторно вводить не обязательно.')
    path = root / 'config.json'
    try:
        config = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise vk_api.VKAPIError('Не найдены настройки Discord для включения режима API.') from None
    source = config.get('vk_albums_url', 'https://vk.ru/albums-334424').rstrip('/')
    if source not in {'https://vk.ru/albums-334424', 'https://vk.com/albums-334424',
                       'https://m.vk.ru/albums-334424', 'https://m.vk.com/albums-334424'}:
        raise vk_api.VKAPIError('Проверка API выполнена для другого источника. Настройки не изменены.')
    config.update(vk_mode='api', vk_api_rev=0)
    download.atomic_json(path, config)


def reference_manifest(root):
    candidates = [root / 'downloads' / ('album' + KEY) / 'manifest.json',
                  Path(os.environ.get('USERPROFILE', str(Path.home()))) / 'Downloads/vk_discord_publisher_windows_v1/vk_discord_publisher/downloads' / ('album' + KEY) / 'manifest.json']
    for path in candidates:
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8'))
            if data.get('album') == KEY and data.get('photo_ids'):
                return data
    return None


def verify(client, root, report):
    print('Проверяю получение списка альбомов через API...', flush=True)
    albums = client.albums(OWNER)
    report['albums_received'] = len(albums)
    print(f'Список получен: {len(albums)} альбомов в первой части.', flush=True)
    print('Проверяю фотографии главы 1199 через API...', flush=True)
    before = client.album(OWNER, ALBUM)
    photos = client.photos(OWNER, ALBUM)
    after = client.album(OWNER, ALBUM)
    if any(before.get(k) != after.get(k) for k in ('title', 'size', 'updated')) or len(photos) != after['size'] or not photos:
        raise vk_api.VKAPIError('Состав альбома изменился или неполон. Проверку нужно повторить.')
    report.update(album_title=after.get('title'), photo_count=len(photos))
    print(f"Глава {after.get('title')}: получено {len(photos)} фотографий.", flush=True)
    reference = reference_manifest(root)
    report['order_matches_reference'] = None
    if reference:
        if [p['id'] for p in photos] != [str(p) for p in reference['photo_ids']]:
            report['order_matches_reference'] = False
            raise vk_api.VKAPIError('API доступен, но порядок фотографий отличается от ранее проверенной копии. Автоматика не переключена.')
        report['order_matches_reference'] = True
        print('Порядок совпадает с ранее скачанной и проверенной главой.', flush=True)
    else:
        print('Нет прежнего манифеста: порядок API пока не сопоставлен с проверенной копией.', flush=True)
    print('Проверяю скачивание первой страницы...', flush=True)
    with tempfile.TemporaryDirectory() as temp, requests.Session() as session:
        target = Path(temp) / 'page.part'
        extension, size = download.download_one(session, photos[0]['url'], target, max_bytes=24*1024*1024)
        report['first_image'] = {'format': extension, 'width': size[0], 'height': size[1]}
    print(f'Первая страница читается: {size[0]} × {size[1]}.', flush=True)
    report['api_read_verified'] = True


def main():
    parser = argparse.ArgumentParser(description='Проверить официальный VK API без публикации в Discord.')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--activate', action='store_true', help='select API using a completed local check; no publication')
    args = parser.parse_args()
    default_root = Path(os.environ['LOCALAPPDATA']) / 'VKDiscordPublisher' if not args.root else None
    root = (args.root or default_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if args.activate:
        try:
            activate_api(root)
            print('Режим API выбран по успешной проверке. Публикация не запускалась.', flush=True)
            return 0
        except vk_api.VKAPIError as exc:
            print(str(exc), flush=True)
            return 1
    diagnostics = root / 'diagnostics'
    diagnostics.mkdir(exist_ok=True)
    report = {'source': 'official_vk_api', 'owner': OWNER, 'album': KEY, 'api_read_verified': False,
              'token_saved': False, 'discord_modified': False}
    client = None
    try:
        print('Проверка VK API. Сообщения в Discord не отправляются.', flush=True)
        try:
            saved = vk_api.read_token(root)
        except vk_api.VKAPIError:
            saved = None
        if os.environ.get('VK_API_TOKEN'):
            token = saved
        else:
            hint = ' Enter = сохранённый ключ.' if saved else ''
            token = getpass.getpass('Ключ VK API (ввод скрыт; не отправляйте его в чат).' + hint + ': ') or saved or ''
        client = vk_api.Client(token)
        verify(client, root, report)
        vk_api.save_token(root, token)
        report['token_saved'] = True
        print('Ключ сохранён с защитой Windows. Расписание не включалось.', flush=True)
        return 0
    except KeyboardInterrupt:
        print('\nПроверка остановлена.')
        report['error'] = 'interrupted'
        return 130
    except Exception as exc:
        report['error'] = str(exc) if isinstance(exc, (vk_api.VKAPIError, runtime.AppError)) else type(exc).__name__
        if isinstance(exc, vk_api.VKAPIError):
            report['api_error_code'] = exc.code
        print('\n' + report['error'], flush=True)
        print('Ключ не сохранён. Автоматическая публикация не включена.', flush=True)
        return 1
    finally:
        if client:
            client.close()
        download.atomic_json(diagnostics / 'vk_api_probe.json', report)
        print('Отчёт без ключа: ' + str(diagnostics / 'vk_api_probe.json'), flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
