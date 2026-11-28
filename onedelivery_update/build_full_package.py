"""Build one full archive directly from the canonical Git source; no version folders."""
import hashlib
import json
from pathlib import Path
import subprocess
import time
import zipfile

from build_package import source_bytes
from full_package import FULL_FILES
from full_install import PACKAGE_SUPPORT

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
APP = PROJECT / 'vk_discord_publisher'


def build():
    baseline = json.loads((APP / 'SOURCE_IMPORT.json').read_text(encoding='utf-8'))['sha256']
    supported = json.loads((HERE / 'supported_sources.json').read_text(encoding='utf-8'))
    head = subprocess.check_output(['git', '-c', 'safe.directory=' + PROJECT.as_posix(),
                                   'rev-parse', 'HEAD'], cwd=PROJECT).decode().strip()
    dirty = bool(subprocess.check_output(['git', '-c', 'safe.directory=' + PROJECT.as_posix(),
        'status', '--porcelain', '--untracked-files=normal'], cwd=PROJECT).strip())
    manifest = dict(kind='full-runtime', source_commit=head, source_dirty=dirty, built_at=time.time(), files={})
    payload = {}
    for name in FULL_FILES:
        data = source_bytes(name)
        if name.endswith('.py'):
            compile(data, name, 'exec')
        payload[name] = data
        compatible = set()
        for version in supported.values():
            if isinstance(version, dict):
                compatible.update(version.get(name, []))
        manifest['files'][name] = dict(before=baseline.get(name), after=hashlib.sha256(data).hexdigest(),
                                      compatible_before=sorted(compatible))
    archive = PROJECT / 'OneDelivery_full.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
        launcher = (PROJECT / 'INSTALL_ONEDELIVERY.cmd').read_text(encoding='utf-8-sig')
        output.writestr('INSTALL_ONEDELIVERY.cmd', launcher.replace('\n', '\r\n'))
        for name in PACKAGE_SUPPORT:
            if name == 'package.json':
                data = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
            elif name == 'README.txt':
                data = (HERE / 'README_FULL.txt').read_bytes()
            else:
                data = (HERE / name).read_bytes()
            output.writestr('onedelivery_update/' + name, data)
        for name, data in payload.items():
            output.writestr('onedelivery_update/payload/' + name, data)
    print(str(archive))
    print('Runtime files: ' + str(len(FULL_FILES)))
    return archive


if __name__ == '__main__':
    build()
