import contextlib
from dataclasses import asdict
from datetime import datetime
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / 'vk_discord_publisher'))
import ongoing as app
from vk_source import Album, Snapshot, SourceError, parse_index, parse_snapshot, VKSource
from test_publisher_v3 import FakeDiscord


def snapshot(key='-334424_2', chapter=1200, count=2, ids=None):
    return Snapshot(Album(key, str(chapter), chapter, count), count,
                    [{'id': str(i), 'url': f'https://cdn.example/{i}.jpg', 'width': 1500, 'height': 2400}
                     for i in (range(1, count + 1) if ids is None else ids)])


def at(hour, minute=0, day=24):
    return datetime(2026, 11, day, hour, minute, tzinfo=app.MOSCOW).timestamp()


class ScheduleChecks(unittest.TestCase):
    def test_moscow_window_and_next_slots(self):
        self.assertFalse(app.discovery_open(at(11, 59)))
        self.assertTrue(app.discovery_open(at(12)))
        self.assertTrue(app.discovery_open(at(23, 59)))
        self.assertEqual(app.next_discovery(at(11)), at(12))
        self.assertEqual(app.next_discovery(at(12, 1)), at(12, 30))
        self.assertEqual(app.next_discovery(at(23, 59)), at(12, day=25))

    def test_three_samples_over_ten_minutes(self):
        observation = None
        for t in (0, 300):
            observation, ready = app.observe(observation, snapshot(), t)
            self.assertFalse(ready)
        observation, ready = app.observe(observation, snapshot(), 600)
        self.assertTrue(ready)

    def test_sleep_gap_or_clock_reversal_resets_evidence(self):
        for gap in (2000, -1):
            old, _ = app.observe(None, snapshot(), 0)
            current, ready = app.observe(old, snapshot(), gap)
            self.assertFalse(ready)
            self.assertEqual(current['samples'], 1)
            self.assertEqual(current['first'], gap)

    def test_equal_count_reordering_resets_evidence(self):
        old, _ = app.observe(None, snapshot(), 0)
        current, ready = app.observe(old, snapshot(ids=[2, 1]), 300)
        self.assertEqual(current['first'], 300)
        self.assertFalse(ready)

    def test_early_repeated_calls_do_not_add_samples(self):
        old, _ = app.observe(None, snapshot(), 0)
        for t in range(1, 300):
            old, ready = app.observe(old, snapshot(), t)
        self.assertEqual(old['samples'], 1)


class SourceChecks(unittest.TestCase):
    def test_actual_metadata_empty_and_complete(self):
        meta = {'album': {'id': 2, 'name': '1200', 'count': 2}, 'user': {'id': -334424}}
        raw = [[2, '', meta]] + [[3, f'https://cdn/{i}.jpg', dict(meta, id=i)] for i in (1, 2)]
        result = parse_snapshot(json.dumps(raw), '-334424_2')
        self.assertTrue(result.ready)
        self.assertEqual([p['id'] for p in result.photos], ['1', '2'])
        meta['album']['count'] = 0
        empty = parse_snapshot(json.dumps([[2, '', meta]]), '-334424_2')
        self.assertFalse(empty.ready)

    def test_same_number_different_ids_not_collapsed(self):
        raw = '<a href="/album-334424_1" title="1200">Нет фотографий</a>' \
              '<a href="/album-334424_2" title="1200">12 фотографий</a>'
        items, _, _ = parse_index(raw, '-334424', 'https://vk.ru/albums-334424')
        self.assertEqual([a.key for a in items], ['-334424_1', '-334424_2'])
        self.assertEqual([a.count for a in items], [0, 12])

    def test_signed_url_rotation_does_not_reset_stability(self):
        first, second = snapshot(), snapshot()
        second.photos[0]['url'] += '?sig=new'
        self.assertEqual(first.signature, second.signature)

    def test_login_redirect_is_error_not_no_chapter(self):
        with tempfile.TemporaryDirectory() as temp:
            source = VKSource(temp, {})
            self.addCleanup(source.close)
            response = types.SimpleNamespace(url='https://m.vk.ru/', text='', raise_for_status=lambda: None)
            with patch.object(source.session, 'get', return_value=response):
                with self.assertRaises(SourceError): source.albums()

    def test_recent_api_read_one_page_full_read_all_pages(self):
        with tempfile.TemporaryDirectory() as temp:
            source = VKSource(temp, {})
            self.addCleanup(source.close)
            calls = []
            def post(*args, **kwargs):
                self.assertFalse(kwargs['allow_redirects'])
                calls.append(kwargs['data']['offset'])
                return types.SimpleNamespace(is_redirect=False, raise_for_status=lambda: None, json=lambda: {
                    'response': {'count': 2, 'items': [{'owner_id': -334424, 'id': len(calls), 'title': str(1201-len(calls)), 'size': 12}]}})
            with patch.object(source.session, 'post', side_effect=post), patch('vk_source.time.sleep'):
                self.assertEqual(len(source.api_albums('fake')), 1)
                calls.clear()
                self.assertEqual(len(source.api_albums('fake', full=True)), 2)
                self.assertEqual(calls, [0, 1])


