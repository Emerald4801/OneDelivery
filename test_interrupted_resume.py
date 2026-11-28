"""Real Watcher/Controller/gallery recovery with local images and a fake Discord."""
import copy
import json
import logging
from pathlib import Path
import unittest
from unittest.mock import patch

from PIL import Image

import test_ongoing as fixtures
import test_publisher_v3 as gallery
from test_v9_recovery import RecordingDiscord
import ongoing
from publish_control import LocalControl
from publisher_controller import Controller


class InterruptedDiscord(RecordingDiscord):
    def __init__(self, *, accepted=False):
        super().__init__()
        self.accepted = accepted
        self.fail_batch = 2
        self.batch_attempts = 0
        self.offline = False

    def request(self, method, route, payload=None, attachment=None, params=None):
        if self.offline:
            raise ongoing.runtime.AppError('Discord недоступен.')
        batch = method == 'POST' and route.endswith('/messages') and attachment
        if batch:
            self.batch_attempts += 1
            if self.batch_attempts == self.fail_batch and not self.accepted:
                self.fail_batch = None
                raise ongoing.runtime.UnknownResult('Ответ на отправку не получен.')
        result = super().request(method, route, payload, attachment, params)
        if batch and self.batch_attempts == self.fail_batch:
            self.fail_batch = None
            raise ongoing.runtime.UnknownResult('Ответ на отправку не получен.')
        return result


