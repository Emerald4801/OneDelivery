import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent / 'vk_discord_publisher'))
import types
from test_history import source as historical_source
app = types.ModuleType('historical_publish_album')
app.__file__ = str(Path(__file__).parent / 'vk_discord_publisher' / 'publish_album.py')
exec(compile(historical_source('publish_album.py'), app.__file__, 'exec'), app.__dict__)


class FakeDiscord:
    def __init__(self):
        self.bot_id = '900'
        self.channels = {}
        self.store = {'200': []}
        self.counter = 1000
        self.fail_at = None

    def messages(self, channel):
        return iter(list(self.store.get(str(channel), [])))

    def request(self, method, route, payload=None, attachment=None, params=None):
        parts = route.strip('/').split('/')
        if method == 'GET' and parts[0] == 'channels' and len(parts) == 2:
            if parts[1] not in self.channels:
                raise app.ApiError(404)
            return self.channels[parts[1]]
        if method == 'POST' and parts[-1] == 'threads':
            thread = {'id': parts[3], 'parent_id': parts[1], 'guild_id': '100', 'type': 11}
            self.channels[parts[3]] = thread
            self.store[parts[3]] = []
            if self.fail_at == 'thread':
                self.fail_at = None
                raise app.UnknownResult('simulated lost thread response')
            return thread
        if method == 'PATCH' and len(parts) == 4:
            message = next(x for x in self.store[parts[1]] if x['id'] == parts[3])
            message.update(payload)
            return message
        if method == 'PATCH' and len(parts) == 2:
            self.channels[parts[1]].update(payload)
            return self.channels[parts[1]]
        raise AssertionError((method, route))

    def post_message(self, channel, content, identity, attachment=None):
        self.counter += 1
        message = {'id': str(self.counter), 'content': content, 'author': {'id': self.bot_id}, 'attachments': []}
        if attachment:
            message['attachments'] = [{'filename': attachment.name, 'size': attachment.stat().st_size}]
        self.store.setdefault(str(channel), []).append(message)
        point = attachment.name if attachment else 'card'
        if self.fail_at == point:
            self.fail_at = None
            raise app.UnknownResult('simulated lost post response')
        return message


