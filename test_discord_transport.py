"""Behavior shared by the single-page and spoiler-gallery Discord transports."""
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import requests
import test_ongoing  # canonical application import path
import publisher_runtime as runtime
import publish_album_v3 as gallery


def response(status=200, body=None, headers=None):
    return types.SimpleNamespace(status_code=status, content=b'json', headers=headers or {},
                                 json=lambda: {} if body is None else body)


class TransportChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.files = [Path(self.temp.name) / f'{n:04}.jpg' for n in (1, 2)]
        for index, path in enumerate(self.files): path.write_bytes(b'local-photo-' + str(index).encode())
        patcher = patch.object(runtime, 'wait')
        patcher.start()
        self.addCleanup(patcher.stop)

    def api(self, cls):
        api = cls('test-only')
        self.addCleanup(api.close)
        return api

    def send(self, api):
        files = self.files if isinstance(api, gallery.Discord) else self.files[0]
        return api.post_message('200', 'local test', 'stable-test-identity', files)

    def test_rate_limit_reopens_all_streams_and_preserves_payload_and_timeouts(self):
        for cls, timeout in ((runtime.Discord, 120), (gallery.Discord, 180)):
            with self.subTest(cls=cls.__name__, timeout=timeout):
                api = self.api(cls)
                seen, opened = [], []
                def request(method, url, **options):
                    self.assertEqual(options['timeout'], (20, timeout))
                    self.assertFalse(options['allow_redirects'])
                    payload = json.loads(options['data']['payload_json'])
                    streams = [item[1] for item in options['files'].values()]
                    seen.append((payload, [(item[0], item[1].read()) for item in options['files'].values()]))
                    opened.append(streams)
                    return response(429, {'retry_after': 0.01}) if len(seen) == 1 else response(body={'id':'1'})
                with patch.object(api.session, 'request', side_effect=request):
                    self.assertEqual(self.send(api), {'id':'1'})
                self.assertEqual(seen[0], seen[1])
                self.assertTrue(seen[0][0]['flags'] & 4096)
                self.assertTrue(seen[0][0]['enforce_nonce'])
                self.assertTrue(all(stream.closed for group in opened for stream in group))
                self.assertTrue(all(first is not second for first, second in zip(*opened)))
                if cls is gallery.Discord:
                    self.assertTrue(all(name.startswith('SPOILER_') for name, _ in seen[0][1]))

    def test_lost_post_and_server_post_error_never_resend_and_always_close_files(self):
        for cls in (runtime.Discord, gallery.Discord):
            for failure in ('network', 'server'):
                with self.subTest(cls=cls.__name__, failure=failure):
                    api, opened = self.api(cls), []
                    def request(*args, **options):
                        opened.extend(item[1] for item in options['files'].values())
                        if failure == 'network': raise requests.Timeout('lost response')
                        return response(503)
                    with patch.object(api.session, 'request', side_effect=request) as call:
                        with self.assertRaises(runtime.UnknownResult): self.send(api)
                    self.assertEqual(call.call_count, 1)
                    self.assertTrue(opened and all(stream.closed for stream in opened))

    def test_idempotent_requests_retry_server_errors_with_same_json_and_parameters(self):
        for cls in (runtime.Discord, gallery.Discord):
            for method in ('GET', 'PATCH'):
                api = self.api(cls)
                with patch.object(api.session, 'request', side_effect=[response(503),response(body={'id':'ok'})]) as call:
                    self.assertEqual(api.request(method,'/test',{'content':'same'},params={'limit':1}),{'id':'ok'})
                self.assertEqual(call.call_count,2)
                self.assertEqual(call.call_args_list[0],call.call_args_list[1])

    def test_permission_failure_stops_without_retry_and_keeps_http_code(self):
        for cls in (runtime.Discord, gallery.Discord):
            api=self.api(cls)
            with patch.object(api.session,'request',return_value=response(403,{'code':50013})) as call:
                with self.assertRaises(runtime.ApiError) as caught: self.send(api)
            self.assertEqual((caught.exception.status,caught.exception.code),(403,50013))
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(call.call_count,1)

    def test_rate_limit_non_object_body_uses_header_in_both_transports(self):
        for cls in (runtime.Discord,gallery.Discord):
            api=self.api(cls)
            with patch.object(runtime.time,'monotonic',return_value=100), \
                 patch.object(api.session,'request',side_effect=[response(429,[],{'Retry-After':'2'}),response(body=['ok'])]) as call:
                self.assertEqual(api.request('GET','/test'),['ok'])
            self.assertAlmostEqual(api.ready_at,102.2)
            self.assertEqual(call.call_count,2)

    def test_partial_multipart_open_failure_closes_earlier_files_without_http_call(self):
        api=self.api(gallery.Discord)
        actual=Path.open
        opened=[]
        def tracked_open(path,*args,**kwargs):
            result=actual(path,*args,**kwargs)
            opened.append(result)
            return result
        self.files[1].unlink()
        with patch.object(Path,'open',tracked_open),patch.object(api.session,'request') as call:
            with self.assertRaises(FileNotFoundError): self.send(api)
        call.assert_not_called()
        self.assertTrue(opened and all(stream.closed for stream in opened))

    def test_invalid_routes_and_too_many_attachments_fail_before_network(self):
        for cls in (runtime.Discord,gallery.Discord):
            api=self.api(cls)
            with patch.object(api.session,'request') as call:
                with self.assertRaises(runtime.AppError): api.request('GET','https://example.invalid')
                call.assert_not_called()
        api=self.api(gallery.Discord)
        with patch.object(api.session,'request') as call:
            with self.assertRaises(runtime.AppError): api.post_message('200','test','identity',[self.files[0]]*11)
            call.assert_not_called()

    def test_retry_exhaustion_is_temporary_and_bounded(self):
        for cls in (runtime.Discord,gallery.Discord):
            api=self.api(cls)
            with patch.object(api.session,'request',return_value=response(429,{'retry_after':1})) as call:
                with self.assertRaises(runtime.TransientError): api.request('GET','/test')
            self.assertEqual(call.call_count,5)


if __name__ == '__main__':
    unittest.main()
