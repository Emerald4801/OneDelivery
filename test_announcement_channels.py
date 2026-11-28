"""Announcement destinations, effective rights and recovery without live Discord calls."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / 'vk_discord_publisher'))
import destination_settings
import ongoing
import publisher_runtime as runtime
import test_publisher_v3 as fixtures
import test_v9_recovery as recovery


class DestinationAPI:
    def __init__(self, token, *, channel_type=5, missing_bit=None):
        self.channel_type = channel_type
        self.missing_bit = missing_bit
        self.calls = []

    def close(self):
        pass

    def request(self, method, route, payload=None, attachment=None, params=None):
        self.calls.append((method, route))
        if method != 'GET':
            raise AssertionError('Destination checks must not send messages')
        if route == '/users/@me':
            return dict(bot=True, id='900', username='OneDelivery')
        if route == '/channels/200':
            return dict(id='200', guild_id='100', type=self.channel_type,
                        name='announcements', permission_overwrites=[])
        if route == '/guilds/100':
            return dict(id='100', name='Server', owner_id='999')
        if route == '/guilds/100/roles':
            rights = sum(1 << bit for bit in (10, 11, 15, 16, 35, 38) if bit != self.missing_bit)
            return [dict(id='100', permissions='0'), dict(id='500', permissions=str(rights))]
        if route == '/guilds/100/members/900':
            return dict(roles=['500'])
        if route == '/channels/200/messages':
            return []
        if route == '/guilds/100/channels':
            return [dict(id=str(kind + 1000), name='channel-' + str(kind), type=kind, position=kind)
                    for kind in (0, 2, 4, 5, 10, 11, 12, 15, 16)]
        raise AssertionError(route)


class DestinationChecks(unittest.TestCase):
    def test_dropdown_includes_text_and_announcement_but_not_other_types(self):
        api = DestinationAPI('test')
        with patch.object(ongoing, 'noninteractive_token', return_value='test'), \
                patch.object(runtime, 'Discord', return_value=api):
            items = destination_settings.list_destinations(Path('.'), '100')
        self.assertEqual([item['type'] for item in items], [0, 5])
        self.assertEqual([item['id'] for item in items], ['1000', '1005'])
        self.assertEqual(api.calls, [('GET', '/guilds/100/channels')])

    def test_effective_bot_role_permissions_accept_both_supported_channel_types(self):
        for kind in (0, 5):
            with self.subTest(kind=kind), contextlib.redirect_stdout(io.StringIO()):
                api = DestinationAPI('test', channel_type=kind)
                _, channel = runtime.check(api, dict(guild_id='100', channel_id='200'))
                self.assertEqual(channel['type'], kind)
                self.assertTrue(all(method == 'GET' for method, _ in api.calls))

    def test_announcement_access_check_still_requires_each_existing_permission(self):
        for bit, label in ((10, 'View Channel'), (11, 'Send Messages'), (15, 'Attach Files'),
                           (16, 'Read Message History'), (35, 'Create Public Threads'),
                           (38, 'Send Messages in Threads')):
            with self.subTest(permission=label):
                api = DestinationAPI('test', missing_bit=bit)
                with self.assertRaisesRegex(runtime.AppError, label):
                    runtime.check(api, dict(guild_id='100', channel_id='200'))
                self.assertTrue(all(method == 'GET' for method, _ in api.calls))

    def test_forums_categories_voice_and_existing_threads_remain_unsupported(self):
        for kind in (2, 4, 10, 11, 12, 15, 16):
            with self.subTest(kind=kind), self.assertRaises(runtime.AppError):
                runtime.check(DestinationAPI('test', channel_type=kind), dict(guild_id='100', channel_id='200'))


class PublicationChecks(unittest.TestCase):
    state_path = fixtures.PublisherChecks.state_path

    def setUp(self):
        fixtures.PublisherChecks.setUp(self)
        self.api = recovery.RecordingDiscord()
        self.api.parent_type = 5

    def send(self):
        fixtures.app.publish(self.api, self.config, self.folder, '1199', self.root)

    def test_announcement_thread_completes_with_ordered_spoilers_and_silent_pages(self):
        self.send()
        self.assertEqual(self.api.channels['1001']['type'], 10)
        self.assertEqual(ongoing.Pipeline(self.root, self.config, self.api).latest(), 1199)
        self.assertEqual([a['filename'] for message in self.api.store['1001'] for a in message['attachments']],
                         [f'SPOILER_{n:04}.jpg' for n in range(1, 13)])
        posts = [operation for operation in self.api.mutations
                 if operation[0] == 'POST' and operation[1].endswith('/messages')]
        self.assertEqual(posts[0][2]['flags'], 4)
        self.assertTrue(all(operation[2]['flags'] == 4096 | 4 for operation in posts[1:]))
        self.assertFalse(any('/crosspost' in operation[1] for operation in self.api.mutations))

    def test_lost_final_edit_recovers_announcement_thread_without_new_posts(self):
        self.api.fail_at = 'finish'
        with self.assertRaises(runtime.UnknownResult): self.send()
        self.assertFalse(json.loads(self.state_path().read_text(encoding='utf-8')).get('complete'))
        posts_before = sum(operation[0] == 'POST' for operation in self.api.mutations)
        self.assertEqual(ongoing.Pipeline(self.root, self.config, self.api).latest(), 1199)
        self.assertTrue(json.loads(self.state_path().read_text(encoding='utf-8'))['complete'])
        self.assertEqual(sum(operation[0] == 'POST' for operation in self.api.mutations), posts_before)

    def test_partial_announcement_thread_resumes_without_duplicates(self):
        self.api.fail_at = 'batch'
        with self.assertRaises(runtime.UnknownResult): self.send()
        self.assertIsNone(ongoing.Pipeline(self.root, self.config, self.api).latest())
        self.send()
        self.assertEqual(len(self.api.store['200']), 1)
        self.assertEqual(len(self.api.store['1001']), 2)
        self.assertEqual(ongoing.Pipeline(self.root, self.config, self.api).latest(), 1199)

    def test_private_thread_is_rejected_during_resume_and_completeness_check(self):
        self.api.fail_at = 'batch'
        with self.assertRaises(runtime.UnknownResult): self.send()
        self.api.channels['1001']['type'] = 12
        self.assertIsNone(ongoing.Pipeline(self.root, self.config, self.api).latest())
        pages_before = len(self.api.store['1001'])
        with self.assertRaisesRegex(runtime.AppError, 'тип ветки'): self.send()
        self.assertEqual(len(self.api.store['1001']), pages_before)


if __name__ == '__main__':
    unittest.main()
