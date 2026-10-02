import io

import pytest
from PIL import Image, PngImagePlugin

from ttar.photo import fingerprints
from ttar.core import Store
from test_core import raw


def test_metadata_reupload_is_same_photo(tmp_path):
    image = Image.new('RGB', (32, 32), 'white')
    first, second = io.BytesIO(), io.BytesIO()
    image.save(first, format='PNG')
    info = PngImagePlugin.PngInfo()
    info.add_text('Comment', 'reuploaded')
    image.save(second, format='PNG', pnginfo=info)
    sha1, pixels1 = fingerprints(first.getvalue())
    sha2, pixels2 = fingerprints(second.getvalue())
    assert sha1 != sha2
    assert pixels1 == pixels2
    store = Store(tmp_path/'test.sqlite3')
    with store.transaction():
        pid, new = store.put_photo(-123, 1, 'uid1', sha1, 100, 42, raw(), pixels1)
        assert new
        same, new = store.put_photo(-123, 2, 'uid2', sha2, 200, 42, raw(), pixels2)
        assert same == pid and not new


def test_invalid_image_never_reaches_codex():
    with pytest.raises(ValueError):
        fingerprints(b'not an image')
