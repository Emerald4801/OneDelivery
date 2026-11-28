"""One isolated unittest suite; native CI must fail on unexpected skips."""
import argparse
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', required=True)
    parser.add_argument('--pattern', default='test_*.py')
    parser.add_argument('--strict', action='store_true')
    args = parser.parse_args()
    start = Path(args.start).resolve()
    suite = unittest.defaultTestLoader.discover(str(start), pattern=args.pattern, top_level_dir=str(start))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if result.testsRun == 0:
        print('ERROR: no tests discovered', file=sys.stderr)
        return 1
    if args.strict and result.skipped:
        print('ERROR: native Windows checks were skipped', file=sys.stderr)
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
