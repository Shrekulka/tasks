from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

from teremok_parser.config import settings
from teremok_parser.constants import ALL_MONTHS

SOURCE_TIMEZONE = ZoneInfo(settings.source_timezone)


def to_utc(dt_naive: Optional[datetime]) -> Optional[datetime]:
    if dt_naive is None:
        return None
    return dt_naive.replace(tzinfo=SOURCE_TIMEZONE).astimezone(timezone.utc)


def parse_preview_date(text: str) -> Optional[datetime]:
    m = re.search(r"(\d{1,2})\s+([а-яіїєґ]+)\s+(\d{4})\s+[а-яіїєґa-z]*\s*(\d{1,2}):(\d{2})", text.strip(), re.IGNORECASE)
    if not m:
        return None
    day, month_name, year, hh, mm = m.groups()
    month = ALL_MONTHS.get(month_name.lower())
    if not month:
        return None
    return to_utc(datetime(int(year), month, int(day), int(hh), int(mm)))


def sort_key(dt: Optional[datetime]) -> datetime:
    return dt or datetime.min.replace(tzinfo=timezone.utc)
