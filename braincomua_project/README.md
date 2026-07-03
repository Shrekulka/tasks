# Brain.com.ua Product Scraper

A small test/training project that collects full information about a single smartphone from **[brain.com.ua](https://brain.com.ua/)** (a Ukrainian electronics retailer) using **three different scraping approaches**, and stores the results in a PostgreSQL database through Django.

It was built as a pre-internship technical assignment for a Python Backend Developer position, but the code and structure follow general production-style conventions and can be used as a reference for how to organize a Django-based scraping project.

---

## 🎯 What problem does this solve?

Websites like online stores don't offer a public API for their product data, so getting structured, up-to-date product information (price, specs, images, stock code, etc.) requires **web scraping** — programmatically reading and extracting data from the site's HTML.

There isn't one single "correct" way to scrape a website — the right tool depends on the situation:

- Sometimes the page is plain HTML and a simple HTTP request is enough.
- Sometimes the content only appears after JavaScript runs in a real browser.
- Sometimes you need to simulate a user typing into a search box and clicking a result.

This project demonstrates all three approaches **on the exact same task** — collecting one smartphone's full product page — so the results can be compared side by side.

---

## 🧩 What does the project actually do?

There are **three independent scripts**, each solving the same problem with a different tool:

| Script | Tool | What it does |
|---|---|---|
| `1_get_by_requests.py` | `requests` + `BeautifulSoup4` | Opens a direct product URL and parses the raw HTML — fastest, works only when the page doesn't require JavaScript |
| `2_get_by_selenium.py` | `Selenium` + real Chrome | Opens the homepage, types a search query, clicks the first result, and reads the resulting page — simulates a real user in a real browser |
| `3_get_by_playwright.py` | `Playwright` (headless Chromium) | Same search-and-click scenario as Selenium, but with a modern, faster automation engine |

All three scripts collect the **same 12 fields** about a product (title, color, memory, vendor, regular/promo price, all photo URLs, product code, review count, screen diagonal, screen resolution, and a full specifications table) and save them into the same PostgreSQL table — so no matter which tool is used, the end result is identical.

```
requests+BS4  ─┐
Selenium      ─┼──► same 12 product fields ──► PostgreSQL (via Django ORM) ──► CSV / DB dump
Playwright    ─┘
```

---

## 🛠️ Tech stack

- **Python 3.13+**
- **Django** — ORM and admin panel (no web pages are actually served; Django is used purely as a database layer + admin UI)
- **PostgreSQL** — data storage
- **BeautifulSoup4**, **Selenium**, **Playwright** — the three scraping engines
- **pgAdmin** — used for exporting results to CSV and creating the DB dump

---

## 📂 Project structure (short version)

```
braincomua_project/
├── config/          # Django settings
├── modules/         # The three scraper scripts + Django ORM bridge (load_django.py)
├── parser_app/      # Django app: the Product model + a customized admin panel
├── results/         # Exported CSV files and a database dump
└── docs/images/     # Screenshots proving each step works
```

A full breakdown of every file, plus step-by-step deployment instructions, is in **[django_setup_steps.md](./django_setup_steps.md)**.

---

## 🚀 Quick start

```bash
git clone <repo-url>
cd braincomua_project
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# set up PostgreSQL and .env — see django_setup_steps.md for exact commands
python manage.py migrate

cd modules
python 1_get_by_requests.py
python 2_get_by_selenium.py
python 3_get_by_playwright.py
```

👉 For the full, step-by-step version of the above (database creation, `.env` setup, admin panel features, exporting to CSV, DB dump) see **[django_setup_steps.md](./django_setup_steps.md)**.

---

## 🔍 How the data is actually found on the page

Scraping isn't just "grab the text" — the trickiest part is telling the code *exactly* where on the page each piece of data lives, in a way that won't break if the site's layout changes slightly. A few examples of the approach used here:

- Fields like **color**, **memory**, or **screen size** aren't found by "the 5th item in a list" (fragile — breaks the moment the site reorders anything). Instead, the code looks for the *label* (e.g. `"Діагональ екрану"`) and reads the value right next to it.
- The search results page has two visually similar elements — the actual grid of products, and an unrelated "switch view" button that happens to share some of the same CSS class names. The code checks for a specific *combination* of classes to make sure it's reading the real product grid.
- Every field is looked up independently, so if one piece of data is missing on a given product page, the rest of the fields are still collected correctly (missing values become `None` rather than crashing the whole script or leaving placeholder text like `"N/A"`).

A full field-by-field table of exactly which selector was used for which piece of data (and why) is documented separately in **`TASK_GUIDE.md`** (development notes, not meant for end users of this README).

---

## 📸 Proof of work

Screenshots showing successful migrations, database read/write tests, scraper execution logs, the customized Django admin panel, and the pgAdmin CSV export step are available in [`docs/images/`](./docs/images/) and referenced in [django_setup_steps.md](./django_setup_steps.md#-screenshot-checklist-for-the-instructor).

---
[django_setup_steps.md](django_setup_steps.md)[README.md](README.md)
## ⚠️ Notes

- This is a learning/demonstration project — it scrapes a single product per run, not the entire catalog.
- `.env` is required and is **not** committed to the repository; use `.env.example` as a template.
- Running the scrapers repeatedly is safe: each script updates the existing database record for that product (matched by its unique link) instead of creating duplicates.