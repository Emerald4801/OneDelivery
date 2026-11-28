"""Durable recovery decisions with fake transports and real application state."""
import contextlib
import copy
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import requests

import test_interrupted_resume as interrupted
from test_ongoing import FakeSource, at
import test_lifecycle as lifecycle
import ongoing
import publish_album_v3 as publisher
from publish_control import ControlError
import failure_policy as policy
import vk_api
import vk_source


class ClassificationChecks(unittest.TestCase):
    def test_network_and_http_decisions_do_not_depend_on_error_text(self):
        self.assertTrue(policy.should_retry(requests.Timeout('anything')))
        self.assertTrue(policy.should_retry(requests.ConnectionError('anything')))
        self.assertTrue(policy.should_retry(requests.exceptions.ChunkedEncodingError('truncated response')))
        self.assertFalse(policy.should_retry(requests.exceptions.SSLError('offline')))
        for status in (400, 401, 403, 404, 408, 413, 429, 500, 502, 503):
            with self.subTest(status=status):
                response = requests.Response()
                response.status_code = status
                error = requests.HTTPError('do not inspect this text', response=response)
                self.assertEqual(policy.should_retry(error), status in (408, 429, 500, 502, 503))

    def test_unknown_and_integrity_errors_block_instead_of_retrying_forever(self):
        for error in (ongoing.runtime.AppError('Discord offline'), ValueError('bad state'),
                      OSError('disk full'), RuntimeError('unexpected'), vk_source.SourceError('conflict')):
            self.assertFalse(policy.should_retry(error))
        self.assertTrue(policy.should_retry(ongoing.runtime.UnknownResult('response lost')))
        self.assertTrue(policy.should_retry(ongoing.runtime.TransientError('offline')))
        self.assertTrue(policy.should_retry(vk_source.SourceError('album changed', retryable=True)))
        for status in (401, 403, 404, 413, 429, 503):
            self.assertEqual(policy.should_retry(ongoing.runtime.ApiError(status)), status in (429, 503))

    def test_send_lock_contention_is_temporary_and_not_a_corrupt_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with ongoing.runtime.exclusive(root):
                with self.assertRaises(ongoing.runtime.TransientError) as caught:
                    with ongoing.runtime.exclusive(root): pass
            self.assertTrue(policy.should_retry(caught.exception))

    def test_external_error_messages_do_not_expose_urls_or_response_bodies(self):
        secret = 'secret-example-token'
        for error in (requests.ConnectionError('https://cdn.example/?token=' + secret),
                      requests.HTTPError('server echoed ' + secret), OSError('private file ' + secret)):
            self.assertNotIn(secret, policy.failure_message(error))

    def test_vk_transport_and_source_wrapper_preserve_retry_decision(self):
        session = types.SimpleNamespace(post=Mock(side_effect=requests.ConnectionError('signed URL')))
        client = vk_api.Client('test-only', session)
        with patch.object(vk_api.time, 'sleep'), self.assertRaises(vk_api.VKAPIError) as caught:
            client.call('photos.getAlbums', {})
        self.assertTrue(policy.should_retry(caught.exception))
        self.assertEqual(session.post.call_count, 4)
        with tempfile.TemporaryDirectory() as temporary, patch.object(vk_api, 'read_token', return_value='test-only'):
            source = vk_source.VKSource(Path(temporary), dict(vk_mode='api'))
            self.addCleanup(source.close)
            for temporary_error in (True, False):
                with patch.object(source.api_client, 'albums', side_effect=vk_api.VKAPIError('safe', retryable=temporary_error)):
                    with self.assertRaises(vk_source.SourceError) as wrapped:
                        source.api_albums('test-only')
                self.assertEqual(policy.should_retry(wrapped.exception), temporary_error)

    def test_download_preserves_network_failure_and_stops_on_invalid_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / '0001.part'
            session = types.SimpleNamespace(get=Mock(side_effect=requests.ConnectionError('secret URL')))
            with patch.object(ongoing.download.time, 'sleep'), self.assertRaises(ongoing.download.DownloadError) as caught:
                ongoing.download.download_one(session, 'https://example.invalid/image', path)
            self.assertTrue(policy.should_retry(caught.exception))
            self.assertEqual(session.get.call_count, 3)
            response = types.SimpleNamespace(headers={'Content-Type': 'text/html'}, raise_for_status=lambda: None)
            session.get = Mock(side_effect=lambda *a, **kw: contextlib.nullcontext(response))
            with self.assertRaises(ongoing.download.DownloadError) as caught:
                ongoing.download.download_one(session, 'https://example.invalid/image', path)
            self.assertFalse(policy.should_retry(caught.exception))
            self.assertEqual(session.get.call_count, 1)

    def test_discord_unknown_post_is_not_blindly_repeated_inside_transport(self):
        for cls in (ongoing.runtime.Discord, publisher.Discord):
            api = cls('test-only')
            self.addCleanup(api.close)
            for error in (requests.Timeout('lost'), requests.exceptions.ChunkedEncodingError('lost')):
                with patch.object(api.session, 'request', side_effect=error) as request, \
                     patch.object(ongoing.runtime, 'wait'), patch.object(publisher, 'wait'):
                    with self.assertRaises(ongoing.runtime.UnknownResult): api.request('POST', '/test', {})
                    self.assertEqual(request.call_count, 1)
                    with self.assertRaises(ongoing.runtime.TransientError): api.request('GET', '/test')
                    self.assertEqual(request.call_count, 6)
            for method in ('GET', 'POST'):
                with patch.object(api.session, 'request', side_effect=requests.exceptions.SSLError('invalid certificate')) as request:
                    with self.assertRaises(ongoing.runtime.AppError) as caught: api.request(method, '/test', {})
                    self.assertFalse(policy.should_retry(caught.exception))
                    self.assertEqual(request.call_count, 1)


