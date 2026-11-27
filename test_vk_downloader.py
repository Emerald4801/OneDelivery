import argparse
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent / 'vk_album_downloader'))
import download_album as app


class Response:
    def __init__(self, data, content_type='image/jpeg'):
        self.data = data
        self.headers = {'Content-Type': content_type}

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def raise_for_status(self): pass
    def iter_content(self, size): yield self.data


class Session:
    def __init__(self, response):
        self.headers = {}
        self.response = response
        self.calls = 0

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def get(self, *args, **kwargs):
        self.calls += 1
        return self.response


class Checks(unittest.TestCase):
    def test_vk_url_only(self):
        self.assertEqual(app.album_key(app.DEFAULT_URL), '-334424_310875314')
        for bad in ['https://vk.ru.evil.org/album1_2', 'http://vk.ru/album1_2', 'https://vk.ru/feed']:
            with self.assertRaises(ValueError): app.album_key(bad)

    def test_extraction_order_and_deduplication(self):
        photos = app.parse_photos(json.dumps([[2, {}], [3, 'https://cdn/2', {'id': 2}],
                                              [3, 'https://cdn/1', {'id': 1}],
                                              [3, 'https://cdn/2', {'id': 2}]]))
        self.assertEqual([p['id'] for p in photos], ['2', '1'])
        with self.assertRaises(ValueError): app.parse_photos('[]')

    def test_html_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(app.time, 'sleep'):
            target = Path(temp) / 'one.part'
            with self.assertRaises(RuntimeError):
                app.download_one(Session(Response(b'<html>login</html>', 'text/html')), 'https://cdn/1', target)
            self.assertFalse(target.exists())

    def test_download_resume_repair_cbz_and_changed_order(self):
        data = io.BytesIO()
        Image.new('RGB', (900, 1400), 'white').save(data, 'JPEG')
        session = Session(Response(data.getvalue()))
        photos = [{'id': '2', 'url': 'https://cdn/2'}, {'id': '1', 'url': 'https://cdn/1'}]
        with tempfile.TemporaryDirectory() as temp, patch.object(app, 'extract', return_value=photos), \
             patch('requests.Session', return_value=session), patch.object(app.time, 'sleep'):
            args = argparse.Namespace(url=app.DEFAULT_URL, output=temp, expected=2, cookies=None, browser=None)
            app.run(args)
            self.assertEqual(session.calls, 2)
            folder = Path(temp) / ('album' + app.album_key(args.url))
            state = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(state['files']['1']['width'], 900)
            with zipfile.ZipFile(next(folder.glob('*.cbz'))) as archive:
                self.assertEqual(archive.namelist(), ['0001.jpg', '0002.jpg'])
            self.assertNotIn('https://cdn', (folder / 'manifest.json').read_text(encoding='utf-8'))
            app.run(args)
            self.assertEqual(session.calls, 2)
            (folder / '0002.jpg').write_bytes(b'corrupt')
            app.run(args)
            self.assertEqual(session.calls, 3)
            with self.assertRaises(ValueError):
                app.load_manifest(folder / 'manifest.json', args.url, list(reversed(photos)))
            args.expected = 3
            with self.assertRaises(ValueError): app.run(args)


if __name__ == '__main__':
    unittest.main()
