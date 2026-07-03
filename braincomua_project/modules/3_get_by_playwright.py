# braincomua_project/modules/3_get_by_playwright.py

"""
Parser #3: searches for a product on brain.com.ua, clicks the first result,
and collects data using Playwright (Synchronous API).
"""
from pprint import pprint
import os
from urllib.parse import quote
from load_django import *

os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
from parser_app.models import Product

from playwright.sync_api import sync_playwright, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from utils import clean_text, clean_price

SEARCH_QUERY = "Apple iPhone 15 128GB Black"
BASE_URL = "https://brain.com.ua/ukr/"

# The page fires ~235 background requests (Google Tag Manager, Analytics,
# doubleclick.net trackers, etc. — confirmed via DevTools Network tab).
# Playwright's default page.goto() wait_until="load" blocks until the
# browser's "load" event, which only fires once *every* resource on the
# page has finished (including slow/hanging third-party trackers). That
# event can take far longer than 30s or never fire cleanly, which is what
# caused "Page.goto: Timeout 30000ms exceeded" in an earlier run.
# "domcontentloaded" only waits for the HTML/DOM to be parsed, which is
# enough here since the script already waits explicitly for the specific
# elements it needs (search input, product link, title, etc.) right after.
NAV_TIMEOUT = 60000
NAV_WAIT_UNTIL = "domcontentloaded"


