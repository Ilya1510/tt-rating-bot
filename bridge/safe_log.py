import builtins
import os
import re


def safe_print(*args, **kwargs):
    text = ' '.join(map(str, args))
    for key, value in os.environ.items():
        if any(x in key for x in ('TOKEN', 'SECRET', 'KEY')) and len(value) >= 6:
            text = text.replace(value, '[secret]')
    text = re.sub(r'(?:\d{6,12}:[\w-]{25,}|vk1\.[\w.-]+|(?:y[01]_|t[01]_|AQAD-)[\w-]+)', '[secret]', text)
    builtins.print(text, flush=True)