class FakePipeline:
    def __init__(self, root):
        self.directory = root / 'state'
        self.directory.mkdir()
        self.guild, self.channel, self.api = '100', '200', types.SimpleNamespace(bot_id='900')
        self.latest_number = 1199
        self.sent = []
        self.downloaded = []

    def latest(self): return self.latest_number
    def existing(self, number, count): return number == self.latest_number
    def download(self, snap): self.downloaded.append(snap); return self.directory
    def publish(self, snap, folder, *, origin='manual'): self.sent.append(snap); self.latest_number = snap.album.chapter


class FakeSource:
    owner = '-334424'
    def __init__(self):
        self.entries = {'-334424_1': snapshot('-334424_1', count=0), '-334424_2': snapshot()}
        self.calls = []
        self.failure = False
    def albums(self, full=False):
        self.calls.append(('index', full))
        if self.failure: raise SourceError('Нет доступа')
        return [s.album for s in self.entries.values()]
    def snapshot(self, album):
        self.calls.append(('snapshot', album.key))
        if self.failure: raise SourceError('Нет доступа')
        return self.entries[album.key]


class WatcherChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = FakeSource()
        self.pipeline = FakePipeline(self.root)
        self.worker = app.Watcher(self.root, {}, self.source, self.pipeline)

    def test_empty_duplicate_does_not_block_filled_album(self):
        for offset in (0, 300, 600, 900): self.worker.cycle(at(12) + offset)
        self.assertEqual([s.album.chapter for s in self.pipeline.sent], [1200])
        self.assertIn(('index', True), self.source.calls)

    def test_complete_scan_drops_deleted_cached_candidates(self):
        self.worker.state['known']=[asdict(snapshot('-334424_999').album)]
        self.source.last_scan_complete=True
        candidates=self.worker.discover(1200)
        self.assertNotIn('-334424_999',[a.key for a in candidates])

    def test_two_filled_albums_stop_before_download(self):
        self.source.entries['-334424_1'] = snapshot('-334424_1')
        with self.assertRaises(SourceError): self.worker.cycle(at(12))
        self.assertFalse(self.pipeline.downloaded)

    def test_only_empty_albums_wait_without_publish(self):
        self.source.entries.pop('-334424_2')
        self.worker.cycle(at(12))
        self.assertFalse(self.pipeline.sent)
        self.assertNotIn('observation', self.worker.state)

    def test_next_chapter_gap_is_not_skipped(self):
        self.source.entries = {'-334424_3': snapshot('-334424_3', chapter=1201)}
        self.worker.cycle(at(12))
        self.assertFalse(self.pipeline.sent)
        self.assertEqual(self.worker.state['expected'], 1200)

    def test_no_network_outside_window_without_active_candidate(self):
        self.assertEqual(self.worker.cycle(at(3)), at(12))
        self.assertFalse(self.source.calls)

    def test_empty_discord_at_night_also_waits_until_noon(self):
        self.pipeline.latest_number = None
        self.assertEqual(self.worker.cycle(at(3)), at(12))
        self.assertFalse(self.source.calls)

    def test_candidate_found_before_midnight_finishes_after_midnight(self):
        for offset in (0, 300, 600, 900): self.worker.cycle(at(23, 55) + offset)
        self.assertEqual(len(self.pipeline.sent), 1)

    def test_observation_reset_after_error_retains_candidate_after_midnight(self):
        self.worker.cycle(at(23, 55))
        self.worker.state.pop('observation')
        self.worker.cycle(at(0, day=25))
        self.assertTrue(self.worker.state.get('observation'))
        self.assertFalse(self.pipeline.sent)

    def test_restart_does_not_count_offline_time(self):
        self.worker.cycle(at(12))
        self.worker.save()
        worker = app.Watcher(self.root, {}, self.source, self.pipeline)
        self.assertNotIn('observation', worker.state)
        worker.cycle(at(13))
        self.assertFalse(self.pipeline.sent)

    def test_album_change_after_download_does_not_publish(self):
        for offset in (0, 300): self.worker.cycle(at(12) + offset)
        def changed(snap):
            self.source.entries[snap.album.key] = snapshot(ids=[2, 1])
            return self.pipeline.directory
        self.pipeline.download = changed
        self.worker.cycle(at(12, 10))
        self.assertFalse(self.pipeline.sent)

    def test_empty_channel_bootstrap_does_not_send_archive(self):
        self.pipeline.latest_number = None
        self.worker.cycle(at(12))
        self.assertFalse(self.pipeline.sent)
        self.assertEqual(self.worker.state['expected'], 1201)

    def test_source_error_does_not_advance_chapter(self):
        self.source.failure = True
        with self.assertRaises(SourceError): self.worker.cycle(at(12))
        self.assertEqual(self.worker.state['expected'], 1200)
        self.assertFalse(self.pipeline.sent)

    def test_missing_local_token_fails_without_prompt(self):
        with patch.dict('os.environ', {}, clear=True), patch.object(app.runtime.getpass, 'getpass') as prompt:
            with self.assertRaises(app.runtime.AppError): app.noninteractive_token(self.root)
            prompt.assert_not_called()


