import hashlib
import io

from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 10000000


def fingerprints(image):
    """Raw SHA plus decoded-pixel SHA: lossless metadata/format changes dedup too."""
    digest = hashlib.sha256(image).hexdigest()
    try:
        with Image.open(io.BytesIO(image)) as source:
            if source.width * source.height > 10000000 or source.format not in ('JPEG', 'PNG', 'WEBP'):
                raise ValueError('Unsupported/oversized photo')
            normalized = ImageOps.exif_transpose(source).convert('RGB')
            canonical = hashlib.sha256()
            canonical.update(f'{normalized.width}x{normalized.height}:RGB:'.encode())
            canonical.update(normalized.tobytes())
            return digest, canonical.hexdigest()
    except Exception:
        raise ValueError('Фотография повреждена, имеет неподдерживаемый формат или слишком велика') from None
