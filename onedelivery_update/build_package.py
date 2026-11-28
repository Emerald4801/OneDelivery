"""Build a local delta from the canonical source and the verified import hashes."""
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

from update_settings import FILES

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
APP = PROJECT / 'vk_discord_publisher'


def source_bytes(name):
    # Match .gitattributes regardless of the checkout's native line endings.
    text = (APP / name).read_text(encoding='utf-8-sig')
    if name.endswith(('.ps1', '.cmd')):
        text = text.replace('\n', '\r\n')
    return text.encode('utf-8')


def build():
    imported = json.loads((APP / 'SOURCE_IMPORT.json').read_text(encoding='utf-8'))['sha256']
    supported = json.loads((HERE / 'supported_sources.json').read_text(encoding='utf-8'))
    manifest = dict(description='OneDelivery: destination settings including announcement channels', files={})
    payload = HERE / 'payload'
    payload.mkdir(exist_ok=True)
    for name in FILES:
        data = source_bytes(name)
        (payload / name).write_bytes(data)
        manifest['files'][name] = dict(before=imported.get(name), after=hashlib.sha256(data).hexdigest(),
                                      compatible_before=supported['settings_84ca648'].get(name, []))
    manifest['source_commit'] = subprocess.check_output(['git', '-c', 'safe.directory=' + PROJECT.as_posix(),
        'rev-parse', 'HEAD'], cwd=PROJECT).decode().strip()
    (HERE / 'package.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    archive = PROJECT / 'OneDelivery_settings_update.zip'
    names = ['update_settings.py', 'package.json', 'README.txt']
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
        launcher = (PROJECT / 'UPDATE_ONEDELIVERY.cmd').read_text(encoding='utf-8-sig')
        output.writestr('UPDATE_ONEDELIVERY.cmd', launcher.replace('\n', '\r\n'))
        for name in names:
            output.write(HERE / name, 'onedelivery_update/' + name)
        for name in FILES:
            output.write(payload / name, 'onedelivery_update/payload/' + name)
    print(str(archive))
    print('Delta files: ' + str(len(FILES)))


if __name__ == '__main__':
    build()