class PipelineChecks(unittest.TestCase):
    def test_verify_completed_discord_galleries_and_ignore_user_comments(self):
        api = FakeDiscord()
        api.store['200'] = [{'id':'1001', 'author':{'id':'900'}, 'content':'**ГЛАВА 1200**\nСтраниц: 2'}]
        api.channels['1001'] = {'id':'1001','parent_id':'200','guild_id':'100','type':11}
        api.store['1001'] = [
            {'id':'1002','author':{'id':'900'},'content':'Страницы 1-2','attachments':[
                {'filename':f'SPOILER_{n:04}.jpg','size':20} for n in (1,2)]},
            {'id':'1003','author':{'id':'OTHER'},'content':'Comment','attachments':[{'filename':'random.jpg'}]}]
        with tempfile.TemporaryDirectory() as temp:
            pipeline = app.Pipeline(temp, {'guild_id':'100','channel_id':'200'}, api)
            self.assertEqual(pipeline.latest(),1200)
            self.assertTrue(pipeline.existing(1200,2))
            api.store['1001'][0]['attachments'].reverse()
            self.assertIsNone(pipeline.latest())
            with self.assertRaises(app.runtime.AppError): pipeline.existing(1200,2)

    def test_read_errors_do_not_masquerade_as_deleted_thread(self):
        api = FakeDiscord()
        with tempfile.TemporaryDirectory() as temp:
            pipeline = app.Pipeline(temp, {'guild_id':'100','channel_id':'200'}, api)
            with patch.object(api, 'request', side_effect=app.runtime.ApiError(403)):
                with self.assertRaises(app.runtime.ApiError): pipeline.complete_thread({'id':'1001'},2)

    def test_legacy_card_is_recognized_without_rewriting_it(self):
        message = {'content':'**ГЛАВА 1199**\nhttps://vk.ru/album-334424_2\nФотографий: 12\nСтатус: опубликовано'}
        self.assertEqual(app.chapter_card(message), (1199,12))


