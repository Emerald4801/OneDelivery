"""Refresh the complete existing runtime while retaining its Windows profile and data."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import zipfile

from full_package import FULL_FILES, PRESERVED_FILES
import update_settings

HERE = Path(__file__).resolve().parent
PACKAGE_SUPPORT = ('full_install.py', 'full_package.py', 'update_settings.py', 'package.json', 'README.txt')
ARCHIVE_FILES = {'INSTALL_ONEDELIVERY.cmd', *('onedelivery_update/' + name for name in PACKAGE_SUPPORT),
                 *('onedelivery_update/payload/' + name for name in FULL_FILES)}


@contextmanager
def unpack(archive):
    with tempfile.TemporaryDirectory(prefix='onedelivery_full_') as temporary:
        destination = Path(temporary)
        with zipfile.ZipFile(archive) as source:
            names = source.namelist()
            if len(names) != len(set(names)) or set(names) != ARCHIVE_FILES:
                raise ValueError('Неподдерживаемый состав полного архива OneDelivery.')
            for info in source.infolist():
                if info.file_size > 2 * 1024 * 1024:
                    raise ValueError('Некорректный размер файла полного пакета.')
                path = destination.joinpath(*info.filename.split('/'))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(source.read(info))
        yield destination / 'onedelivery_update'


def refresh(root, package, restore=None):
    root, package = Path(root).resolve(), Path(package).resolve()
    missing = [name for name in PRESERVED_FILES if not (root / name).is_file()]
    if missing:
        raise ValueError('Не найдены сохранённые настройки или токены в установленной программе. '
                         'Сначала восстановите её папку AppData из своей резервной копии; данные не менялись.')
    if not restore:
        manifest = json.loads((package / 'package.json').read_text(encoding='utf-8'))
        if manifest.get('kind') != 'full-runtime' or set(manifest.get('files', {})) != set(FULL_FILES):
            raise ValueError('Полный пакет не собран. Нужен OneDelivery_full.zip из папки проекта.')
    # The shared transaction stops only the matched idle worker, then holds both locks.
    update_settings.install(root, package, restore=restore, files=FULL_FILES)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(os.environ.get('LOCALAPPDATA', '.')) / 'VKDiscordPublisher')
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--restore', type=Path)
    args = parser.parse_args()
    try:
        if args.archive:
            with unpack(args.archive) as package:
                refresh(args.root, package, args.restore)
        else:
            refresh(args.root, HERE, args.restore)
        return 0
    except Exception as exc:
        print('Полное обновление остановлено: ' + str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
