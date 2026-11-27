import io
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import requests
sys.path.insert(0, str(Path(__file__).parent / 'vk_discord_publisher'))
import vk_api
import vk_source
import probe_vk_api


def photo(ident, owner=-334424, album=2):
    return {'owner_id':owner,'album_id':album,'id':ident,'sizes':[
        {'type':'s','url':'https://cdn/small','width':75,'height':75},
        {'type':'w','url':'https://cdn/large','width':1650,'height':2400}]}


class ApiChecks(unittest.TestCase):
    def setUp(self):
        self.client = vk_api.Client('PRIVATE-TEST-TOKEN')
        self.addCleanup(self.client.close)
        self.clock = patch('vk_api.time.sleep')
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_token_only_in_post_body_no_redirect_or_raw_error_logging(self):
        response=types.SimpleNamespace(is_redirect=False,raise_for_status=lambda:None,
            json=lambda:{'error':{'error_code':1051,'error_msg':'PRIVATE-TEST-TOKEN','request_params':[{'access_token':'PRIVATE-TEST-TOKEN'}]}})
        with patch.object(self.client.session,'post',return_value=response) as post:
            with self.assertRaises(vk_api.VKAPIError) as caught:self.client.albums(-334424)
        self.assertEqual(caught.exception.code,1051)
        self.assertNotIn('PRIVATE-TEST-TOKEN',str(caught.exception))
        args,kwargs=post.call_args
        self.assertNotIn('PRIVATE-TEST-TOKEN',args[0])
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['data']['access_token'],'PRIVATE-TEST-TOKEN')

    def test_redirect_never_followed(self):
        response=types.SimpleNamespace(is_redirect=True)
        with patch.object(self.client.session,'post',return_value=response):
            with self.assertRaises(vk_api.VKAPIError):self.client.albums(-334424)

    def test_transient_network_read_retries_but_does_not_accept_failure_as_empty(self):
        with patch.object(self.client.session,'post',side_effect=requests.Timeout):
            with self.assertRaises(vk_api.VKAPIError):self.client.albums(-334424)

    def test_rate_limit_then_success(self):
        def response(payload):return types.SimpleNamespace(is_redirect=False,raise_for_status=lambda:None,json=lambda:payload)
        with patch.object(self.client.session,'post',side_effect=[response({'error':{'error_code':6}}),response({'response':{'count':0,'items':[]}})]):
            self.assertEqual(self.client.albums(-334424),[])

    def test_all_photo_pages_preserve_order_and_take_largest_size(self):
        pages=[{'count':3,'items':[photo(5),photo(3)]},{'count':3,'items':[photo(9)]}]
        with patch.object(self.client,'call',side_effect=pages) as call:
            result=self.client.photos(-334424,2)
        self.assertEqual([p['id'] for p in result],['5','3','9'])
        self.assertEqual(result[0]['url'],'https://cdn/large')
        self.assertEqual(call.call_args_list[1].args[1]['offset'],2)

    def test_photo_from_other_album_rejected(self):
        with patch.object(self.client,'call',return_value={'count':1,'items':[photo(1,album=3)]}):
            with self.assertRaises(vk_api.VKAPIError):self.client.photos(-334424,2)

    def test_duplicate_photo_or_changed_counter_rejected(self):
        for pages in [
            [{'count':2,'items':[photo(1),photo(1)]}],
            [{'count':2,'items':[photo(1)]},{'count':3,'items':[photo(2)]}]]:
            with self.subTest(pages=pages),patch.object(self.client,'call',side_effect=pages):
                with self.assertRaises(vk_api.VKAPIError):self.client.photos(-334424,2)

    def test_empty_album_is_valid_empty_snapshot_input(self):
        with patch.object(self.client,'call',return_value={'count':0,'items':[]}):
            self.assertEqual(self.client.photos(-334424,2),[])

    def test_write_method_is_not_available(self):
        with self.assertRaises(vk_api.VKAPIError):self.client.call('wall.post',{})


