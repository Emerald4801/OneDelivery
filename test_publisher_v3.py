import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent / 'vk_discord_publisher'))
import publish_album_v3 as app
import album_info


class FakeDiscord:
    def __init__(self):
        self.bot_id = '900'
        self.store = {'200': []}
        self.channels = {}
        self.counter = 1000
        self.fail_at = None
        self.mutations = []

    def messages(self, channel):
        return iter(list(self.store.get(str(channel), [])))

    def trip(self, point):
        if self.fail_at == point:
            self.fail_at = None
            raise app.UnknownResult('lost response')

    def request(self, method, route, payload=None, attachment=None, params=None):
        parts = route.strip('/').split('/')
        if method == 'GET' and len(parts) == 2:
            if parts[1] not in self.channels: raise app.ApiError(404)
            return self.channels[parts[1]]
        if method == 'GET' and len(parts) == 4:
            return next(m for m in self.store[parts[1]] if m['id'] == parts[3])
        self.mutations.append((method, route, payload))
        if method == 'POST' and parts[-1] == 'threads':
            thread = {'id': parts[3], 'parent_id': parts[1], 'guild_id': '100', 'type': 11,
                      'name': payload['name'], 'thread_metadata': {'auto_archive_duration': payload['auto_archive_duration']}}
            self.channels[parts[3]], self.store[parts[3]] = thread, []
            self.trip('thread')
            return thread
        if method == 'PATCH' and len(parts) == 2:
            self.channels[parts[1]].update(payload)
            return self.channels[parts[1]]
        if method == 'PATCH' and len(parts) == 4:
            message = next(m for m in self.store[parts[1]] if m['id'] == parts[3])
            message.update(payload)
            return message
        raise AssertionError((method, route))

    def post_message(self, channel, content, identity, attachment=None):
        self.counter += 1
        files = app.files_list(attachment)
        body = app.Discord.payload(content, files)
        message = {'id': str(self.counter), 'content': content, 'author': {'id': '900'}, 'embeds': [],
                   'attachments': [{'id': str(self.counter * 10 + i), 'filename': data['filename'],
                                    'size': path.stat().st_size, 'flags': 8}
                                   for i, (data, path) in enumerate(zip(body.get('attachments', []), files))]}
        self.store.setdefault(str(channel), []).append(message)
        self.mutations.append(('POST', str(channel), body))
        self.trip('batch' if files else 'card')
        return message


class PublisherChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / 'album'
        self.folder.mkdir()
        manifest = {'album': '-334424_310875314', 'source': 'https://vk.ru/album-334424_310875314',
                    'album_title': '1199', 'photo_ids': [str(n) for n in range(12)], 'files': {}}
        for n in range(1, 13):
            path = self.folder / f'{n:04}.jpg'
            Image.new('RGB', (20, 30), (n, 2, 3)).save(path)
            manifest['files'][str(n)] = {'name': path.name, 'sha256': app.downloader.checksum(path)}
        app.downloader.atomic_json(self.folder / 'manifest.json', manifest)
        self.config = {'guild_id': '100', 'channel_id': '200', 'max_file_mib': 8}
        self.api = FakeDiscord()
        capture = contextlib.redirect_stdout(io.StringIO())
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        patcher = patch.object(app, 'wait')
        patcher.start()
        self.addCleanup(patcher.stop)

    def publish(self):
        return app.publish(self.api, self.config, self.folder, '1199', self.root)

    def state_path(self):
        return self.root / 'state/100/200/-334424_310875314.json'

    def test_exact_content_spoilers_order_and_no_technical_labels(self):
        self.publish()
        card = self.api.store['200'][0]
        self.assertEqual(card['content'], '**ГЛАВА 1199**\nСтраниц: 12\n<https://discord.com/channels/100/1001>')
        messages = self.api.store[card['id']]
        self.assertEqual([m['content'] for m in messages], ['Страницы 1-6', 'Страницы 7-12'])
        names = [a['filename'] for m in messages for a in m['attachments']]
        self.assertEqual(names, [f'SPOILER_{n:04}.jpg' for n in range(1, 13)])
        self.assertTrue(all(a['flags'] & 8 for m in messages for a in m['attachments']))
        self.assertEqual(self.api.channels[card['id']]['name'], 'ГЛАВА 1199')
        self.assertEqual(self.api.channels[card['id']]['thread_metadata']['auto_archive_duration'], 10080)
        self.assertFalse(any('vk-' in m['content'] or 'Версия' in m['content'] or 'Статус' in m['content'] for m in [card] + messages))

    def test_complete_chapter_does_not_touch_any_old_messages(self):
        self.publish()
        count = len(self.api.mutations)
        self.publish()
        self.assertEqual(len(self.api.mutations), count)
        state = json.loads(self.state_path().read_text(encoding='utf-8'))
        state.pop('schema')
        app.downloader.atomic_json(self.state_path(), state)
        self.api.store['200'][0]['content'] = 'OLD V2 FORMAT'
        self.publish()
        self.assertEqual(self.api.store['200'][0]['content'], 'OLD V2 FORMAT')
        self.assertEqual(len(self.api.mutations), count)

    def test_lost_card_thread_or_batch_response_resumes_without_duplicates(self):
        for point in ['card', 'thread', 'batch']:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as temp:
                self.api = FakeDiscord()
                self.api.fail_at = point
                with self.assertRaises(app.UnknownResult):
                    app.publish(self.api, self.config, self.folder, '1199', Path(temp))
                app.publish(self.api, self.config, self.folder, '1199', Path(temp))
                self.assertEqual(len(self.api.store['200']), 1)
                self.assertEqual(len(self.api.store['1001']), 2)

    def test_lost_state_does_not_republish_clean_card(self):
        self.publish()
        self.state_path().unlink()
        count = len(self.api.mutations)
        with self.assertRaises(app.AppError): self.publish()
        self.assertEqual(len(self.api.mutations), count)

    def test_unfinished_old_version_does_not_mutate_discord(self):
        _, _, fp = app.runtime.local_album(self.folder, self.config)
        self.state_path().parent.mkdir(parents=True)
        app.downloader.atomic_json(self.state_path(), {'fingerprint': fp, 'bot_id': '900', 'card_id': '1001'})
        with self.assertRaises(app.AppError): self.publish()
        self.assertFalse(self.api.mutations)

    def test_existing_old_card_without_state_is_not_changed(self):
        _, _, fp = app.runtime.local_album(self.folder, self.config)
        self.api.store['200'].append({'id': '1001', 'author': {'id': '900'},
                                     'content': f'[vk-album:-334424_310875314]\nВерсия: {fp[:20]}', 'attachments': []})
        self.publish()
        self.assertFalse(self.api.mutations)

    def test_limits(self):
        class Virtual:
            def stat(self): return types.SimpleNamespace(st_size=7 * 1024 * 1024)
        self.assertEqual(app.plan_batches([Virtual() for _ in range(8)], {}), [[1, 3], [4, 6], [7, 8]])
        with self.assertRaises(app.AppError): app.plan_batches([Virtual()], {'batch_size': 11})


