"""User-selected application icon, copied locally and normalized once."""
from pathlib import Path
import tempfile

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError


class IconError(ValueError):
    pass


def default_icon():
    image = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((3, 3, 61, 61), radius=15, fill='#3678DF')
    draw.line([(16, 18), (16, 44), (28, 50), (28, 24), (16, 18)], fill='white', width=4)
    draw.line([(28, 24), (47, 18), (47, 44), (28, 50)], fill='white', width=4)
    return image


def icon_path(root):
    return Path(root) / 'ui' / 'tray_icon.png'


def save_icon(source, root):
    source = Path(source)
    target = icon_path(root)
    try:
        if source.stat().st_size > 20 * 1024 * 1024:
            raise IconError('Выберите изображение меньше 20 МБ.')
        with Image.open(source) as original:
            if original.format not in ('PNG', 'JPEG', 'WEBP', 'ICO'):
                raise IconError('Поддерживаются PNG, JPG, WEBP и ICO.')
            if original.width * original.height > 25_000_000:
                raise IconError('Слишком большое изображение. Уменьшите его до 25 мегапикселей или меньше.')
            image = ImageOps.exif_transpose(original).convert('RGBA')
            image = ImageOps.contain(image, (256, 256), method=Image.Resampling.LANCZOS)
            canvas = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
            canvas.paste(image, ((256 - image.width) // 2, (256 - image.height) // 2))
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.png', delete=False) as stream:
            temporary = Path(stream.name)
        try:
            canvas.save(temporary, format='PNG')
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return canvas
    except IconError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError, UnidentifiedImageError):
        raise IconError('Не удалось открыть картинку. Прежний значок сохранён.') from None


def load_icon(root):
    try:
        with Image.open(icon_path(root)) as image:
            image.load()
            return image.convert('RGBA')
    except (OSError, ValueError):
        return default_icon()


def reset_icon(root):
    icon_path(root).unlink(missing_ok=True)
    return default_icon()