class DurableRecoveryChecks(unittest.TestCase):
    setUp = interrupted.InterruptedResumeChecks.setUp
    state_path = interrupted.InterruptedResumeChecks.state_path
    reopen = interrupted.InterruptedResumeChecks.reopen
    tick = interrupted.InterruptedResumeChecks.tick
    interrupt = interrupted.InterruptedResumeChecks.interrupt
    assert_finished = interrupted.InterruptedResumeChecks.assert_finished

    def test_manual_retry_keeps_pause_id_and_target_then_finishes_without_resume(self):
        before = self.interrupt()
        job = self.control.snapshot()['manual']
        self.assertEqual(job['status'], 'running')
        self.assertTrue(self.control.snapshot()['paused'])
        self.assertFalse(self.tick(899))
        self.tick(900)
        self.assert_finished(before)
        self.assertEqual(self.control.snapshot()['manual']['id'], job['id'])
        self.assertTrue(self.control.snapshot()['paused'])
        self.assertIsNone(self.control.snapshot().get('recovery'))

    def test_restart_keeps_absolute_retry_deadline_and_manual_operation(self):
        before = self.interrupt()
        state = self.control.snapshot()
        self.reopen()
        self.assertFalse(self.tick(899))
        self.assertEqual(self.control.snapshot()['recovery']['retry_at'], at(12) + 900)
        self.tick(900)
        self.assert_finished(before)
        self.assertEqual(self.control.snapshot()['manual']['id'], state['manual']['id'])
        self.assertTrue(self.control.snapshot()['paused'])

    def test_retry_now_reuses_same_job_without_enabling_auto(self):
        before = self.interrupt()
        job = self.control.snapshot()['manual']
        self.assertEqual(self.control.request_manual()['id'], job['id'])
        self.tick(601)
        self.assert_finished(before)
        self.assertTrue(self.control.snapshot()['paused'])

    def test_cancel_waiting_operation_preserves_receipt_and_allows_a_later_manual_retry(self):
        before = self.interrupt()
        original = self.state_path().read_bytes()
        old_id = self.control.snapshot()['manual']['id']
        self.control.cancel_manual()
        calls = list(self.source.calls)
        self.assertFalse(self.tick(900))
        self.assertEqual(self.source.calls, calls)
        self.assertEqual(self.state_path().read_bytes(), original)
        self.assertTrue(self.control.snapshot()['manual']['cancelled'])
        self.assertTrue(self.control.snapshot()['paused'])
        self.assertNotEqual(self.control.request_manual()['id'], old_id)
        self.tick(901)
        self.assert_finished(before)

    def test_cancellation_winning_send_lock_race_prevents_all_cycle_requests(self):
        self.interrupt()
        @contextlib.contextmanager
        def cancelled_before_cycle():
            self.control.cancel_manual()
            yield
        self.controller.exclusive = cancelled_before_cycle
        calls = list(self.source.calls)
        mutations = copy.deepcopy(self.api.mutations)
        self.assertFalse(self.tick(900))
        self.assertEqual(self.source.calls, calls)
        self.assertEqual(self.api.mutations, mutations)
        self.assertIsNone(self.control.snapshot().get('recovery'))

    def test_permission_failure_survives_restart_and_requires_explicit_retry(self):
        before = self.interrupt()
        self.controller.handle_failure(ongoing.runtime.ApiError(403), at(12) + 900)
        state = self.control.snapshot()
        self.assertEqual(state['recovery']['status'], 'blocked')
        self.assertEqual(state['manual']['status'], 'failed')
        self.assertTrue(state['paused'])
        mutations = copy.deepcopy(self.api.mutations)
        self.reopen()
        self.assertFalse(self.tick(10000))
        self.assertEqual(self.api.mutations, mutations)
        self.control.request_manual()
        self.tick(10001)
        self.assert_finished(before)

    def test_corrupt_cached_page_blocks_without_endless_retries(self):
        self.interrupt()
        (self.cached / '0013.jpg').write_bytes(b'damaged')
        with self.assertRaises(ongoing.runtime.AppError) as caught: self.tick(900)
        self.controller.handle_failure(caught.exception, at(12) + 1200)
        self.assertEqual(self.control.snapshot()['recovery']['status'], 'blocked')
        mutations = copy.deepcopy(self.api.mutations)
        self.assertFalse(self.tick(2000))
        self.assertEqual(self.api.mutations, mutations)
        self.assertIsNotNone(self.control.snapshot().get('expected_override'))

    def test_late_network_failure_cannot_resurrect_a_cancelled_operation(self):
        self.interrupt()
        self.control.cancel_manual()
        recovery = self.controller.handle_failure(ongoing.runtime.TransientError('late response'), at(12) + 1200)
        self.assertIsNone(recovery)
        self.assertIsNone(self.control.snapshot().get('recovery'))
        self.assertTrue(self.control.snapshot()['manual']['cancelled'])

    def test_pending_gallery_in_empty_discord_resumes_without_a_local_chapter_counter(self):
        self.control.set_expected(None)
        self.worker.state['empty_baseline'] = 1198
        before = self.interrupt(manual=False)
        self.worker.state.pop('empty_baseline', None)
        self.worker.save()
        self.reopen()
        self.tick(900)
        self.assert_finished(before)

    def test_multiple_pending_galleries_block_instead_of_selecting_one(self):
        self.interrupt()
        ongoing.download.atomic_json(self.pipeline.directory / '-334424_999.json', dict(
            album='-334424_999', chapter='1200', bot_id='900', card_id='9000', schema=3))
        mutations = copy.deepcopy(self.api.mutations)
        with self.assertRaisesRegex(ongoing.runtime.AppError, 'несколько незавершённых глав'):
            self.tick(900)
        self.assertEqual(self.api.mutations, mutations)

    def test_pending_gallery_is_finished_before_following_a_newer_complete_chapter(self):
        self.interrupt(manual=False)
        self.control.set_expected(None)
        newer = dict(id='9000', author={'id': '900'}, attachments=[],
                     content=publisher.card_text('1201', 1, '100', '9000'))
        self.api.store['200'].append(newer)
        self.api.channels['9000'] = dict(id='9000', parent_id='200', guild_id='100', type=11)
        self.api.store['9000'] = [dict(id='9001', author={'id': '900'}, content='Страницы 1',
                                      attachments=[dict(filename='SPOILER_0001.jpg', size=10)])]
        self.tick(900)
        self.assertTrue(json.loads(self.state_path().read_text(encoding='utf-8'))['complete'])
        self.assertEqual(self.worker.state['expected'], 1202)
        self.assertEqual(len(self.api.store['200']), 2)
        self.assertEqual(len(self.api.store['1001']), 3)
        self.assertEqual(self.api.store['200'][-1], newer)

    def test_started_automatic_gallery_can_finish_after_midnight(self):
        before = self.interrupt(manual=False)
        self.tick(at(0, 5, day=25) - at(12))
        receipt = self.assert_finished(before)
        self.assertEqual(receipt['publication_origin'], 'automatic')
        self.assertFalse(self.control.snapshot()['paused'])
        self.assertEqual(self.worker.state['cooldown_until'], receipt['completed_at'] + 72 * 3600)

    def test_missing_pending_source_blocks_instead_of_reporting_no_new_chapter(self):
        self.interrupt()
        self.source.entries.clear()
        with self.assertRaisesRegex(ongoing.runtime.AppError, 'Исходный альбом незавершённой главы не найден'):
            self.tick(900)

    def test_invalid_recovery_deadline_is_rejected_without_deleting_state(self):
        self.interrupt()
        original = self.control.path.read_bytes()
        for deadline in (float('inf'), float('nan'), True, 'tomorrow'):
            state = json.loads(original)
            state['recovery']['retry_at'] = deadline
            data = json.dumps(state).encode('utf-8')
            self.control.path.write_bytes(data)
            with self.assertRaises(ControlError): self.control.snapshot()
            self.assertEqual(self.control.path.read_bytes(), data)


