"""Reuse connections and retry only failures before a request could be sent."""
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_local = threading.local()


def request(method, url, **kwargs):
    if not hasattr(_local, 'session'):
        session = requests.Session()
        retries = Retry(total=2, connect=2, read=0, status=0, redirect=0,
                        allowed_methods=None, backoff_factor=.2)
        session.mount('https://', HTTPAdapter(max_retries=retries))
        _local.session = session
    try:
        return _local.session.request(method, url, **kwargs)
    except requests.RequestException:
        # Requests exceptions contain credential-bearing URLs. Never propagate them.
        raise RuntimeError('HTTP transport failed') from None


def get(url, **kwargs): return request('GET', url, **kwargs)
def post(url, **kwargs): return request('POST', url, **kwargs)
