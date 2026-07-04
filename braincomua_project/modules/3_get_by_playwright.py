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
from django.db import Error as DjangoDBError
from parser_app.models import Product

from playwright.sync_api import sync_playwright, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from utils import clean_text, clean_price

SEARCH_QUERY = "Apple iPhone 15 128GB Black"
BASE_URL = "https://brain.com.ua/ukr/"

# Confirmed by hand via the DevTools Network tab on the live page: it fires
# ~235 background requests (Google Tag Manager, Analytics, doubleclick.net
# trackers, etc.). Playwright's default page.goto() wait_until="load" blocks
# until the browser's "load" event, which only fires once *every* resource on
# the page has finished (including slow/hanging third-party trackers). That
# event took longer than 30s in an earlier run and produced
# "Page.goto: Timeout 30000ms exceeded". "domcontentloaded" only waits for
# the HTML/DOM to be parsed, which is enough here since the script already
# waits explicitly for the specific elements it needs (search input, product
# link, title, etc.) right after.
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
            # networkidle is NOT used here on purpose: confirmed via the same
            # DevTools Network tab inspection mentioned above that this site
            # keeps ~235 background connections alive (analytics, trackers,
            # websockets), so the browser's "0 network connections for 500ms"
            # condition for networkidle may never be met, causing this line to
            # time out even though the page is fully usable. A short fixed
            # wait is a more reliable proxy for "JS handlers have attached" on
            # this particular site.
            page.wait_for_timeout(1500)

            print(f"Searching for: {SEARCH_QUERY}")
            search_input = page.locator("input.quick-search-input:visible").first
            search_input.wait_for(state="visible", timeout=15000)
            search_input.fill(SEARCH_QUERY)

            try:
                # Class chosen by hand, not guessed: had 4 candidate class
                # names for this button originally. Ran a standalone
                # diagnostic script against the live page that, for each of
                # the 4 candidates separately, printed the actual Locator
                # .count() and .is_visible() result. Only
                # 'search-button-first-form' printed count=1, visible=True —
                # the others either matched nothing at all, or matched a
                # hidden duplicate input (class='qsr-submit', visible=False)
                # that risks a click failure if it were ever picked instead of
                # the real, visible button. The class used below is the one
                # the diagnostic output actually confirmed.
                search_button = page.locator("//input[contains(@class, 'search-button-first-form')]").first
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
            # pattern — this pattern was read directly off the browser's address
            # bar after manually performing the same search in a real (non-headless)
            # browser and observing where it actually landed.
            if "search" not in page.url or "Search=" not in page.url:
                print("⚠️ Warning: Click did not land on the search results page "
                      f"(got '{page.url}'). Falling back to direct navigation.")
                search_url = BASE_URL + f"search/?Search={quote(SEARCH_QUERY)}"
                page.goto(search_url, wait_until=NAV_WAIT_UNTIL, timeout=NAV_TIMEOUT)

            print(f"Current URL after search: {page.url}")

            print("Waiting for search results...")
            # Container structure identified by hand on the live search-results
            # page: opened DevTools on the real results grid, expanded the DOM
            # around the first product card, and read off the real container
            # classes ("view-grid", "tab-pane", "active") plus the data-pid
            # attribute present on each card's own wrapper div. Then wrote a
            # diagnostic script that ran this exact XPath against the live
            # page and printed the resulting href for the first match,
            # comparing it against the product actually shown first on
            # screen. That diagnostic run is also what revealed the need for
            # the extra 'tab-pane' + 'active' condition below: a plain
            # 'view-grid' substring check alone also matched an unrelated
            # view-switcher toggle link (<li class="view-grid-link active">)
            # elsewhere on the page, which has no product cards inside it.
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
                specs_tab = page.locator("//a[contains(@href, 'characteristics')]").first
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
                    "//div[@id='br-characteristics']").first
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
                # Confirmed by hand via View Page Source on the live product
                # page: the gallery container's actual "class" attribute value
                # has several space-separated tokens (e.g.
                # "br-pic-block br-elem-block slick-initialized"), so an exact
                # @class="br-pic-block" match would fail on the real page —
                # contains() is required, not assumed as a "safe default".
                gallery = page.locator("//div[contains(@class, 'br-pic-block')]").first
                image_urls = []
                if gallery.count() > 0:
                    img_tags = gallery.locator("img")
                    for i in range(img_tags.count()):
                        img = img_tags.nth(i)
                        src = img.get_attribute("src") or img.get_attribute("data-src") or img.get_attribute(
                            "data-lazy")
                        if not src:
                            continue
                        # Same finding as in 1_get_by_requests.py and
                        # 2_get_by_selenium.py, re-confirmed here on this
                        # engine too by printing every img src collected
                        # inside this gallery block on this specific product
                        # page: it holds images for MULTIPLE product variants
                        # at once, and a data-pid attribute is only present on
                        # the 3 full-size slides, missing from this product's
                        # own thumbnails — so data-pid can't be used as the
                        # filter either. The one marker present on every one
                        # of this product's images — both full-size and
                        # thumbnail — is the product_code in the filename.
                        if product.get("product_code") and product["product_code"] not in src:
                            continue
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
            try:
                obj, created = Product.objects.get_or_create(link=link)
                for key, value in product.items():
                    setattr(obj, key, value)
                obj.save()
            except DjangoDBError as e:
                print(f"❌ Database error while saving the product: {e}")
                return

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