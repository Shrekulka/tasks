from __future__ import annotations

import logging
import urllib.robotparser
from urllib.parse import urljoin

import requests

from teremok_parser.constants import CATEGORIES, HEADERS

logger = logging.getLogger("teremok_parser.http.robots")


def check_robots_allowed(session: requests.Session, base_url: str) -> bool:
    robots_url = urljoin(base_url, "/robots.txt")
    parser = urllib.robotparser.RobotFileParser()
    try:
        resp = session.get(robots_url, timeout=10)
        if resp.status_code == 200:
            parser.parse(resp.text.splitlines())
        elif resp.status_code in (401, 403):
            logger.warning(f"  [robots.txt] HTTP {resp.status_code}. Відмова в доступі; зупинка.")
            return False
        elif 400 <= resp.status_code < 500:
            logger.info(f"  [robots.txt] HTTP {resp.status_code}. Файл недоступний, доступ дозволено (RFC 9309).")
            return True
        else:
            logger.error(f"  [robots.txt] HTTP {resp.status_code}. Джерело недоступне.")
            return False
    except Exception as e:
        logger.error(f"  [robots.txt] Мережева помилка {robots_url}: {e}. Зупинка (fail-closed).")
        return False

    user_agent = HEADERS.get("User-Agent", "*")
    allowed_root = parser.can_fetch(user_agent, base_url + "/")
    allowed_categories = all(
        parser.can_fetch(user_agent, urljoin(base_url, path)) for path in CATEGORIES.values()
    )
    return allowed_root and allowed_categories
