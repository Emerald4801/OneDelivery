"""Stage and replace one updater-owned file without PowerShell/.NET path binding."""
import argparse
import os
from pathlib import Path
import shutil
import sys
import tempfile


def atomic_copy(source, target):
    source, target = Path(source), Path(target)
    temporary = None
    try:
        with source.open('rb') as incoming:
            with tempfile.NamedTemporaryFile(mode='wb', dir=target.parent,
                                             prefix=target.name + '.update_', suffix='.tmp', delete=False) as outgoing:
                temporary = Path(outgoing.name)
                shutil.copyfileobj(incoming, outgoing)
                outgoing.flush()
                os.fsync(outgoing.fileno())
        os.replace(temporary, target)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('target', type=Path)
    args = parser.parse_args()
    try:
        atomic_copy(args.source, args.target)
    except OSError as exc:
        print('Could not replace ' + args.target.name + ': ' + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
