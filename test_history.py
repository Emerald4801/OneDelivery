"""Load immutable v7 fixtures from Git; never keep a second application tree."""
from pathlib import Path
import subprocess

PROJECT = Path(__file__).resolve().parent
BASELINE = '5357621'


def source(name):
    return subprocess.check_output(['git', '-c', 'safe.directory=' + PROJECT.as_posix(),
        'show', BASELINE + ':vk_discord_publisher/' + name], cwd=PROJECT).decode('utf-8')
