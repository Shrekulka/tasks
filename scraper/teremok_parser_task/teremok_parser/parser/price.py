from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Optional


def parse_price(raw: Optional[str]) -> tuple[Optional[Decimal], Optional[str]]:
    if not raw:
        return None, None
    raw = raw.strip()
    if not raw:
        return None, None

    currency = "USD" if ("$" in raw or "USD" in raw.upper()) else ("UAH" if ("₴" in raw or "грн" in raw.lower()) else None)
    cleaned = re.sub(r"[^\d.,]", "", raw)
    if not cleaned:
        return None, currency

    if "." in cleaned and "," in cleaned:
        if cleaned.rfind(".") > cleaned.rfind(","):
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        parts = cleaned.split(",")
        cleaned = cleaned.replace(",", ".") if len(parts) == 2 and len(parts[1]) in (1, 2) else cleaned.replace(",", "")
    elif "." in cleaned:
        parts = cleaned.split(".")
        if not (len(parts) == 2 and len(parts[1]) in (1, 2)):
            cleaned = cleaned.replace(".", "")

    try:
        return Decimal(cleaned), currency
    except InvalidOperation:
        return None, currency
