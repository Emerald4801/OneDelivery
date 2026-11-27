"""Optional, VK-only cookie import. Nothing is read from a browser profile."""
from http.cookiejar import Cookie, CookieJar, MozillaCookieJar
import json
from pathlib import Path
import sys

import publisher_runtime as runtime

DOMAINS = {'vk.ru', 'vk.com', 'm.vk.ru', 'm.vk.com'}
FIELDS = ('version', 'name', 'value', 'port', 'port_specified', 'domain', 'domain_specified',
          'domain_initial_dot', 'path', 'path_specified', 'secure', 'expires', 'discard',
          'comment', 'comment_url', 'rfc2109')


def allowed(cookie):
    return cookie.domain.lstrip('.') in DOMAINS


def usable(cookie):
    # Netscape exporters commonly encode session cookies with an expiry of zero.
    if cookie.expires == 0:
        cookie.expires, cookie.discard = None, True
    return allowed(cookie) and not cookie.is_expired()


def load(root):
    path = Path(root) / 'vk_cookies.dpapi'
    jar = CookieJar()
    if path.exists():
        try:
            data = json.loads(runtime.protect(path.read_bytes(), decrypt=True))
            for item in data:
                cookie = Cookie(**item, rest={})
                if usable(cookie):
                    jar.set_cookie(cookie)
        except Exception:
            raise runtime.AppError('Не удалось прочитать сессию ВК. Повторите локальный импорт cookies.') from None
    else:
        # Legacy scoped export remains supported, but never copied into a release archive.
        old = Path(root) / 'cookies.txt'
        if old.exists():
            source = MozillaCookieJar(str(old))
            source.load(ignore_discard=True, ignore_expires=True)
            for cookie in source:
                if usable(cookie):
                    jar.set_cookie(cookie)
    return jar


def import_file(source, root):
    source = Path(source).resolve()
    destination = Path(root) / 'vk_cookies.dpapi'
    jar = MozillaCookieJar(str(source))
    jar.load(ignore_discard=True, ignore_expires=True)
    cookies = [{k: getattr(cookie, k) for k in FIELDS} for cookie in jar if usable(cookie)]
    if not cookies:
        raise runtime.AppError('В файле нет действующих cookies ВК.')
    encrypted = runtime.protect(json.dumps(cookies).encode())
    temporary = destination.with_suffix('.tmp')
    temporary.write_bytes(encrypted)
    temporary.replace(destination)
    return len(cookies)


if __name__ == '__main__':
    try:
        source = sys.argv[1] if len(sys.argv) > 1 else input('Путь к экспортированному файлу cookies ВК: ').strip().strip('"')
        total = import_file(source, Path(__file__).resolve().parent)
        print(f'Сохранена защищённая сессия ВК ({total} cookies). Исходный экспорт можно удалить.')
    except Exception as exc:
        print(str(exc) if isinstance(exc, runtime.AppError) else type(exc).__name__)
        sys.exit(1)
