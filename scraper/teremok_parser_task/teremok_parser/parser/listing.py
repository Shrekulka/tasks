from __future__ import annotations

import copy
import logging
import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from teremok_parser.constants import DETAIL_SELECTORS, MISSING
from teremok_parser.models import Listing, ParseResult, ParseStatus
from teremok_parser.parser.date import to_utc
from teremok_parser.parser.price import parse_price

logger = logging.getLogger(__name__)


def parse_listing_html(html: str, url: str, listing_id: str, source_category: str) -> ParseResult:
    try:
        soup = BeautifulSoup(html, "lxml")

        h1 = soup.select_one(DETAIL_SELECTORS["title"])
        title = h1.get_text(strip=True) if h1 else MISSING

        crumbs = [
            a.get_text(strip=True) for a in soup.select(DETAIL_SELECTORS["breadcrumbs"])
            if a.get_text(strip=True) and a.get_text(strip=True) not in ("назад", "Головна")
        ]
        category = " > ".join(crumbs) if crumbs else MISSING

        price_block = soup.select_one(DETAIL_SELECTORS["price"])
        amount, currency, price_raw = None, None, MISSING
        if price_block:
            price_copy = copy.copy(price_block)
            bid = price_copy.select_one(".bl-inl")
            if bid:
                bid.decompose()
            price_raw_text = price_copy.get_text(strip=True)
            if price_raw_text:
                price_raw = price_raw_text
                amount, currency = parse_price(price_raw_text)

        city_tag = soup.select_one(DETAIL_SELECTORS["city"])
        city = city_tag.get_text(strip=True) if city_tag else MISSING

        desc_block = soup.select_one(DETAIL_SELECTORS["description"])
        description, published_at_raw, published_at_dt, views = MISSING, MISSING, None, None

        if desc_block:
            desc_copy = copy.copy(desc_block)
            stat_block = desc_copy.select_one(".product-stat")
            if stat_block:
                stat_text = stat_block.get_text(" ", strip=True)
                views_m = re.search(r"(?:перегляди|просмотров):\s*(\d+)", stat_text, re.IGNORECASE)
                if views_m:
                    views = int(views_m.group(1))

                stat_span = stat_block.select_one("span[title]")
                title_attr = stat_span.get("title", "") if stat_span else stat_text
                m = re.search(r"(\d{2}\.\d{2}\.\d{4})\s+[а-яіїєґa-z]*\s*(\d{2}:\d{2})", title_attr, re.IGNORECASE)
                if m:
                    date_str, time_str = m.group(1), m.group(2)
                    published_at_raw = f"{date_str} {time_str}"
                    try:
                        naive = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
                        published_at_dt = to_utc(naive)
                    except ValueError:
                        logger.debug(f"Не вдалося розпарсити дату '{date_str} {time_str}' для {url}")
                stat_block.decompose()
            description = desc_copy.get_text(" ", strip=True)

        characteristics: dict[str, str] = {}
        params_list = soup.select_one("ul.product_params")
        if params_list:
            for li in params_list.select("li"):
                span = li.select_one("span")
                if span:
                    k = span.get_text(strip=True).rstrip(":")
                    v = li.get_text(" ", strip=True).replace(span.get_text(), "", 1).strip()
                elif ":" in li.get_text():
                    k, v = li.get_text().split(":", 1)
                    k, v = k.strip(), v.strip()
                else:
                    k, v = "Параметр", li.get_text(strip=True)
                if k and v:
                    characteristics[k] = v

        images: list[str] = []
        for a in soup.select(DETAIL_SELECTORS["images"]):
            img = a.select_one("img")
            src = a.get("data-full") or a.get("data-src") or a.get("href") or (img.get("src") if img else None)
            if src:
                full_img = urljoin(url, src)
                if full_img not in images:
                    images.append(full_img)

        seller = MISSING
        for script in soup.select("script"):
            script_content = script.get_text()
            if "elementName" in script_content:
                match = re.search(r'name\s*=\s*"([^"]+)"', script_content)
                if match:
                    seller = match.group(1).strip()
                    break

        listing = Listing(
            id=listing_id,
            title=title,
            description=description,
            price=amount,
            currency=currency,
            price_raw=price_raw,
            category=category,
            category_path=crumbs,
            source_category=source_category,
            city=city,
            published_at=published_at_dt,
            published_at_raw=published_at_raw,
            url=url,
            images=images,
            seller=seller,
            views=views,
            characteristics=characteristics,
        )
        return ParseResult(listing=listing, status=ParseStatus.SUCCESS)
    except Exception as e:
        return ParseResult(status=ParseStatus.PARSE_ERROR, error_msg=f"HTML error: {e}")
