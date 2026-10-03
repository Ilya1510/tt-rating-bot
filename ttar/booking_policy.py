"""Trusted validation of the editable, data-only booking policy."""
import json
from pathlib import Path


def load_policy(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 1024:
        raise ValueError('Invalid booking policy file')
    data = json.loads(path.read_text())
    if (not isinstance(data, dict) or set(data) != {'regular_minutes'}
            or type(data['regular_minutes']) is not int
            or not 1 <= data['regular_minutes'] <= 150):
        raise ValueError('regular_minutes must be an integer from 1 to 150')
    return data
