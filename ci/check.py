"""Run the same source, Windows and package checks locally and on GitHub."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

PROJECT = Path(__file__).resolve().parent.parent


def run(command, env):
    print('\nChecking: ' + ' '.join(map(str, command)), flush=True)
    subprocess.run(command, cwd=PROJECT, env=env, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--native-windows', action='store_true', help='enable Tk/CIM and reject skipped tests')
    args = parser.parse_args()
    if args.native_windows and os.name != 'nt':
        parser.error('--native-windows requires Windows')
    env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    for secret in ('DISCORD_BOT_TOKEN', 'VK_API_TOKEN'):
        env.pop(secret, None)
    for flag in ('ONEDELIVERY_TEST_TK', 'ONEDELIVERY_TEST_NATIVE_TASK_PREVIEW'):
        env[flag] = '1' if args.native_windows else '0'
    try:
        run(['git', '-c', 'safe.directory=' + PROJECT.as_posix(), 'diff', '--check'], env)
        run([sys.executable, '-m', 'compileall', '-q', 'vk_discord_publisher', 'onedelivery_update', 'ci'], env)
        for directory, pattern in (('.', 'test_*.py'), ('onedelivery_update', 'test_*.py'),
                                   ('vk_card_update_v2', 'test_card_update.py'), ('vk_card_update_v2', 'test_shutdown.py')):
            command = [sys.executable, 'ci/unit_suite.py', '--start', directory, '--pattern', pattern]
            run(command + (['--strict'] if args.native_windows else []), env)
        run([sys.executable, 'ci/verify_packages.py'], env)
    except subprocess.CalledProcessError as error:
        return error.returncode or 1
    print('\nAll source, test and package checks passed.', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
