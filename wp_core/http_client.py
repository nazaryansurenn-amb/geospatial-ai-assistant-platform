import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def build_retry_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    session = requests.Session()
    session.headers.update({"User-Agent": "echmiadzin-water-land-monitor/1.0"})
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
