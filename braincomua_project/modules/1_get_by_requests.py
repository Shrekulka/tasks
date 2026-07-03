# braincomua_project/modules/1_get_by_requests.py

"""
Parser #1: collects product data from a single brain.com.ua product page
using Requests + BeautifulSoup4.
"""
from pprint import pprint
import os
import requests
from bs4 import BeautifulSoup
from load_django import *
from parser_app.models import Product
from utils import clean_text, clean_price

headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,ru-RU;q=0.8,ru;q=0.7,en-US;q=0.6,en;q=0.5",
    "Referer": "https://www.google.com/",
    "Connection": "keep-alive",
    "Cache-Control": "max-age=0",
    "Upgrade-Insecure-Requests": "1",
    "DNT": "1",
}

# Declare cookies according to the formatting requirements
cookies = {
    # For a basic GET request to the product page, cookies are not required,
    # but the dictionary is declared and passed to comply with the regulations.
}

URL = "https://brain.com.ua/ukr/Mobilniy_telefon_Apple_iPhone_16_Pro_Max_256GB_Black_Titanium-p1145443.html"


def get_value_by_label(scope, label_text, tag_with_label="span", tag_with_value="span"):
    """
    Finds a specification value by locating its label text first (case-insensitive)
    INSIDE the given scope element, then reading the next sibling tag.
    """
    if not scope:
        return None
    label_tag = scope.find(tag_with_label, string=lambda t: t and label_text.lower() in t.lower())
    if not label_tag:
        return None
    value_tag = label_tag.find_next_sibling(tag_with_value)
    return clean_text(value_tag.text) if value_tag else None


def parse_product():
    response = requests.get(URL, headers=headers, cookies=cookies, timeout=15)

    if response.status_code != 200:
        print(f"Request failed with status code: {response.status_code}")
        return

    soup = BeautifulSoup(response.text, "lxml")
    product = {}

    # Find the specifications container once and reuse it for label-based searches
    specs_container = soup.find(id="br-characteristics") or soup.find(class_="br-characteristics-wrapper")

    try:
        product["title"] = clean_text(soup.find("h1", class_="desktop-only-title").text)
    except AttributeError:
        product["title"] = None

    # Collecting the promo price (promo_price) — flat block
    try:
        red_price_elem = soup.find("span", class_="red-price")
        product["promo_price"] = clean_price(red_price_elem.text) if red_price_elem else None
    except AttributeError:
        product["promo_price"] = None

    # Collecting the regular price (price) — flat block
    try:
        if soup.find("span", class_="red-price"):
            # If there is a promo, the regular price is in the old block (.br-pr-op)
            old_price_block = soup.find("div", class_="br-pr-op")
            product["price"] = clean_price(old_price_block.text) if old_price_block else None
        else:
            # If there is no promo, the price is in .price-wrapper
            price_wrapper = soup.find("div", class_="price-wrapper")
            price_span = price_wrapper.find("span") if price_wrapper else None
            product["price"] = clean_price(price_span.text) if price_span else None
    except AttributeError:
        product["price"] = None

    # Vendor (search inside the specs container or via breadcrumbs)
    try:
        vendor = get_value_by_label(specs_container, "Виробник")
        if not vendor:
            breadcrumbs = soup.find_all("span", itemprop="name")
            for i, bc in enumerate(breadcrumbs):
                bc_text = bc.get_text(strip=True)
                if "телефон" in bc_text.lower() or "смартфон" in bc_text.lower():
                    if i + 1 < len(breadcrumbs):
                        vendor = breadcrumbs[i + 1].get_text(strip=True)
                        break
        product["vendor"] = clean_text(vendor) if vendor else None
    except (AttributeError, IndexError):
        product["vendor"] = None

    try:
        product["product_code"] = clean_text(soup.find("span", class_="br-pr-code-val").text)
    except AttributeError:
        product["product_code"] = None

    try:
        reviews_elem = soup.find("a", class_="reviews-count")
        if reviews_elem:
            num_span = reviews_elem.find("span")
            if num_span:
                product["reviews_count"] = int(num_span.text.strip())
            else:
                reviews_text = reviews_elem.text.strip()
                digits = "".join(ch for ch in reviews_text if ch.isdigit())
                product["reviews_count"] = int(digits) if digits else None
        else:
            product["reviews_count"] = None
    except (AttributeError, ValueError):
        product["reviews_count"] = None

    product["color"] = get_value_by_label(specs_container, "Колір")
    product["memory"] = get_value_by_label(specs_container, "Вбудована пам")
    product["screen_diagonal"] = get_value_by_label(specs_container, "Діагональ екрану")
    product["screen_resolution"] = get_value_by_label(specs_container, "Роздільна здатність")

    try:
        gallery = soup.find("div", class_="br-pic-block") or soup.find("div", class_="main-pictures-block")
        image_urls = []
        if gallery:
            for img in gallery.find_all("img"):
                src = img.get("src") or img.get("data-src") or img.get("data-lazy")
                if src:
                    if src.startswith("/"):
                        src = "https://brain.com.ua" + src
                    image_urls.append(src)
        unique_urls = list(dict.fromkeys(image_urls))
        product["image_urls"] = unique_urls if unique_urls else None
    except AttributeError:
        product["image_urls"] = []

    try:
        specifications = {}
        if specs_container:
            for div in specs_container.find_all("div"):
                spans = [span for span in div.find_all("span", recursive=False) if span.text.strip()]
                if len(spans) == 2:
                    key = clean_text(spans[0].text)
                    value = clean_text(spans[1].text)
                    if key and value:
                        specifications[key] = value
        product["specifications"] = specifications if specifications else None
    except AttributeError:
        product["specifications"] = {}

    product["status"] = "Done"
    product["link"] = URL

    pprint(product)

    link = product.pop("link")

    # Two-step save per project convention (Сбор данных страницы.docx / Models.docx):
    # 1) get_or_create with ONLY the unique field, no defaults, no other data;
    # 2) enrich the object via plain attribute assignment + save().
    obj, created = Product.objects.get_or_create(link=link)
    for key, value in product.items():
        setattr(obj, key, value)
    obj.save()
    print("✅ Created new record in Database" if created else "🔁 Updated existing record in Database")


if __name__ == "__main__":
    parse_product()