class InterruptedResumeChecks(unittest.TestCase):
    state_path = gallery.PublisherChecks.state_path

    def setUp(self):
        gallery.PublisherChecks.setUp(self)
        manifest = json.loads((self.folder / 'manifest.json').read_text(encoding='utf-8'))
        for number in range(13, 17):
            path = self.folder / f'{number:04}.jpg'
            Image.new('RGB', (20, 30), (number, 2, 3)).save(path)
            manifest['files'][str(number)] = dict(name=path.name, sha256=ongoing.download.checksum(path))
        manifest['photo_ids'] = [str(n) for n in range(16)]
        ongoing.download.atomic_json(self.folder / 'manifest.json', manifest)
        self.snap = fixtures.snapshot(manifest['album'], chapter=1199, count=16, ids=range(16))
        self.cached = self.root / 'downloads' / ('album' + self.snap.album.key) / self.snap.signature[:20]
        self.cached.mkdir(parents=True)
        for path in self.folder.iterdir():
            (self.cached / path.name).write_bytes(path.read_bytes())
        self.source = fixtures.FakeSource()
        self.source.entries = {self.snap.album.key: self.snap}
        self.api = InterruptedDiscord()
        self.control = LocalControl(self.root, self.config)
        self.control.initialise(paused=True)
        self.control.set_expected(1199)
        self.reopen()
        no_download = patch.object(ongoing.download, 'download_one', side_effect=AssertionError('Unexpected image download'))
        no_download.start()
        self.addCleanup(no_download.stop)

    def reopen(self):
        self.pipeline = ongoing.Pipeline(self.root, self.config, self.api)
        self.worker = ongoing.Watcher(self.root, self.config, self.source, self.pipeline)
        self.controller = Controller(self.worker, self.control, ongoing.next_discovery, logging.getLogger('test'))

    def tick(self, offset):
        now = fixtures.at(12) + offset
        with patch('time.time', return_value=now):
            return self.controller.tick(now)

    def interrupt(self, *, manual=True):
        if manual:
            self.control.request_manual()
        else:
            self.control.pause(False)
        for offset in (0, 300):
            self.tick(offset)
            self.assertFalse(self.api.store['200'])
        with self.assertRaises(ongoing.runtime.UnknownResult):
            self.tick(600)
        with patch('time.time', return_value=fixtures.at(12) + 600):
            self.controller.fail('Ответ на отправку не получен.', fixtures.at(12) + 900)
        receipt = json.loads(self.state_path().read_text(encoding='utf-8'))
        self.assertFalse(receipt.get('complete'))
        self.assertEqual(list(receipt['groups']), ['1'])
        return copy.deepcopy(self.api.store['1001'])

    def assert_finished(self, before):
        receipt = json.loads(self.state_path().read_text(encoding='utf-8'))
        self.assertTrue(receipt['complete'])
        self.assertEqual(len(self.api.store['200']), 1)
        self.assertEqual(self.api.store['1001'][:len(before)], before)
        self.assertEqual([m['content'] for m in self.api.store['1001']],
                         ['Страницы 1-6', 'Страницы 7-12', 'Страницы 13-16'])
        names = [a['filename'] for m in self.api.store['1001'] for a in m['attachments']]
        self.assertEqual(names, [f'SPOILER_{n:04}.jpg' for n in range(1, 17)])
        self.assertIsNone(self.control.snapshot().get('expected_override'))
        self.assertNotIn('observation', self.worker.state)
        return receipt

    def test_manual_six_of_sixteen_resume_on_first_connected_cycle_without_observation(self):
        before = self.interrupt()
        self.assertEqual(len(before), 1)
        self.control.pause(False)
        self.tick(601)
        receipt = self.assert_finished(before)
        self.assertEqual(receipt['publication_origin'], 'manual')
        self.assertIsNone(self.worker.state.get('cooldown_until'))

    def test_manual_retry_while_auto_paused_resumes_the_same_gallery(self):
        before = self.interrupt()
        self.control.request_manual()
        self.tick(601)
        self.assert_finished(before)
        self.assertTrue(self.control.snapshot()['paused'])
        self.assertEqual(self.control.snapshot()['manual']['status'], 'succeeded')

    def test_resume_without_manual_number_uses_existing_baseline_and_pending_receipt(self):
        self.control.set_expected(None)
        self.worker.state['empty_baseline'] = 1198
        before = self.interrupt()
        self.control.pause(False)
        self.reopen()
        self.tick(601)
        self.assert_finished(before)

    def test_lost_response_after_acceptance_recovers_twelve_remote_pages_without_duplicates_after_restart(self):
        self.api.accepted = True
        self.api.parent_type = 5
        before = self.interrupt(manual=False)
        self.assertEqual(len(before), 2)
        self.reopen()
        self.tick(601)
        receipt = self.assert_finished(before)
        self.assertEqual(receipt['publication_origin'], 'automatic')
        self.assertEqual(self.worker.state['cooldown_until'], receipt['completed_at'] + 72 * 3600)

    def test_failed_reconnection_keeps_receipt_and_due_retry_resumes_without_ten_minutes(self):
        before = self.interrupt()
        original = self.state_path().read_bytes()
        self.control.pause(False)
        self.api.offline = True
        with self.assertRaises(ongoing.runtime.AppError):
            self.tick(601)
        self.controller.fail('Discord недоступен.', fixtures.at(12) + 901)
        self.assertEqual(self.state_path().read_bytes(), original)
        self.api.offline = False
        self.assertFalse(self.tick(900))
        self.tick(901)
        self.assert_finished(before)

    def test_changed_source_order_stops_before_any_message_mutation(self):
        self.interrupt()
        self.snap.photos.reverse()
        self.control.pause(False)
        count = len(self.api.mutations)
        with self.assertRaisesRegex(ongoing.runtime.AppError, 'Состав начатой главы изменился'):
            self.tick(601)
        self.assertEqual(len(self.api.mutations), count)

    def test_damaged_local_page_stops_before_any_message_mutation(self):
        self.interrupt()
        (self.cached / '0013.jpg').write_bytes(b'damaged')
        self.control.pause(False)
        count = len(self.api.mutations)
        with self.assertRaisesRegex(ongoing.runtime.AppError, 'повреждён'):
            self.tick(601)
        self.assertEqual(len(self.api.mutations), count)

    def test_missing_saved_manifest_stops_before_any_message_mutation(self):
        self.interrupt()
        (self.cached / 'manifest.json').unlink()
        self.control.pause(False)
        count = len(self.api.mutations)
        with self.assertRaisesRegex(ongoing.runtime.AppError, 'сохранённые файлы недоступны'):
            self.tick(601)
        self.assertEqual(len(self.api.mutations), count)

    def test_changed_receipt_fingerprint_stops_before_any_message_mutation(self):
        self.interrupt()
        receipt = json.loads(self.state_path().read_text(encoding='utf-8'))
        receipt['fingerprint'] = '0' * 64
        ongoing.download.atomic_json(self.state_path(), receipt)
        self.control.pause(False)
        count = len(self.api.mutations)
        with self.assertRaisesRegex(ongoing.runtime.AppError, 'отличаются от начатой публикации'):
            self.tick(601)
        self.assertEqual(len(self.api.mutations), count)


if __name__ == '__main__':
    unittest.main()
