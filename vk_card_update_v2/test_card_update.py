import ast
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from PIL import Image

import publisher_patch as update

PROJECT = Path(__file__).resolve().parent.parent
APP = PROJECT / 'vk_discord_publisher'
sys.path.insert(0, str(APP))
BASE_PUBLISHER = (APP / 'publish_album_v3.py').read_text(encoding='utf-8')
BASE_PIPELINE = (APP / 'ongoing.py').read_text(encoding='utf-8')

# The v9 receipt-recovery method is the patch contract. The supplied project
# snapshot is v7, so this fixture makes the later contract explicit and testable.
V9_LATEST = '''    def latest(self):
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
                recovered = pending.get(str(message['id']))
                if recovered:
                    path, receipt = recovered
                    publication_state.finish(receipt)
                    download.atomic_json(path, receipt)
                if latest is None:
                    latest = number
        return latest
'''


def v9_pipeline():
    tree = ast.parse(BASE_PIPELINE)
    pipeline = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Pipeline')
    latest = next(n for n in pipeline.body if isinstance(n, ast.FunctionDef) and n.name == 'latest')
    return update.replace_node(BASE_PIPELINE, latest, V9_LATEST)


def module_from_source(name, source):
    result = types.ModuleType(name)
    result.__file__ = str(APP / (name + '.py'))
    exec(compile(source, result.__file__, 'exec'), result.__dict__)
    return result


publisher = module_from_source('card_test_publisher', update.patch_publisher(BASE_PUBLISHER))
pipeline_module = module_from_source('card_test_ongoing', update.patch_pipeline(v9_pipeline()))
pipeline_module.publisher = publisher
pipeline_module.publication_state = types.SimpleNamespace(
    RECEIPT_NAME=re.compile(r'-?\d+_\d+(?:_\d+)?\.json'), ORIGINS=('automatic', 'manual'),
    finish=lambda receipt: receipt.update(complete=True))


class Discord:
    payload = staticmethod(publisher.Discord.payload)
    post_message = publisher.Discord.post_message

    def __init__(self):
        self.bot_id = '900'
        self.store = {'200': []}
        self.channels = {}
        self.number = 1000
        self.operations = []
        self.fail_once = None

    def messages(self, channel):
        return iter(copy.deepcopy(self.store.get(str(channel), [])))

    def request(self, method, route, payload=None, attachment=None, params=None):
        parts = route.strip('/').split('/')
        self.operations.append((method, route, copy.deepcopy(payload)))
        if method == 'POST' and parts[-1] == 'messages':
            self.number += 1
            files = publisher.files_list(attachment)
            message = {'id': str(self.number), 'content': payload['content'], 'author': {'id': '900'},
                       'flags': payload['flags'], 'attachments': [
                           {'filename': a['filename'], 'size': f.stat().st_size, 'flags': 8}
                           for a, f in zip(payload.get('attachments', []), files)]}
            self.store.setdefault(parts[1], []).append(message)
            if self.fail_once == 'batch' and files:
                self.fail_once = None
                raise publisher.UnknownResult('simulated lost response')
            return copy.deepcopy(message)
        if method == 'GET' and len(parts) == 2:
            if parts[1] not in self.channels:
                raise publisher.ApiError(404)
            return copy.deepcopy(self.channels[parts[1]])
        if method == 'GET' and len(parts) == 4:
            return copy.deepcopy(next(m for m in self.store[parts[1]] if m['id'] == parts[3]))
        if method == 'POST' and parts[-1] == 'threads':
            thread = dict(id=parts[3], parent_id=parts[1], guild_id='100', type=11,
                          thread_metadata={'archived': False}, name=payload['name'])
            self.channels[parts[3]] = thread; self.store[parts[3]] = []
            return copy.deepcopy(thread)
        if method == 'PATCH' and len(parts) == 2:
            self.channels[parts[1]].update(payload)
            return copy.deepcopy(self.channels[parts[1]])
        if method == 'PATCH' and len(parts) == 4:
            if self.fail_once == 'finish' and payload['content'].endswith('Опубликовано в: <#1001>'):
                self.fail_once = None
                raise publisher.UnknownResult('simulated interruption before final edit')
            message = next(m for m in self.store[parts[1]] if m['id'] == parts[3])
            message.update(payload)
            return copy.deepcopy(message)
        raise AssertionError((method, route))


class PublicationChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / 'album'; self.folder.mkdir()
        manifest = dict(album='-334424_310875314', source='https://vk.ru/album-334424_310875314',
                        album_title='1200', photo_ids=[str(n) for n in range(16)], files={})
        for number in range(1, 17):
            path = self.folder / f'{number:04}.jpg'
            Image.new('RGB', (20, 30), (number, 3, 4)).save(path)
            manifest['files'][str(number)] = dict(name=path.name, sha256=publisher.downloader.checksum(path))
        publisher.downloader.atomic_json(self.folder / 'manifest.json', manifest)
        self.config = dict(guild_id='100', channel_id='200', max_file_mib=8)
        self.api = Discord()
        self.capture = contextlib.redirect_stdout(io.StringIO()); self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)
        self.state_path = self.root / 'state/100/200/-334424_310875314.json'

    def send(self):
        publisher.publish(self.api, self.config, self.folder, '1200', root=self.root)

    def test_loading_link_precedes_pages_then_final_status_edits_same_card(self):
        self.send()
        card = self.api.store['200'][0]
        self.assertEqual(card['content'], '**ГЛАВА 1200**\nСтраниц: 16\nОпубликовано в: <#1001>')
        edits = [op for op in self.api.operations if op[0] == 'PATCH' and '/messages/' in op[1]]
        self.assertEqual(edits[0][2]['content'], '**ГЛАВА 1200**\nСтраниц: 16\nГлава загружается в: <#1001>')
        page_posts = [op for op in self.api.operations if op[0] == 'POST' and op[1] == '/channels/1001/messages']
        self.assertLess(self.api.operations.index(edits[0]), self.api.operations.index(page_posts[0]))
        self.assertGreater(self.api.operations.index(edits[-1]), self.api.operations.index(page_posts[-1]))
        self.assertEqual(len(self.api.store['200']), 1)
        self.assertNotIn('https://', card['content'])

    def test_only_card_allows_notifications_pages_remain_silent_spoilers_and_ordered(self):
        self.send()
        posts = [op for op in self.api.operations if op[0] == 'POST' and op[1].endswith('/messages')]
        self.assertEqual(posts[0][2]['flags'] & 4096, 0)
        self.assertTrue(all(op[2]['flags'] & 4096 for op in posts[1:]))
        self.assertTrue(all(op[2]['allowed_mentions'] == {'parse': []} for op in posts))
        pages = self.api.store['1001']
        self.assertEqual([m['content'] for m in pages], ['Страницы 1-6', 'Страницы 7-12', 'Страницы 13-16'])
        self.assertEqual([a['filename'] for m in pages for a in m['attachments']],
                         [f'SPOILER_{n:04}.jpg' for n in range(1, 17)])

    def test_lost_batch_response_resumes_without_another_card_or_repeated_pages(self):
        self.api.fail_once = 'batch'
        with self.assertRaises(publisher.UnknownResult): self.send()
        self.assertIn('Глава загружается в:', self.api.store['200'][0]['content'])
        self.send()
        self.assertEqual(len(self.api.store['200']), 1)
        self.assertEqual(len(self.api.store['1001']), 3)
        self.assertIn('Опубликовано в:', self.api.store['200'][0]['content'])

    def test_recovery_completes_loading_status_after_all_pages_without_new_posts(self):
        self.api.fail_once = 'finish'
        with self.assertRaises(publisher.UnknownResult): self.send()
        receipt = json.loads(self.state_path.read_text(encoding='utf-8'))
        receipt['publication_origin'] = 'manual'
        publisher.downloader.atomic_json(self.state_path, receipt)
        posts_before = sum(op[0] == 'POST' for op in self.api.operations)
        pipeline = pipeline_module.Pipeline(self.root, self.config, self.api)
        self.assertEqual(pipeline.latest(), 1200)
        self.assertIn('Опубликовано в:', self.api.store['200'][0]['content'])
        self.assertTrue(json.loads(self.state_path.read_text(encoding='utf-8'))['complete'])
        self.assertEqual(sum(op[0] == 'POST' for op in self.api.operations), posts_before)

    def test_incomplete_gallery_is_not_marked_published_by_recovery(self):
        self.api.fail_once = 'batch'
        with self.assertRaises(publisher.UnknownResult): self.send()
        receipt = json.loads(self.state_path.read_text(encoding='utf-8')); receipt['publication_origin'] = 'manual'
        publisher.downloader.atomic_json(self.state_path, receipt)
        pipeline = pipeline_module.Pipeline(self.root, self.config, self.api)
        self.assertIsNone(pipeline.latest())
        self.assertNotIn('Опубликовано в:', self.api.store['200'][0]['content'])

    def test_receipt_stays_incomplete_until_final_edit_is_confirmed(self):
        self.api.fail_once = 'finish'
        with self.assertRaises(publisher.UnknownResult): self.send()
        receipt = json.loads(self.state_path.read_text(encoding='utf-8')); receipt['publication_origin'] = 'manual'
        publisher.downloader.atomic_json(self.state_path, receipt)
        pipeline = pipeline_module.Pipeline(self.root, self.config, self.api)
        self.api.fail_once = 'finish'
        with self.assertRaises(publisher.UnknownResult): pipeline.latest()
        self.assertFalse(json.loads(self.state_path.read_text(encoding='utf-8')).get('complete'))
        self.assertIn('Глава загружается в:', self.api.store['200'][0]['content'])
        self.assertEqual(pipeline.latest(), 1200)
        self.assertEqual(len(self.api.store['1001']), 3)

    def test_completed_old_cards_are_not_rewritten(self):
        self.send()
        self.api.store['200'][0]['content'] = '**ГЛАВА 1200**\nСтраниц: 16\nhttps://discord.com/channels/100/1001'
        before = len(self.api.operations)
        pipeline = pipeline_module.Pipeline(self.root, self.config, self.api)
        self.assertEqual(pipeline.latest(), 1200)
        self.assertFalse(any(op[0] != 'GET' for op in self.api.operations[before:]))


