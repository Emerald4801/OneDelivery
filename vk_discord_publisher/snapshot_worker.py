"""One bounded gallery-dl extraction. VK cookies arrive over stdin, never a temp file."""
import json
import sys

from gallery_dl import config
from gallery_dl.job import DataJob
from download_album import album_key


def main():
    url = sys.argv[1]
    album_key(url)
    data = json.load(sys.stdin)
    config.set(('extractor',), 'retries', 3)
    config.set(('extractor',), 'timeout', 30)
    config.set(('extractor',), 'sleep-request', 1.2)
    config.set(('extractor',), 'input', False)
    if data.get('cookies'):
        config.set(('extractor', 'vk'), 'cookies', data['cookies'])
    job = DataJob(url, file=sys.stdout)
    job.run()
    return 1 if job.exception else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        sys.exit(1)
