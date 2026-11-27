"""Test the imported production modules directly, without the old v7 contract fixture."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent / 'vk_discord_publisher'))
import ongoing
import publish_album_v3 as publisher
import publication_state
import test_publisher_v3 as fixtures


class RecordingDiscord(fixtures.FakeDiscord):
    post_message = publisher.Discord.post_message
    payload = staticmethod(publisher.Discord.payload)

    def request(self, method, route, payload=None, attachment=None, params=None):
        if method == 'POST' and route.endswith('/messages'):
            self.counter += 1
            files = publisher.files_list(attachment)
            message = dict(id=str(self.counter), content=payload['content'], author={'id': self.bot_id},
                flags=payload['flags'], attachments=[dict(filename=a['filename'], size=p.stat().st_size, flags=8)
                    for a, p in zip(payload.get('attachments', []), files)])
            self.store.setdefault(route.split('/')[2], []).append(message)
            self.mutations.append((method, route, copy.deepcopy(payload)))
            self.trip('batch' if files else 'card')
            return copy.deepcopy(message)
        if method == 'PATCH' and '/messages/' in route and 'Опубликовано в:' in payload.get('content', ''):
            self.trip('finish')
        return super().request(method, route, payload, attachment, params)


class RecoveryChecks(unittest.TestCase):
    setUp = fixtures.PublisherChecks.setUp
    state_path = fixtures.PublisherChecks.state_path

    def send(self, origin='manual'):
        publisher.publish(self.api, self.config, self.folder, '1199', self.root, origin=origin)

    def test_final_edit_must_succeed_before_receipt_completes_and_recovery_sends_no_pages(self):
        self.api = RecordingDiscord()
        self.api.fail_at = 'finish'
        with self.assertRaises(publisher.UnknownResult): self.send('automatic')
        receipt = json.loads(self.state_path().read_text(encoding='utf-8'))
        self.assertFalse(receipt.get('complete'))
        self.assertEqual(receipt['publication_origin'], 'automatic')
        pipeline = ongoing.Pipeline(self.root, self.config, self.api)
        before = sum(m[0] == 'POST' for m in self.api.mutations)
        self.api.fail_at = 'finish'
        with self.assertRaises(publisher.UnknownResult): pipeline.latest()
        self.assertFalse(json.loads(self.state_path().read_text(encoding='utf-8')).get('complete'))
        self.assertEqual(pipeline.latest(), 1199)
        receipt = json.loads(self.state_path().read_text(encoding='utf-8'))
        self.assertTrue(receipt['complete'])
        self.assertEqual(sum(m[0] == 'POST' for m in self.api.mutations), before)
        self.assertIn('Опубликовано в: <#1001>', self.api.store['200'][0]['content'])
        summary = publication_state.PublicationIndex(pipeline.directory).summary('-334424', '900')
        self.assertEqual(summary['cooldown_until'], receipt['completed_at'] + 72 * 3600)

    def test_loading_card_precedes_ordered_silent_spoiler_pages(self):
        self.api = RecordingDiscord()
        self.send()
        posts = [m for m in self.api.mutations if m[0] == 'POST' and m[1].endswith('/messages')]
        self.assertEqual(posts[0][2]['flags'], 4)
        self.assertTrue(all(m[2]['flags'] == 4096 | 4 for m in posts[1:]))
        first_page = self.api.mutations.index(posts[1])
        loading = next(i for i, m in enumerate(self.api.mutations)
                       if m[0] == 'PATCH' and 'Глава загружается в:' in m[2].get('content', ''))
        self.assertLess(loading, first_page)
        names = [a['filename'] for m in self.api.store['1001'] for a in m['attachments']]
        self.assertEqual(names, [f'SPOILER_{n:04}.jpg' for n in range(1, 13)])
        summary = publication_state.PublicationIndex(self.state_path().parent).summary('-334424', '900')
        self.assertIsNone(summary['cooldown_until'])

    def test_partial_gallery_is_not_recovered_as_complete(self):
        self.api = RecordingDiscord()
        self.api.fail_at = 'batch'
        with self.assertRaises(publisher.UnknownResult): self.send()
        pipeline = ongoing.Pipeline(self.root, self.config, self.api)
        self.assertIsNone(pipeline.latest())
        self.assertFalse(json.loads(self.state_path().read_text(encoding='utf-8')).get('complete'))


if __name__ == '__main__':
    unittest.main()