class StartupPolicyChecks(lifecycle.ProfileChecks):
    boot = lifecycle.StartupChecks.boot

    def test_persisted_block_prevents_token_reads_and_network_at_restart(self):
        self.control.record_failure('No rights')
        result = self.boot(AssertionError('Must not contact Discord'))
        self.assertEqual(result, (1, 0, 0, 0))

    def test_queued_manual_request_survives_startup_network_outage(self):
        job = self.control.request_manual()
        source = FakeSource()
        source.close = lambda: None
        with patch.object(ongoing.Pipeline, 'latest', return_value=1199), \
             patch.object(ongoing.Pipeline, 'existing', return_value=False):
            result = self.boot([ongoing.runtime.TransientError('offline'), None], once=False,
                               stop_after_tick=True, source_override=source)
        self.assertEqual(result[:3], (0, 2, 60))
        state = self.control.snapshot()
        self.assertEqual(state['manual']['id'], job['id'])
        self.assertEqual(state['manual']['status'], 'running')
        self.assertTrue(state['paused'])
        self.assertIsNone(state.get('recovery'))
        self.assertTrue(source.calls)

    def test_resume_after_waking_a_blocked_worker_does_not_lose_the_start_request(self):
        self.control.record_failure('Fix permissions first')
        def resume(): self.control.pause(False)
        with patch.object(ongoing.Controller, 'tick', lambda controller, now: False):
            result = self.boot(once=False, stop_after_tick=True, sleep_action=resume)
        self.assertEqual(result, (0, 1, 1, 1))
        self.assertFalse(self.control.snapshot()['paused'])
        self.assertIsNone(self.control.snapshot().get('recovery'))


if __name__ == '__main__':
    unittest.main()