class SecurityChecks(unittest.TestCase):
    def test_windows_mutex_blocks_other_installation(self):
        with app.channel_lock({'guild_id':'123','channel_id':'456'}):
            with self.assertRaises(app.runtime.AppError):
                with app.channel_lock({'guild_id':'123','channel_id':'456'}):
                    self.fail('second worker started')
        with app.channel_lock({'guild_id':'123','channel_id':'456'}):
            pass

    def test_cookie_import_filters_other_sites_and_writes_only_protected_bytes(self):
        import vk_session
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'export.txt'
            source.write_text('# Netscape HTTP Cookie File\n.vk.ru\tTRUE\t/\tTRUE\t0\tsession\tPRIVATE-VK-COOKIE\n.example.com\tTRUE\t/\tTRUE\t0\tother\tPRIVATE-OTHER-COOKIE\n')
            captured = []
            def protect(data, decrypt=False):
                if decrypt: return captured[0]
                captured.append(data)
                return b'protected-cookie-data'
            with patch.object(vk_session.runtime, 'protect', side_effect=protect):
                self.assertEqual(vk_session.import_file(source, root),1)
                self.assertEqual([(c.domain,c.name,c.value) for c in vk_session.load(root)], [('.vk.ru','session','PRIVATE-VK-COOKIE')])
            encrypted = (root / 'vk_cookies.dpapi').read_bytes()
            self.assertNotIn(b'PRIVATE-VK-COOKIE',encrypted)
            self.assertNotIn(b'PRIVATE-OTHER-COOKIE',encrypted)
            self.assertNotIn(b'PRIVATE-OTHER-COOKIE', captured[0])

    def test_dpapi_roundtrip_when_available(self):
        try:
            encrypted = app.runtime.protect(b'integration-test-secret')
        except app.runtime.AppError:
            self.skipTest('DPAPI is unavailable in this execution context')
        self.assertNotIn(b'integration-test-secret', encrypted)
        self.assertEqual(app.runtime.protect(encrypted, decrypt=True), b'integration-test-secret')

    def test_snapshot_credentials_pass_via_stdin_not_commandline(self):
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            source = VKSource(temp,{})
            self.addCleanup(source.close)
            source.session.cookies.set('session','PRIVATE-VK-COOKIE',domain='.vk.ru')
            meta = {'album': {'id':2,'name':'1200','count':0},'user':{'id':-334424}}
            output = types.SimpleNamespace(returncode=0,stdout=json.dumps([[2,'',meta]]))
            with patch('vk_source.subprocess.run', return_value=output) as run:
                source.snapshot(snapshot().album)
            args, kwargs = run.call_args
            self.assertNotIn('PRIVATE-VK-COOKIE',str(args))
            self.assertEqual(json.loads(kwargs['input'])['cookies']['session'],'PRIVATE-VK-COOKIE')
            self.assertEqual(kwargs['timeout'],600)

    def test_wrong_dependency_version_does_not_silently_upgrade(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'requirements.lock').write_text('requests==0.0.0\n')
            with self.assertRaises(app.runtime.AppError): app.verify_dependencies(root)

    def test_download_limit_stops_before_reading_oversized_body(self):
        class Response:
            headers = {'Content-Type':'image/jpeg','Content-Length':'1000'}
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def raise_for_status(self): pass
            def iter_content(self,*args): raise AssertionError('oversized body must not be read')
        session=types.SimpleNamespace(get=lambda *a,**k:Response())
        with tempfile.TemporaryDirectory() as temp, patch('download_album.time.sleep'):
            target=Path(temp)/'image.part'
            with self.assertRaises(RuntimeError): app.download.download_one(session,'https://cdn/image',target,max_bytes=100)
            self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
