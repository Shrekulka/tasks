from __future__ import annotations

MISSING = "немає даних"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7",
}

RETRY_STATUS_FORCELIST = (408, 425, 429, 500, 502, 503, 504)

CATEGORIES = {
    "Квартири, кімнати": "/Nedvizhimost/apartmen/",
    "Мобільні телефони": "/Elektronika-i-bytovaya-tehnika/Mobilnye-telefony/",
    "Продаж транспорту": "/Avtotransport/Prodazha-transporta/",
    "Меблі для кухні": "/Mebel/Mebel-dlya-kuhni/",
    "Дитячий одяг": "/Vse-dlya-detei/Detskaya-odezhda/",
}

UA_MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}
RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
ALL_MONTHS = {**UA_MONTHS, **RU_MONTHS}

DETAIL_SELECTORS = {
    "title": ".product_info h1",
    "description": ".product_description",
    "price": ".product_price",
    "city": ".product_region-bl",
    "breadcrumbs": ".breadcrumb li a",
    "images": ".fotorama a",
}
