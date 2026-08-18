from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

from teremok_parser.config import Settings
from teremok_parser.constants import HEADERS, RETRY_STATUS_FORCELIST


def build_session(config: Settings) -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)

    retry = Retry(
        total=config.retry_total,
        backoff_factor=config.retry_backoff_factor,
        status_forcelist=RETRY_STATUS_FORCELIST,
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
