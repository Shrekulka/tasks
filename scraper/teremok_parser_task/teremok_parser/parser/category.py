from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, NavigableString

from teremok_parser.models import ListingCandidate
from teremok_parser.parser.date import parse_preview_date


def get_pagination_links(soup: BeautifulSoup, current_url: str) -> list[str]:
    links: list[str] = []
    bar = soup.select_one("div.pagination-bar ul.pagination")
    if not bar:
        return links
    for a in bar.select("li a[href]"):
        full = urljoin(current_url, a["href"])
        if full not in links:
            links.append(full)
    return links


def parse_category_card(card_soup: BeautifulSoup, category_name: str, page_url: str) -> Optional[ListingCandidate]:
    card_id = card_soup.get("id", "")
    m = re.search(r"message_(\d+)", card_id)
    if not m:
        return None
    listing_id = m.group(1)

    title_a = card_soup.select_one(".offer-title h3 a")
    if not title_a:
        return None

    location_div = card_soup.select_one(".offer-location")
    city = None
    if location_div:
        for node in location_div.contents:
            if isinstance(node, NavigableString) and not isinstance(node, Comment):
                txt = node.strip()
                if txt:
                    city = txt
                    break

    date_span = card_soup.select_one(".offer-details .offer-date")
    preview_dt = parse_preview_date(date_span.get_text()) if date_span else None

    return ListingCandidate(
        id=listing_id,
        title=title_a.get_text(strip=True),
        url=urljoin(page_url, title_a["href"]),
        city=city,
        preview_category=category_name,
        preview_dt=preview_dt,
    )


def parse_category_page_html(
    html: str,
    page_url: str,
    category_name: str,
) -> tuple[list[ListingCandidate], list[str]]:
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select("div.offerlist div.offer.bl-tb[id^='message_']")
    candidates: list[ListingCandidate] = []

    for card in cards:
        cand = parse_category_card(card, category_name, page_url)
        if cand:
            candidates.append(cand)

    pagination_links = get_pagination_links(soup, page_url)
    return candidates, pagination_links
