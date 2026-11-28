"""A generated delta must accept prior supported packages, including the full bundle."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import build_package
import update_settings


class BuildCompatibilityChecks(unittest.TestCase):
    def test_previous_recovery_package_is_accepted_by_the_generated_delta(self):
        project = Path(__file__).resolve().parent.parent
        previous = subprocess.check_output(['git', '-c', 'safe.directory=' + project.as_posix(),
            'show', 'e3f3d68:vk_discord_publisher/ongoing.py'], cwd=project)
        with tempfile.TemporaryDirectory(prefix='OneDelivery delta build ') as temporary:
            root = Path(temporary)
            app, package, installed = root / 'app', root / 'package', root / 'installed'
            for folder in (app, package, installed): folder.mkdir()
            for name in update_settings.FILES:
                (app / name).write_bytes(build_package.source_bytes(name))
                (installed / name).write_bytes(previous if name == 'ongoing.py' else build_package.source_bytes(name))
            (app / 'SOURCE_IMPORT.json').write_bytes((project / 'vk_discord_publisher' / 'SOURCE_IMPORT.json').read_bytes())
            (package / 'supported_sources.json').write_bytes((Path(__file__).parent / 'supported_sources.json').read_bytes())
            for name in ('update_settings.py', 'README.txt'):
                (package / name).write_text('test fixture\n', encoding='utf-8')
            (root / 'UPDATE_ONEDELIVERY.cmd').write_text('@echo off\n', encoding='utf-8')
            with patch.object(build_package, 'APP', app), patch.object(build_package, 'HERE', package), \
                 patch.object(build_package, 'PROJECT', root), \
                 patch.object(build_package.subprocess, 'check_output', return_value=b'test-commit'), \
                 contextlib.redirect_stdout(io.StringIO()):
                build_package.build()
            manifest = json.loads((package / 'package.json').read_text(encoding='utf-8'))
            self.assertIn(update_settings.digest(previous), manifest['files']['ongoing.py']['compatible_before'])
            self.assertTrue(update_settings.prepare(installed, package, root / 'backup'))
            update_settings.apply_files(installed, package, root / 'backup')
            self.assertEqual((installed / 'ongoing.py').read_bytes(), (app / 'ongoing.py').read_bytes())


if __name__ == '__main__':
    unittest.main()
