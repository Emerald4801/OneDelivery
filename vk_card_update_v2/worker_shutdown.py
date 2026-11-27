"""Wait for the installed application's own locks; never stop a process."""
import argparse
from contextlib import contextmanager, ExitStack
import importlib
import json
from pathlib import Path
import sys
import time


class WorkerBusy(RuntimeError):
    pass


@contextmanager
def application_lock(root):
    root = Path(root).resolve()
    config = json.loads((root / 'config.json').read_text(encoding='utf-8-sig'))
    if not all(str(config.get(key, '')).isdigit() for key in ('guild_id', 'channel_id')):
        raise ValueError('Invalid destination identifiers.')
    sys.path.insert(0, str(root))
    try:
        ongoing = importlib.import_module('ongoing')
        runtime = importlib.import_module('publisher_runtime')
        for module in (ongoing, runtime):
            if Path(module.__file__).resolve().parent != root:
                raise ValueError('Lock module does not belong to this installation.')
        with ExitStack() as stack:
            try:
                stack.enter_context(ongoing.channel_lock(config))
                stack.enter_context(runtime.exclusive(root))
            except runtime.AppError as exc:
                raise WorkerBusy(str(exc)) from None
            yield
    finally:
        sys.path.pop(0)


def wait_until_free(root, timeout=30, *, lock=application_lock, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + timeout
    while True:
        try:
            with lock(root):
                return True
        except WorkerBusy:
            remaining = deadline - clock()
            if remaining <= 0:
                return False
            sleep(min(0.25, remaining))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args()
    try:
        if not 0 <= args.timeout <= 30:
            raise ValueError('Lock wait must be between 0 and 30 seconds.')
        if wait_until_free(args.root, args.timeout):
            print('Publisher locks released.')
            return 0
        print('Publisher is still busy after waiting. Installed files were not changed.')
        return 1
    except Exception as exc:
        print('Cannot check publisher locks: ' + str(exc))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
