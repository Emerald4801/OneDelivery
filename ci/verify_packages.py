"""Build and exercise both archives using only temporary copies from Git."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

PROJECT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(PROJECT / path) for path in ('onedelivery_update', 'vk_discord_publisher')]
import app_version
import build_full_package
import build_package
from full_package import FULL_FILES, PRESERVED_FILES
from full_install import ARCHIVE_FILES
from update_settings import FILES, prepare, apply_files, restore_files

BASELINES = ('installed-2026-11-28', 'tested-2026-11-28', 'tested-status-2026-11-28',
             'tested-recovery-2026-11-28', 'tested-diagnostics-2026-11-28', 'e3f3d68')


def git(*args):
    return subprocess.check_output(['git', '-c', 'safe.directory=' + PROJECT.as_posix(), *args], cwd=PROJECT)


def check(condition, message):
    if not condition:
        raise ValueError(message)


def verify(archive, names, head):
    with tempfile.TemporaryDirectory(prefix='onedelivery_package_check_') as temporary:
        work = Path(temporary)
        with zipfile.ZipFile(archive) as source:
            expected = ARCHIVE_FILES if names == FULL_FILES else {
                'UPDATE_ONEDELIVERY.cmd', 'onedelivery_update/update_settings.py',
                'onedelivery_update/package.json', 'onedelivery_update/README.txt',
                *('onedelivery_update/payload/' + name for name in names)}
            members = source.namelist()
            check(len(members) == len(set(members)) and set(members) == expected, 'Invalid archive paths')
            check(source.testzip() is None, 'Archive CRC failure')
            source.extractall(work / 'package')  # paths already checked against the exact whitelist
        package = work / 'package' / 'onedelivery_update'
        manifest = json.loads((package / 'package.json').read_text(encoding='utf-8'))
        check(manifest['source_commit'] == head and set(manifest['files']) == set(names), 'Wrong package version or composition')
        for name in names:
            data = (package / 'payload' / name).read_bytes()
            check(data == build_package.source_bytes(name) and
                  hashlib.sha256(data).hexdigest() == manifest['files'][name]['after'], 'Wrong payload: ' + name)
        for index, ref in enumerate(BASELINES):
            tracked = set(git('ls-tree', '-r', '--name-only', ref, 'vk_discord_publisher/').decode('utf-8').splitlines())
            check(bool(tracked), 'Missing baseline: ' + ref)
            root = work / str(index)
            root.mkdir()
            originals = {}
            for name in FULL_FILES:
                path = 'vk_discord_publisher/' + name
                if path not in tracked:
                    check(manifest['files'].get(name, {}).get('before') is None, 'Unexpected missing baseline file: ' + path)
                    originals[name] = None
                    continue
                text = git('show', ref + ':' + path).decode('utf-8-sig').replace('\r\n', '\n')
                if name.endswith(('.ps1', '.cmd')):
                    text = text.replace('\n', '\r\n')
                originals[name] = text.encode('utf-8')
                (root / name).write_bytes(originals[name])
            preserved = {name: ('local-test-marker:' + name).encode() for name in PRESERVED_FILES}
            for name, data in preserved.items():
                (root / name).write_bytes(data)
            backup = work / ('backup' + str(index))
            check(prepare(root, package, backup, files=names), 'Expected an update from ' + ref)
            apply_files(root, package, backup, files=names)
            build = app_version.describe(root)
            check(build['source_commit'] == head and build['status'] ==
                  ('modified' if manifest['source_dirty'] else 'verified'), 'Installed build was not identified')
            receipt = (root / app_version.RECORD).read_bytes()
            check(not prepare(root, package, work / 'unused', files=names), 'Repeat was not idempotent')
            check((root / app_version.RECORD).read_bytes() == receipt, 'Repeat changed build metadata')
            restore_files(root, backup, files=names)
            check(not (root / app_version.RECORD).exists(), 'Rollback left new build metadata')
            for name, data in originals.items():
                check(((root / name).read_bytes() if (root / name).exists() else None) == data, 'Rollback mismatch: ' + name)
            for name, data in preserved.items():
                check((root / name).read_bytes() == data, 'Preserved data changed: ' + name)
            print(archive.name + ' / ' + ref + ': update, version, repeat, rollback, data OK', flush=True)


def main():
    head = git('rev-parse', 'HEAD').decode().strip()
    for ref in BASELINES:
        git('rev-parse', '--verify', ref + '^{commit}')
    build_full_package.build()
    build_package.build()
    for archive, names in (('OneDelivery_full.zip', FULL_FILES), ('OneDelivery_settings_update.zip', FILES)):
        verify(PROJECT / archive, names, head)
    print('Verified ' + str(len(BASELINES) * 2) + ' offline package scenarios for ' + head)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