def get_playwright_value_by_label(scope, label_text):
    """
    Finds a specification value INSIDE the given scope Locator (the specs
    container, found once and reused) by locating a label span via
    case-insensitive text match, then reading its next sibling span.
    """
    if scope is None:
        return None
    try:
        xpath = f".//span[contains(translate(text(), 'АБВГДЕЄЖЗИІЇЙКЛМНОПРСТУФХЦЧШЩЬЮЯ', 'абвгдеєжзиіїйклмнопрстуфхцчшщьюя'), '{label_text.lower()}')]/following-sibling::span[1]"
        element = scope.locator(f"xpath={xpath}").first
        element.wait_for(state="attached", timeout=3000)
        return clean_text(element.text_content())
    except (PlaywrightError, PlaywrightTimeoutError):
        return None


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
        )
        # Apply the same relaxed navigation timeout to every navigation
        # triggered within this context (goto, link clicks that navigate, etc.)
        context.set_default_navigation_timeout(NAV_TIMEOUT)
        page = context.new_page()

        try:
            print(f"Opening home page: {BASE_URL}")
            page.goto(BASE_URL, wait_until=NAV_WAIT_UNTIL, timeout=NAV_TIMEOUT)
            # Give the page's own search JS handler time to attach before interacting.
            # networkidle is NOT used here on purpose: this site keeps ~235
            # background connections alive (analytics, trackers, websockets —
            # confirmed via DevTools Network tab), so the browser's "0 network
            # connections for 500ms" condition for networkidle may never be
            # met, causing this line to time out even though the page is
            # fully usable. A short fixed wait is a more reliable proxy for
            # "JS handlers have attached" on this particular site.
            page.wait_for_timeout(1500)

            print(f"Searching for: {SEARCH_QUERY}")
            search_input = page.locator("input.quick-search-input:visible").first
            search_input.wait_for(state="visible", timeout=15000)
            search_input.fill(SEARCH_QUERY)

            try:
                # The real submit button class, confirmed via DevTools inspection,
                # is "search-button-first-form" (an <input type="submit">).
                search_button = page.locator(
                    "//input[contains(@class, 'search-button-first-form')] | "
                    "//input[@class='quick-search-submit'] | //button[contains(@class, 'search-submit')] | "
                    "//input[@type='submit' and @value='Знайти']").first
                search_button.wait_for(state="visible", timeout=10000)
                search_button.click()
                print("Successfully clicked the search button.")
            except (PlaywrightError, PlaywrightTimeoutError):
                print("⚠️ Warning: Could not click search button directly, trying form Enter press fallback.")
                search_input.press("Enter")

            # Give the site's JS a moment to process the click/submit and navigate.
            page.wait_for_timeout(2000)

            # Safety net: the site's search relies on a JS click handler (the input
            # has no "name" attribute, so a native form submit produces an empty
            # query string). If that handler didn't fire in time under headless
            # automation, fall back to navigating directly to the known results URL
            # pattern, confirmed earlier via manual DevTools inspection.
            if "search" not in page.url or "Search=" not in page.url:
                print("⚠️ Warning: Click did not land on the search results page "
                      f"(got '{page.url}'). Falling back to direct navigation.")
                search_url = BASE_URL + f"search/?Search={quote(SEARCH_QUERY)}"
                page.goto(search_url, wait_until=NAV_WAIT_UNTIL, timeout=NAV_TIMEOUT)

            print(f"Current URL after search: {page.url}")

            print("Waiting for search results...")
            # Require 'tab-pane' together with 'active' to exclude the unrelated
            # view-switcher toggle link (<li class="view-grid-link active">), which
            # also matches a plain 'view-grid' substring check but has no product
            # cards inside it — confirmed via manual DOM inspection.
            first_product_xpath = (
                "//div[contains(@class, 'view-grid') and contains(@class, 'tab-pane') "
                "and contains(@class, 'active')]"
                "//div[@data-pid]"
                "//a[contains(@href, '.html')]"
            )

            # Wait for the results container itself to attach first — the grid can
            # render slightly after the initial page load, especially under headless
            # Chromium, so waiting directly for a descendant link can time out even
            # though the page is still legitimately loading.
            page.wait_for_selector(
                "div.view-grid.tab-pane.active", state="attached", timeout=20000
            )

            first_product_link = page.locator(first_product_xpath).first
            first_product_link.wait_for(state="visible", timeout=20000)
            product_url = first_product_link.get_attribute("href")
            if product_url and product_url.startswith("/"):
                product_url = "https://brain.com.ua" + product_url

            print(f"Opening first result: {product_url}")
            first_product_link.click()

            title_locator = page.locator(".desktop-only-title").first
            title_locator.wait_for(state="visible", timeout=NAV_TIMEOUT)

            # Switch to the specifications tab once
            specs_container = None
            try:
                specs_tab = page.locator(
                    "//a[contains(text(), 'Характеристики') or contains(@href, 'characteristics')]").first
                specs_tab.wait_for(state="attached", timeout=10000)
                specs_tab.scroll_into_view_if_needed()

                # Standard .click() times out here because this tab is a
                # scroll-anchor link (<a class="scroll-to-element-after">)
                # that fails Playwright's strict actionability/visibility
                # check even though a real browser treats it as clickable.
                # A native JS click bypasses that check, mirroring the
                # execute_script approach already used in Selenium.
                try:
                    specs_tab.click(timeout=5000)
                except PlaywrightTimeoutError:
                    specs_tab.evaluate("el => el.click()")

                specs_container = page.locator(
                    "//div[@id='br-characteristics'] | //div[contains(@class, 'br-characteristics-wrapper')]").first
                specs_container.locator("span").first.wait_for(state="attached", timeout=10000)
                print("Successfully switched to specifications tab.")
            except (PlaywrightError, PlaywrightTimeoutError) as e:
                print(f"⚠️ Warning: Could not click on specs tab: {e}")
                specs_container = None

            product = {}

            try:
                product["title"] = clean_text(title_locator.text_content())
            except (PlaywrightError, PlaywrightTimeoutError):
                product["title"] = None

            # Find the price container once and reuse it for both promo_price and price
            try:
                price_container = page.locator(".price-bonuses-body").first
                red_price = price_container.locator(".red-price").first
                has_promo = red_price.count() > 0 and red_price.text_content().strip() != ""
            except (PlaywrightError, PlaywrightTimeoutError):
                price_container = None
                red_price = None
                has_promo = False

            # Collecting the promo price (promo_price)
            try:
                product["promo_price"] = clean_price(red_price.text_content()) if has_promo else None
            except (PlaywrightError, PlaywrightTimeoutError):
                product["promo_price"] = None

            # Collecting the regular price (price)
            try:
                if has_promo:
                    old_price = price_container.locator(".br-pr-op").first
                    product["price"] = clean_price(old_price.text_content()) if old_price.count() > 0 else None
                elif price_container is not None:
                    price_wrapper = price_container.locator(".price-wrapper").first
                    product["price"] = clean_price(
                        price_wrapper.text_content()) if price_wrapper.count() > 0 else None
                else:
                    product["price"] = None
            except (PlaywrightError, PlaywrightTimeoutError):
                product["price"] = None

            try:
                vendor = get_playwright_value_by_label(specs_container, "Виробник")
                if not vendor:
                    breadcrumbs = page.locator("//div[@class='br-breadcrumbs']//*[self::span or self::a]")
                    count = breadcrumbs.count()
                    for i in range(count):
                        bc_text = breadcrumbs.nth(i).text_content().strip()
                        if "телефон" in bc_text.lower() or "смартфон" in bc_text.lower():
                            if i + 1 < count:
                                vendor = breadcrumbs.nth(i + 1).text_content().strip()
                                break
                product["vendor"] = clean_text(vendor) if vendor else None
            except (PlaywrightError, PlaywrightTimeoutError, IndexError):
                product["vendor"] = None

            try:
                code_elem = page.locator(".br-pr-code-val").first
                code_elem.wait_for(state="attached", timeout=3000)
                product["product_code"] = clean_text(code_elem.text_content())
            except (PlaywrightError, PlaywrightTimeoutError):
                product["product_code"] = None

            try:
                reviews_elem = page.locator(".reviews-count").first
                reviews_elem.wait_for(state="attached", timeout=3000)
                reviews_text = reviews_elem.text_content().strip()
                digits = "".join(ch for ch in reviews_text if ch.isdigit())
                product["reviews_count"] = int(digits) if digits else None
            except (PlaywrightError, PlaywrightTimeoutError, ValueError):
                product["reviews_count"] = None

            product["color"] = get_playwright_value_by_label(specs_container, "Колір")
            product["memory"] = get_playwright_value_by_label(specs_container, "Вбудована пам")
            product["screen_diagonal"] = get_playwright_value_by_label(specs_container, "Діагональ екрану")
            product["screen_resolution"] = get_playwright_value_by_label(specs_container, "Роздільна здатність")

            try:
                gallery = page.locator(
                    "//div[@class='br-pic-block'] | //div[contains(@class, 'main-pictures-block')]").first
                image_urls = []
                if gallery.count() > 0:
                    img_tags = gallery.locator("img")
                    for i in range(img_tags.count()):
                        img = img_tags.nth(i)
                        src = img.get_attribute("src") or img.get_attribute("data-src") or img.get_attribute(
                            "data-lazy")
                        if src:
                            if src.startswith("/"):
                                src = "https://brain.com.ua" + src
                            image_urls.append(src)
                unique_urls = list(dict.fromkeys(image_urls))
                product["image_urls"] = unique_urls if unique_urls else None
            except (PlaywrightError, PlaywrightTimeoutError):
                product["image_urls"] = []

            try:
                specifications = {}
                if specs_container is not None and specs_container.count() > 0:
                    div_locators = specs_container.locator("div")
                    for i in range(div_locators.count()):
                        div = div_locators.nth(i)
                        spans = div.locator("span")
                        if spans.count() == 2:
                            key = clean_text(spans.nth(0).text_content())
                            value = clean_text(spans.nth(1).text_content())
                            if key and value:
                                specifications[key] = value
                product["specifications"] = specifications if specifications else None
            except (PlaywrightError, PlaywrightTimeoutError):
                product["specifications"] = {}

            product["status"] = "Done"
            product["link"] = product_url

            print("\n--- Gathered Data (Playwright) ---")
            pprint(product)

            link = product.pop("link")

            obj, created = Product.objects.get_or_create(link=link)
            for key, value in product.items():
                setattr(obj, key, value)
            obj.save()
            print("✅ Created new record in Database" if created else "🔁 Updated existing record in Database")
        except PlaywrightTimeoutError as e:
            print(f"❌ Timeout while waiting for page/element: {e}")
        except PlaywrightError as e:
            print(f"❌ Playwright-level error occurred (browser/navigation/interaction): {e}")
        except Exception as e:
            print(f"❌ Unexpected non-Playwright error: {e}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