class IntegrationChecks(unittest.TestCase):
    def test_saved_key_alone_does_not_change_source(self):
        with tempfile.TemporaryDirectory() as temp,patch('vk_source.vk_api.read_token') as read:
            source=vk_source.VKSource(temp,{})
            self.addCleanup(source.close)
            self.assertIsNone(source.api_client)
            read.assert_not_called()

    def test_regular_api_discovery_reads_all_pages(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'VK_API_TOKEN':'PRIVATE-TEST-TOKEN'}):
            source=vk_source.VKSource(temp,{'vk_mode':'api'})
            self.addCleanup(source.close)
            with patch.object(source.api_client,'albums',return_value=[]) as albums:
                self.assertEqual(source.albums(),[])
            albums.assert_called_once_with('-334424',True)
            self.assertTrue(source.last_scan_complete)

    def receipt(self, root):
        receipt={'source':'official_vk_api','owner':'-334424','album':'-334424_310875314',
                 'api_read_verified':True,'token_saved':True,'discord_modified':False,
                 'photo_count':12,'order_matches_reference':True,
                 'first_image':{'format':'jpg','width':1650,'height':2400}}
        (root/'diagnostics').mkdir()
        (root/'diagnostics/vk_api_probe.json').write_text(json.dumps(receipt))
        (root/'vk_api_token.dpapi').write_bytes(b'protected-test-placeholder')
        (root/'config.json').write_text(json.dumps({'guild_id':'100','channel_id':'200'}))
        return receipt

    def test_successful_existing_report_can_select_api_without_token_prompt(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            self.receipt(root)
            with patch('vk_api.runtime.protect') as decrypt,patch('probe_vk_api.getpass.getpass') as prompt:
                probe_vk_api.activate_api(root)
            cfg=json.loads((root/'config.json').read_text())
            self.assertEqual(cfg,{'guild_id':'100','channel_id':'200','vk_mode':'api','vk_api_rev':0})
            decrypt.assert_not_called()
            prompt.assert_not_called()

    def test_unverified_order_cannot_activate_api(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            receipt=self.receipt(root)
            receipt['order_matches_reference']=None
            (root/'diagnostics/vk_api_probe.json').write_text(json.dumps(receipt))
            with self.assertRaises(vk_api.VKAPIError):probe_vk_api.activate_api(root)
            self.assertNotIn('vk_mode',json.loads((root/'config.json').read_text()))

    def test_receipt_does_not_activate_other_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            self.receipt(root)
            (root/'config.json').write_text(json.dumps({'vk_albums_url':'https://vk.ru/albums-999'}))
            with self.assertRaises(vk_api.VKAPIError):probe_vk_api.activate_api(root)
            self.assertNotIn('vk_mode',json.loads((root/'config.json').read_text()))

    def test_api_mode_uses_no_browser_cookies_or_gallery_dl(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'VK_API_TOKEN':'PRIVATE-TEST-TOKEN'}),patch('vk_source.vk_session.load') as cookies:
            source=vk_source.VKSource(temp,{'vk_mode':'api','vk_browser':'chrome'})
            self.addCleanup(source.close)
            meta={'id':2,'owner_id':-334424,'title':'1200','size':1,'updated':123}
            with patch.object(source.api_client,'album',return_value=meta),patch.object(source.api_client,'photos',return_value=[{'id':'1','url':'https://cdn/a','width':1650,'height':2400}]),patch('vk_source.subprocess.run') as child:
                result=source.snapshot(vk_source.Album('-334424_2','1200',1200,1))
            self.assertTrue(result.ready)
            cookies.assert_not_called()
            child.assert_not_called()

    def test_album_change_between_reads_is_not_ready(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'VK_API_TOKEN':'PRIVATE-TEST-TOKEN'}):
            source=vk_source.VKSource(temp,{'vk_mode':'api'})
            self.addCleanup(source.close)
            meta={'id':2,'owner_id':-334424,'title':'1200','size':1,'updated':123}
            with patch.object(source.api_client,'album',side_effect=[meta,dict(meta,size=2)]),patch.object(source.api_client,'photos',return_value=[]):
                with self.assertRaises(vk_source.SourceError):source.snapshot(vk_source.Album('-334424_2','1200',1200,1))

    def test_api_error_does_not_fall_back_to_html(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'VK_API_TOKEN':'PRIVATE-TEST-TOKEN'}):
            source=vk_source.VKSource(temp,{'vk_mode':'api'})
            self.addCleanup(source.close)
            with patch.object(source.api_client,'albums',side_effect=vk_api.VKAPIError('Denied',1051)),patch.object(source,'web_albums') as web:
                with self.assertRaises(vk_source.SourceError):source.albums()
            web.assert_not_called()

    def test_order_mismatch_fails_before_download(self):
        client=types.SimpleNamespace(albums=lambda owner:[{}],album=lambda owner,ident:{'title':'1199','size':2},
                                    photos=lambda owner,ident:[{'id':'2'},{'id':'1'}])
        with tempfile.TemporaryDirectory() as temp,patch('probe_vk_api.reference_manifest',return_value={'photo_ids':['1','2']}),patch('probe_vk_api.download.download_one') as download,contextlib.redirect_stdout(io.StringIO()):
            report={}
            with self.assertRaises(vk_api.VKAPIError):probe_vk_api.verify(client,Path(temp),report)
            self.assertFalse(report['order_matches_reference'])
            download.assert_not_called()

    def test_failed_key_is_not_saved_and_report_has_no_secret(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{},clear=True),patch.object(sys,'argv',['probe','--root',temp]),patch('probe_vk_api.getpass.getpass',return_value='PRIVATE-TEST-TOKEN'),patch('probe_vk_api.verify',side_effect=vk_api.VKAPIError('photos.get: ошибка VK API 1051.',1051)),patch('probe_vk_api.vk_api.save_token') as save,contextlib.redirect_stdout(io.StringIO()):
            result=probe_vk_api.main()
            save.assert_not_called()
            raw=(Path(temp)/'diagnostics/vk_api_probe.json').read_text(encoding='utf-8')
            self.assertNotIn('PRIVATE-TEST-TOKEN',raw)
            self.assertEqual(json.loads(raw)['api_error_code'],1051)
            self.assertEqual(result,1)


if __name__=='__main__':unittest.main()
