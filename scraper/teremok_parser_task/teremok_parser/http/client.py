from __future__ import annotations

import logging
import random
import time
from typing import Optional

import requests

from teremok_parser.config import Settings
from teremok_parser.models import ParseStatus

logger = logging.getLogger("teremok_parser.http.client")


class HttpClient:
    """Транспортний клієнт (відповідає лише за виконання HTTP-запитів)."""

    def __init__(self, session: requests.Session, config: Settings):
        self.session = session
        self.config = config

    def polite_sleep(self) -> None:
        delay = self.config.request_delay_base + random.uniform(0, self.config.request_delay_jitter)
        logger.debug("Пауза між запитами: %.2f сек", delay)
        time.sleep(delay)

    def fetch_html(self, url: str) -> tuple[Optional[str], ParseStatus, Optional[str]]:
        logger.debug("GET %s", url)
        resp = None
        try:
            resp = self.session.get(url, timeout=self.config.request_timeout)
            resp.raise_for_status()
            logger.debug("Успіх [%s]: %s", resp.status_code, url)
            return resp.text, ParseStatus.SUCCESS, None
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else (resp.status_code if resp is not None else "UNKNOWN")
            msg = f"HTTP Error ({code}): {e}"
            logger.warning("HTTP помилка [%s] для %s: %s", code, url, e)
            return None, ParseStatus.HTTP_ERROR, msg
        except requests.RequestException as e:
            msg = f"Network Error: {e}"
            logger.warning("Мережева помилка для %s: %s", url, e)
            return None, ParseStatus.NETWORK_ERROR, msg
        except Exception as e:
            msg = f"Transport Error: {e}"
            logger.error("Непередбачена помилка транспорту для %s: %s", url, e, exc_info=True)
            return None, ParseStatus.NETWORK_ERROR, msg