class UpdateChecks(unittest.TestCase):
    def test_update_is_idempotent_and_preserves_provenance_and_observation_code(self):
        first = update.patch_publisher(BASE_PUBLISHER)
        self.assertEqual(update.patch_publisher(first), first)
        sample = v9_pipeline()
        patched = update.patch_pipeline(sample)
        self.assertEqual(update.patch_pipeline(patched), patched)
        self.assertIn("publication_state.finish(receipt)", patched)
        self.assertIn('STABLE = 900', patched)  # existing timing is preserved, never downgraded/replaced

    def test_unsupported_source_is_rejected_without_changing_installed_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'publish_album_v3.py').write_text(BASE_PUBLISHER, encoding='utf-8')
            (root / 'ongoing.py').write_text(BASE_PIPELINE, encoding='utf-8')
            original = (root / 'publish_album_v3.py').read_bytes()
            with self.assertRaises(update.PatchError): update.prepare(root, root / 'backup')
            self.assertEqual((root / 'publish_album_v3.py').read_bytes(), original)
            self.assertFalse((root / 'backup').exists())

    def test_failed_second_file_replace_rolls_back_both_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'Обновление с пробелами'; root.mkdir()
            inputs = {'publish_album_v3.py': BASE_PUBLISHER, 'ongoing.py': v9_pipeline()}
            for name, content in inputs.items(): (root / name).write_text(content, encoding='utf-8')
            original = {name: (root / name).read_bytes() for name in inputs}
            backup = root / 'backup'; update.prepare(root, backup)
            write = update.atomic_write
            calls = []
            def fail_second(target, data):
                calls.append(target.name)
                if len(calls) == 2: raise PermissionError('simulated replace failure')
                write(target, data)
            with patch.object(update, 'atomic_write', side_effect=fail_second):
                with self.assertRaises(PermissionError): update.apply(root, backup)
            self.assertTrue(all((root / name).read_bytes() == value for name, value in original.items()))


if __name__ == '__main__':
    unittest.main()
