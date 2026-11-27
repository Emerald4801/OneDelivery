"""Local VK album downloader. Python 3.10+. No passwords or API tokens needed."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlparse
import zipfile

DEFAULT_URL = "https://vk.ru/album-334424_310875314"


def album_key(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"vk.ru", "vk.com", "www.vk.ru", "www.vk.com"}:
        raise ValueError("Нужна HTTPS-ссылка на альбом vk.ru или vk.com.")
    match = re.fullmatch(r"/album(-?\d+_\d+)/?", parsed.path)
    if not match:
        raise ValueError("Формат ссылки: https://vk.ru/album-334424_310875314")
    return match[1]


def parse_photos(raw):
    messages = json.loads(raw)
    if not isinstance(messages, list):
        raise ValueError("Неожиданный формат ответа gallery-dl.")
    photos, seen = [], set()
    for message in messages:
        if not isinstance(message, list) or len(message) < 3 or message[0] != 3:
            continue
        url, metadata = message[1:3]
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ValueError("Загрузчик вернул неподдерживаемую ссылку.")
        if not isinstance(metadata, dict):
            raise ValueError("Не удалось прочитать метаданные фотографии.")
        identity = str(metadata.get("id") or metadata.get("photo_id") or urlparse(url).path)
        if identity in seen:
            continue
        seen.add(identity)
        photos.append({"id": identity, "url": url})
    if not photos:
        raise ValueError("Фотографии не найдены. Возможно, нужен вход в ВК или обновление gallery-dl.")
    return photos


def extract(url, cookies=None, browser=None):
    command = [sys.executable, "-m", "gallery_dl", "--config-ignore", "--no-input",
               "--dump-json", "--sleep-request", "1.2", "--retries", "3",
               "--http-timeout", "30"]
    if cookies:
        command += ["--cookies", str(Path(cookies).resolve())]
    if browser:
        command += ["--cookies-from-browser", browser]
    command.append(url)
    print("Получаю список фотографий...", flush=True)
    # stderr stays on screen. Never persist response pages, cookies or signed URLs.
    result = subprocess.run(command, stdout=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if result.returncode:
        raise RuntimeError("gallery-dl не смог прочитать альбом. См. сообщение выше и README.txt.")
    return parse_photos(result.stdout)


def atomic_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_image(path):
    from PIL import Image
    with Image.open(path) as image:
        fmt, size = image.format, image.size
        image.verify()
    with Image.open(path) as image:
        image.load()
    extensions = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif", "AVIF": "avif"}
    if fmt not in extensions:
        raise ValueError("Неожиданный формат изображения: " + str(fmt))
    return extensions[fmt], size


def download_one(session, url, target):
    # The URL comes from the VK extractor, not from thumbnails in an HTML grid.
    last_error = None
    for attempt in range(3):
        try:
            with session.get(url, stream=True, timeout=(20, 60)) as response:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/html" in content_type or "application/json" in content_type:
                    raise ValueError("Вместо изображения сервер вернул страницу или сообщение об ошибке.")
                total = 0
                with target.open("wb") as stream:
                    for chunk in response.iter_content(256 * 1024):
                        if chunk:
                            total += len(chunk)
                            if total > 200 * 1024 * 1024:
                                raise ValueError("Ответ больше 200 МБ; загрузка остановлена.")
                            stream.write(chunk)
            return inspect_image(target)
        except Exception as exc:
            # Avoid printing exceptions containing signed CDN URLs.
            last_error = type(exc).__name__
            target.unlink(missing_ok=True)
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError("Не удалось скачать изображение: " + last_error)


def load_manifest(path, url, photos):
    identities = [item["id"] for item in photos]
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("photo_ids") != identities or state.get("album") != album_key(url):
            raise ValueError("Состав или порядок альбома изменился. Используйте новую папку --output.")
        return state
    return {"version": 1, "album": album_key(url), "source": url,
            "order": "gallery-dl extraction order; reading order is not independently verified",
            "photo_ids": identities, "files": {}}


def make_cbz(folder, state):
    target = folder / ("album" + state["album"] + ".cbz")
    temp = target.with_suffix(".cbz.tmp")
    with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_STORED) as archive:
        for number in range(1, len(state["photo_ids"]) + 1):
            name = state["files"][str(number)]["name"]
            archive.write(folder / name, arcname=name)
    temp.replace(target)
    return target


def run(args):
    import requests
    key = album_key(args.url)
    photos = extract(args.url, args.cookies, args.browser)
    print(f"Найдено уникальных фотографий: {len(photos)}")
    if args.expected is not None and len(photos) != args.expected:
        raise ValueError(f"Ожидалось {args.expected}, найдено {len(photos)}. Скачивание не начато.")
    folder = Path(args.output).resolve() / ("album" + key)
    folder.mkdir(parents=True, exist_ok=True)
    manifest_path = folder / "manifest.json"
    state = load_manifest(manifest_path, args.url, photos)
    atomic_json(manifest_path, state)
    with requests.Session() as session:
        session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": args.url})
        for number, photo in enumerate(photos, 1):
            previous = state["files"].get(str(number))
            if previous:
                name = previous.get("name", "")
                if Path(name).name != name or not re.fullmatch(r"\d{4,}\.(jpg|png|webp|gif|avif)", name):
                    raise ValueError("Некорректное имя в manifest.json.")
                existing = folder / name
                if existing.is_file() and checksum(existing) == previous["sha256"]:
                    print(f"[{number}/{len(photos)}] Уже скачано: {name}")
                    continue
            temp = folder / f"{number:04}.part"
            ext, (width, height) = download_one(session, photo["url"], temp)
            name = f"{number:04}.{ext}"
            target = folder / name
            if target.exists() and previous is None:
                temp.unlink(missing_ok=True)
                raise ValueError("В папке уже есть файл без записи в manifest.json. Выберите другую --output.")
            temp.replace(target)
            state["files"][str(number)] = {"name": name, "width": width, "height": height,
                                           "sha256": checksum(target)}
            atomic_json(manifest_path, state)
            print(f"[{number}/{len(photos)}] {name}: {width} x {height}")
            if min(width, height) < 700:
                print("  Внимание: небольшое разрешение. Проверьте читаемость текста.")
            time.sleep(1)
    cbz = make_cbz(folder, state)
    print(f"\nГотово: {folder}\nCBZ: {cbz.name}")
    print("Проверьте порядок чтения и полноту. Нумерация сохраняет порядок, полученный от ВК через gallery-dl.")
    if args.expected is None:
        print("Полнота относительно счётчика ВК не проверена. Для проверки используйте --expected ЧИСЛО.")


def main():
    parser = argparse.ArgumentParser(description="Скачать альбом ВК локально и собрать CBZ.")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL)
    parser.add_argument("--output", default=str(Path(__file__).resolve().parent / "downloads"))
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--cookies", help="Путь к Netscape cookies.txt, экспортированному только для ВК")
    auth.add_argument("--browser", choices=["firefox", "chrome", "edge"], help="Прочитать cookies выбранного браузера локально")
    parser.add_argument("--expected", type=int, help="Число фотографий по счётчику альбома ВК")
    args = parser.parse_args()
    if args.expected is not None and args.expected <= 0:
        parser.error("--expected должен быть положительным")
    try:
        run(args)
    except KeyboardInterrupt:
        print("\nОстановлено. Завершённые файлы сохранятся для повторного запуска.")
        return 130
    except Exception as exc:
        if isinstance(exc, (ValueError, RuntimeError)):
            print("\nОшибка: " + str(exc), file=sys.stderr)
        else:
            print("\nОшибка: " + type(exc).__name__ + ". Проверьте зависимости, доступ к папке и README.txt.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
