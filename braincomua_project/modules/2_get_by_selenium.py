# braincomua_project/modules/2_get_by_selenium.py

"""
Parser #2: searches for a product on brain.com.ua, clicks the first result,
and collects data using Selenium.
"""
import traceback
from pprint import pprint

from load_django import *
from parser_app.models import Product

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException

from utils import clean_text, clean_price

SEARCH_QUERY = "Apple iPhone 15 128GB Black"
BASE_URL = "https://brain.com.ua/ukr/"


def get_selenium_value_by_label(scope, label_text):
    """
    Finds a specification value INSIDE the given scope element (the specs
    container, found once and reused) by locating a label span via
    case-insensitive text match, then reading its next sibling span.
    """
    if not scope:
        return None
    try:
        xpath = f".//span[contains(translate(text(), 'АБВГДЕЄЖЗИІЇЙКЛМНОПРСТУФХЦЧШЩЬЮЯ', 'абвгдеєжзиіїйклмнопрстуфхцчшщьюя'), '{label_text.lower()}')]/following-sibling::span[1]"
        element = scope.find_element(By.XPATH, xpath)
        return clean_text(element.get_attribute("textContent"))
    except NoSuchElementException:
        return None


def main():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-gpu")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

    driver = webdriver.Chrome(options=options)
    wait = WebDriverWait(driver, 15)

    try:
        print(f"Opening home page: {BASE_URL}")
        driver.get(BASE_URL)

        print(f"Searching for: {SEARCH_QUERY}")
        wait.until(
            EC.presence_of_all_elements_located(
                (By.XPATH, "//input[@placeholder='Знайти...' or @class='quick-search-input']"))
        )
        search_inputs = driver.find_elements(By.XPATH,
                                             "//input[@placeholder='Знайти...' or @class='quick-search-input']")
        search_input = next((inp for inp in search_inputs if inp.is_displayed()), None)
        if not search_input:
            raise NoSuchElementException("No visible search input found on the page.")

        search_input.clear()
        search_input.send_keys(SEARCH_QUERY)

        try:
            # The real submit button class, confirmed via DevTools inspection,
            # is "search-button-first-form" (an <input type="submit">).
            search_button = wait.until(
                EC.presence_of_element_located((By.XPATH,
                                                "//input[contains(@class, 'search-button-first-form')] | //input[@class='quick-search-submit'] | //button[contains(@class, 'search-submit')] | //input[@type='submit' and @value='Знайти']"))
            )
            # A plain .click() can fail here with ElementClickInterceptedException:
            # the fixed/sticky site header can place another overlapping element
            # (an alternate quick-search input, "qsr-input") on top of the button
            # at its click coordinates, depending on viewport width. Scrolling the
            # button into view first, then dispatching a native JS click, bypasses
            # Selenium's strict overlap check — the same technique already used
            # for the specifications tab further down in this script.
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", search_button)
            driver.execute_script("arguments[0].click();", search_button)
            print("Successfully clicked the search button.")
        except (NoSuchElementException, TimeoutException):
            print("⚠️ Warning: Could not click search button directly, trying form submit fallback.")
            search_input.submit()

        print("Waiting for search results...")
        first_product_xpath = (
            "//div[contains(@class, 'product-wrapper') or contains(@class, 'product_wrapper') or "
            "contains(@class, 'br-pcg-product-wrapper')]//a[contains(@href, '-p') and contains(@href, '.html')] | "
            "//a[contains(@class, 'product-title') or contains(@class, 'product_title') or "
            "contains(@class, 'br-product-title') or contains(@class, 'br-pr-title')] | "
            "//h3/a[contains(@href, '-p')] | //h4/a[contains(@href, '-p')]"
        )
        first_product_link = wait.until(EC.element_to_be_clickable((By.XPATH, first_product_xpath)))
        product_url = first_product_link.get_attribute("href")
        print(f"Opening first result: {product_url}")
        first_product_link.click()

        wait.until(EC.presence_of_element_located((By.CLASS_NAME, "desktop-only-title")))

        # Find and open the specifications tab once
        specs_container_el = None
        try:
            specs_tab = wait.until(
                EC.presence_of_element_located(
                    (By.XPATH, "//a[contains(text(), 'Характеристики') or contains(@href, 'characteristics')]"))
            )
            driver.execute_script("arguments[0].scrollIntoView(true);", specs_tab)
            driver.execute_script("arguments[0].click();", specs_tab)
            specs_container_el = wait.until(
                EC.presence_of_element_located(
                    (By.XPATH, "//div[@id='br-characteristics'] | //div[contains(@class, 'br-characteristics-wrapper')]"))
            )
            print("Successfully switched to specifications tab.")
        except (NoSuchElementException, TimeoutException, WebDriverException) as e:
            print(f"⚠️ Warning: Could not click on specs tab: {e}")

        product = {}

        try:
            title_elem = driver.find_element(By.CLASS_NAME, "desktop-only-title")
            product["title"] = clean_text(title_elem.get_attribute("textContent"))
        except NoSuchElementException:
            product["title"] = None

        # Find the price container once and reuse it for both promo_price and price
        try:
            price_container = driver.find_element(By.CLASS_NAME, "price-bonuses-body")
            red_price_elements = [el for el in price_container.find_elements(By.CLASS_NAME, "red-price")
                                  if el.get_attribute("textContent").strip()]
        except NoSuchElementException:
            price_container = None
            red_price_elements = []

        # Collecting the promo price (promo_price)
        try:
            product["promo_price"] = clean_price(
                red_price_elements[0].get_attribute("textContent")) if red_price_elements else None
        except NoSuchElementException:
            product["promo_price"] = None

        # Collecting the regular price (price)
        try:
            if red_price_elements:
                old_price_element = driver.find_element(By.CLASS_NAME, "br-pr-op")
                product["price"] = clean_price(old_price_element.get_attribute("textContent"))
            elif price_container is not None:
                price_wrapper = driver.find_element(By.CLASS_NAME, "price-wrapper")
                product["price"] = clean_price(price_wrapper.get_attribute("textContent"))
            else:
                product["price"] = None
        except NoSuchElementException:
            product["price"] = None

        try:
            vendor = get_selenium_value_by_label(specs_container_el, "Виробник")
            if not vendor:
                breadcrumbs = driver.find_elements(By.XPATH, "//div[@class='br-breadcrumbs']//span[@itemprop='name']")
                for i, bc in enumerate(breadcrumbs):
                    bc_text = bc.get_attribute("textContent").strip()
                    if "телефон" in bc_text.lower() or "смартфон" in bc_text.lower():
                        if i + 1 < len(breadcrumbs):
                            vendor = breadcrumbs[i + 1].get_attribute("textContent").strip()
                            break
            product["vendor"] = clean_text(vendor) if vendor else None
        except (NoSuchElementException, IndexError):
            product["vendor"] = None

        try:
            code_elem = driver.find_element(By.CLASS_NAME, "br-pr-code-val")
            product["product_code"] = clean_text(code_elem.get_attribute("textContent"))
        except NoSuchElementException:
            product["product_code"] = None

        try:
            reviews_elem = driver.find_element(By.CLASS_NAME, "reviews-count")
            reviews_text = reviews_elem.get_attribute("textContent").strip()
            digits = "".join(ch for ch in reviews_text if ch.isdigit())
            product["reviews_count"] = int(digits) if digits else None
        except NoSuchElementException:
            product["reviews_count"] = None

        product["color"] = get_selenium_value_by_label(specs_container_el, "Колір")
        product["memory"] = get_selenium_value_by_label(specs_container_el, "Вбудована пам")
        product["screen_diagonal"] = get_selenium_value_by_label(specs_container_el, "Діагональ екрану")
        product["screen_resolution"] = get_selenium_value_by_label(specs_container_el, "Роздільна здатність")

        try:
            gallery = driver.find_element(By.XPATH,
                                          "//div[@class='br-pic-block'] | //div[contains(@class, 'main-pictures-block')]")
            image_urls = []
            for img in gallery.find_elements(By.TAG_NAME, "img"):
                src = img.get_attribute("src") or img.get_attribute("data-src") or img.get_attribute("data-lazy")
                if src:
                    if src.startswith("/"):
                        src = "https://brain.com.ua" + src
                    image_urls.append(src)
            unique_urls = list(dict.fromkeys(image_urls))
            product["image_urls"] = unique_urls if unique_urls else None
        except NoSuchElementException:
            product["image_urls"] = []

        try:
            specifications = {}
            if specs_container_el:
                for item in specs_container_el.find_elements(By.XPATH, ".//div[span[2]]"):
                    try:
                        spans = item.find_elements(By.XPATH, "./span")
                        if len(spans) == 2:
                            key = clean_text(spans[0].get_attribute("textContent"))
                            value = clean_text(spans[1].get_attribute("textContent"))
                            if key and value:
                                specifications[key] = value
                    except NoSuchElementException:
                        continue
            product["specifications"] = specifications if specifications else None
        except NoSuchElementException:
            product["specifications"] = {}

        product["status"] = "Done"
        product["link"] = product_url

        print("\n--- Gathered Data (Selenium) ---")
        pprint(product)

        link = product.pop("link")

        obj, created = Product.objects.get_or_create(link=link)
        for key, value in product.items():
            setattr(obj, key, value)
        obj.save()
        print("✅ Created new record in Database" if created else "🔁 Updated existing record in Database")

    except TimeoutException:
        print("❌ Timeout occurred while waiting for page elements.")
    except (NoSuchElementException, WebDriverException) as e:
        print(f"❌ An error occurred during scraping: {e}")
        traceback.print_exc()
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