class Checks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / 'album'
        self.folder.mkdir()
        self.manifest = {'album': '-334424_310875314', 'source': app.downloader.DEFAULT_URL,
                         'photo_ids': ['a', 'b', 'c'], 'files': {}}
        for n in range(1, 4):
            path = self.folder / f'{n:04}.jpg'
            Image.new('RGB', (20, 30), (n, 2, 3)).save(path)
            self.manifest['files'][str(n)] = {'name': path.name, 'sha256': app.downloader.checksum(path)}
        app.downloader.atomic_json(self.folder / 'manifest.json', self.manifest)
        self.config = {'guild_id': '100', 'channel_id': '200', 'max_file_mib': 8}
        self.api = FakeDiscord()
        patcher = patch.object(app, 'wait')
        patcher.start()
        self.addCleanup(patcher.stop)

    def publish(self):
        app.publish(self.api, self.config, self.folder, 'Chapter 1199', self.root)

    def test_success_then_no_duplicate(self):
        self.publish()
        self.publish()
        self.assertEqual(len(self.api.store['200']), 1)
        thread = self.api.store['200'][0]['id']
        self.assertEqual([m['attachments'][0]['filename'] for m in self.api.store[thread]],
                         ['0001.jpg', '0002.jpg', '0003.jpg'])

    def test_lost_card_thread_or_page_response(self):
        for fail_at in ['card', 'thread', '0002.jpg']:
            with self.subTest(fail_at=fail_at):
                self.api = FakeDiscord()
                self.api.fail_at = fail_at
                with tempfile.TemporaryDirectory() as state:
                    with self.assertRaises(app.UnknownResult):
                        app.publish(self.api, self.config, self.folder, 'Chapter', Path(state))
                    app.publish(self.api, self.config, self.folder, 'Chapter', Path(state))
                self.assertEqual(len(self.api.store['200']), 1)
                thread = self.api.store['200'][0]['id']
                self.assertEqual(len(self.api.store[thread]), 3)

    def test_recover_without_local_state(self):
        self.publish()
        with tempfile.TemporaryDirectory() as state:
            app.publish(self.api, self.config, self.folder, 'Chapter', Path(state))
        self.assertEqual(sum(len(x) for x in self.api.store.values()), 4)

    def test_missing_middle_page_does_not_append(self):
        self.publish()
        thread = self.api.store['200'][0]['id']
        self.api.store[thread].pop(1)
        with self.assertRaises(app.AppError): self.publish()
        self.assertEqual(len(self.api.store[thread]), 2)

    def test_changed_album_or_corrupt_image_stops(self):
        self.publish()
        path = self.folder / '0001.jpg'
        path.write_bytes(b'corrupt')
        with self.assertRaises(app.AppError): self.publish()

    def test_other_version_stops_before_posting(self):
        self.publish()
        path = self.folder / '0001.jpg'
        Image.new('RGB', (20, 30), 'red').save(path)
        self.manifest['files']['1']['sha256'] = app.downloader.checksum(path)
        app.downloader.atomic_json(self.folder / 'manifest.json', self.manifest)
        with self.assertRaises(app.AppError): self.publish()
        self.assertEqual(sum(len(x) for x in self.api.store.values()), 4)

    def test_permission_overwrites(self):
        guild = {'id': '100', 'owner_id': '999'}
        roles = [{'id': '100', 'permissions': str((1 << 10) | (1 << 11))},
                 {'id': '300', 'permissions': str(1 << 15)}]
        member = {'roles': ['300']}
        overwrites = [{'id': '100', 'type': 0, 'deny': str(1 << 11), 'allow': '0'},
                      {'id': '900', 'type': 1, 'allow': str(1 << 11), 'deny': str(1 << 15)}]
        bits = app.permissions(guild, member, roles, overwrites, '900')
        self.assertTrue(bits & (1 << 10))
        self.assertTrue(bits & (1 << 11))
        self.assertFalse(bits & (1 << 15))

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_dpapi_roundtrip(self):
        secret = b'LOCAL-TEST-NOT-A-REAL-TOKEN'
        try:
            encrypted = app.protect(secret)
        except app.AppError:
            self.skipTest('DPAPI недоступен в изолированной учётной записи; проверяется fallback без сохранения')
        self.assertNotIn(secret, encrypted)
        self.assertEqual(app.protect(encrypted, decrypt=True), secret)

    @unittest.skipUnless(os.name == 'nt', 'Windows lock')
    def test_single_process_lock(self):
        with app.exclusive(self.root):
            with self.assertRaises(app.AppError):
                with app.exclusive(self.root): pass

    def test_setup_does_not_save_plaintext_when_dpapi_unavailable(self):
        with patch.object(app.getpass, 'getpass', return_value='LOCAL-TEST-NOT-A-REAL-TOKEN'), \
             patch('builtins.input', side_effect=['100', '200']), \
             patch.object(app, 'check'), patch.object(app, 'protect', side_effect=app.AppError('Unavailable')):
            app.setup(self.root)
        self.assertFalse((self.root / 'token.dpapi').exists())
        self.assertNotIn('LOCAL-TEST', (self.root / 'config.json').read_text())

    def test_token_prompt_fallback_is_not_persisted(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(app.getpass, 'getpass', return_value='LOCAL-TEST-NOT-A-REAL-TOKEN'):
            self.assertEqual(app.read_token(self.root), 'LOCAL-TEST-NOT-A-REAL-TOKEN')
        self.assertFalse((self.root / 'token.dpapi').exists())


class Response:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.content = b'json'
        self.headers = headers or {}
    def json(self): return self._body


class HttpChecks(unittest.TestCase):
    @patch.object(app, 'wait')
    def test_429_reopens_attachment_and_retries(self, wait):
        api = app.Discord('LOCAL-TEST-NOT-A-REAL-TOKEN')
        self.addCleanup(api.close)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '0001.jpg'
            path.write_bytes(b'abc')
            reads = []
            def request(*args, **kwargs):
                reads.append(kwargs['files']['files[0]'][1].read())
                return Response(429, {'retry_after': 0.01}) if len(reads) == 1 else Response(body={'id': '1'})
            with patch.object(api.session, 'request', side_effect=request):
                api.post_message('200', '1/1', 'key', path)
            self.assertEqual(reads, [b'abc', b'abc'])

    def test_uncertain_post_not_blindly_retried(self):
        api = app.Discord('LOCAL-TEST-NOT-A-REAL-TOKEN')
        self.addCleanup(api.close)
        with patch.object(api.session, 'request', side_effect=app.requests.Timeout('secret')) as mock:
            with self.assertRaises(app.UnknownResult) as error:
                api.post_message('200', 'test', 'key')
            self.assertNotIn('secret', str(error.exception))
            self.assertEqual(mock.call_count, 1)

    def test_wrong_guild_before_writes(self):
        api = app.Discord('LOCAL-TEST-NOT-A-REAL-TOKEN')
        self.addCleanup(api.close)
        with patch.object(api, 'request', side_effect=[{'id': '900', 'bot': True}, {'guild_id': '999', 'type': 0}]) as request:
            with self.assertRaises(app.AppError):
                app.check(api, {'guild_id': '100', 'channel_id': '200'})
            self.assertTrue(all(call.args[0] == 'GET' for call in request.call_args_list))


if __name__ == '__main__':
    unittest.main()