class TitleChecks(unittest.TestCase):
    def test_title_variants_and_ranges(self):
        for title, expected in [('1199', '1199'), ('Глава 1199', '1199'), ('1199: Заголовок', '1199'),
                                ('Chapter 1199', '1199'), ('1199.5', '1199.5')]:
            self.assertEqual(album_info.chapter_number(title), expected)
        for title in ['Войти в ВК', 'Глава 1199 - 1200', '1199-1200', 'Глава 1199 и глава 1200']:
            with self.assertRaises(ValueError): album_info.chapter_number(title)

    def test_actual_gallery_dl_metadata_shape(self):
        dump = json.dumps([[2, '', {'album': {'id': '310875314', 'name': '1199'}, 'user': {'id': '-334424'}}],
                           [3, 'https://cdn/image', {'album': {'id': '310875314', 'name': '1199'}, 'user': {'id': '-334424'}}]])
        self.assertEqual(album_info.title_from_dump(dump, '-334424_310875314'), '1199')
        with self.assertRaises(ValueError): album_info.title_from_dump(dump, '-111_310875314')

    def test_cached_title_needs_no_network(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(album_info, 'read_album_title') as read:
            root = Path(temp)
            self.assertEqual(album_info.resolve(root, {'album_title': '1199'}, root), '1199')
            read.assert_not_called()

    def test_legacy_manifest_gets_title_once_and_caches_it(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(album_info, 'read_album_title', return_value='1199') as read:
            root = Path(temp)
            manifest = {'source': 'https://vk.ru/album-334424_310875314', 'album': '-334424_310875314'}
            self.assertEqual(album_info.resolve(root, manifest, root), '1199')
            self.assertEqual(album_info.resolve(root, manifest, root), '1199')
            self.assertEqual(read.call_count, 1)

    def test_downloader_preserves_album_name(self):
        dump = json.dumps([[3, 'https://cdn/one.jpg', {'id': 1, 'album': {'id': '2', 'name': '1199'}}]])
        self.assertEqual(app.downloader.parse_photos(dump)[0]['album_title'], '1199')


class HttpChecks(unittest.TestCase):
    @patch.object(app, 'wait')
    def test_spoiler_metadata_and_stream_names_match_and_retry_reopens(self, _):
        api = app.Discord('NOT-A-REAL-TOKEN')
        self.addCleanup(api.close)
        with tempfile.TemporaryDirectory() as temp:
            files = [Path(temp) / f'{n:04}.jpg' for n in [1, 2]]
            for path in files: path.write_bytes(b'photo')
            seen = []
            def request(*args, **kwargs):
                body = json.loads(kwargs['data']['payload_json'])
                self.assertTrue(all(a['is_spoiler'] for a in body['attachments']))
                self.assertEqual([a['filename'] for a in body['attachments']], ['SPOILER_0001.jpg', 'SPOILER_0002.jpg'])
                seen.append([(key, value[0], value[1].read()) for key, value in kwargs['files'].items()])
                status, result = (429, {'retry_after': 0.01}) if len(seen) == 1 else (200, {'id': '1'})
                return types.SimpleNamespace(status_code=status, content=b'json', headers={}, json=lambda: result)
            with patch.object(api.session, 'request', side_effect=request):
                api.post_message('200', 'Страницы 1-2', 'key', files)
            self.assertEqual(seen[0], seen[1])


if __name__ == '__main__':
    unittest.main()
