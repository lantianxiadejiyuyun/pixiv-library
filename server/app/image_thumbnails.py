"""Bound thumbnail decoding; keep full-size pixel copies out of API requests."""
import io
from threading import Lock
import warnings

from PIL import Image, ImageOps


# Cache hits do not take this lock. Cache misses recheck after acquiring it so
# duplicate requests share the file produced by the first request.
generation_lock = Lock()
MAX_PIXELS = 36_000_000


def render(source, size, *, quality=80):
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(source) as original:
            # Orienting before resize loads AND copies the original pixels.
            # JPEG draft decoding and thumbnail() reduce pixels before either
            # EXIF rotation or RGB conversion creates another image.
            orientation = original.getexif().get(274, 1)
            target = size[::-1] if orientation in (5, 6, 7, 8) else size
            original.draft("RGB", target)
            if original.width * original.height > MAX_PIXELS:
                raise ValueError("图片像素过大，无法生成缩略图；仍可查看原图")
            original.thumbnail(target)
            with ImageOps.exif_transpose(original) as oriented:
                with oriented.convert("RGB") as picture:
                    with io.BytesIO() as buffer:
                        picture.save(buffer, "WEBP", quality=quality)
                        return buffer.getvalue()
