import re
import tempfile
from pathlib import Path

from teremok_parser.config import Settings
from teremok_parser.models import ParseStatus, SelectionMode
import teremok_parser.pipeline.scraper as scraper_module
from teremok_parser.pipeline.scraper import TeremokScraper
from teremok_parser.storage.raw_storage import RawHtmlStorage


class ErrorProneHttpClient:
    def __init__(self, fail_detail_ids: set[str]):
        self.fail_detail_ids = fail_detail_ids
        self.session = None  # check_robots_allowed монкіпатчиться, тому не використовується

    def fetch_html(self, url: str):
        if "cat" in url:
            html = """
            <div class="offerlist">
                <div class="offer bl-tb" id="message_1"><div class="offer-title"><h3><a href="/ad/1.html">1</a></h3></div><div class="offer-details"><span class="offer-date">17 серпня 2026 в 12:00</span></div></div>
                <div class="offer bl-tb" id="message_2"><div class="offer-title"><h3><a href="/ad/2.html">2</a></h3></div><div class="offer-details"><span class="offer-date">17 серпня 2026 в 11:00</span></div></div>
                <div class="offer bl-tb" id="message_3"><div class="offer-title"><h3><a href="/ad/3.html">3</a></h3></div><div class="offer-details"><span class="offer-date">17 серпня 2026 в 10:00</span></div></div>
                <div class="offer bl-tb" id="message_4"><div class="offer-title"><h3><a href="/ad/4.html">4</a></h3></div><div class="offer-details"><span class="offer-date">17 серпня 2026 в 09:00</span></div></div>
            </div>
            """
            return html, ParseStatus.SUCCESS, None

        for fid in self.fail_detail_ids:
            if f"/ad/{fid}.html" in url:
                return None, ParseStatus.HTTP_ERROR, "HTTP 500 Internal Server Error"

        m = re.search(r"/ad/(\d+)\.html", url)
        listing_id = m.group(1) if m else "0"
        return (
            f'<div class="product_info"><h1>Успіх {listing_id}</h1></div>'
            f'<div class="product_price">{100 + int(listing_id)} грн</div>',
            ParseStatus.SUCCESS,
            None,
        )

    def polite_sleep(self) -> None:
        pass


def test_strict_newest_recovers_shortfall_from_reserve_after_http_error(monkeypatch):
    """При HTTP 500 на одній з карток strict_newest добирає наступного свіжого кандидата з резерву."""
    monkeypatch.setattr(scraper_module, "CATEGORIES", {"Cat1": "/cat1/"})
    monkeypatch.setattr(scraper_module, "check_robots_allowed", lambda session, url: True)

    # Картка '2' впаде з помилкою 500, але є картка '4' у резерві
    client = ErrorProneHttpClient(fail_detail_ids={"2"})
    config = Settings(target_total=3, min_per_category=1, min_categories_required=1, debug=False)

    with tempfile.TemporaryDirectory() as tmp:
        storage = RawHtmlStorage(run_raw_dir=Path(tmp), enabled=False)
        scraper = TeremokScraper(
            client=client, config=config, mode=SelectionMode.STRICT_NEWEST, run_id="rec_run",
            raw_storage=storage
        )

        final_listings, report = scraper.run()

        # Повинно зібрати 3 успішних (1, 3, 4)
        assert len(final_listings) == 3
        collected_ids = {item.id for item in final_listings}
        assert collected_ids == {"1", "3", "4"}
        assert scraper.metrics.detail_failed == 1
        assert scraper.metrics.reserve_successful == 1
        assert report["hard_gate"]["passed"] is True
        assert report["dataset_status"] == "CONFORMING"


def test_pipeline_handles_unrecoverable_shortfall_as_non_conforming(monkeypatch):
    """Якщо резерв вичерпано і цільова кількість не зібрана, Hard Gate переходить у NON_CONFORMING."""
    monkeypatch.setattr(scraper_module, "CATEGORIES", {"Cat1": "/cat1/"})
    monkeypatch.setattr(scraper_module, "check_robots_allowed", lambda session, url: True)

    # Картки 1, 2, 4 впадуть, залишиться лише картка 3
    client = ErrorProneHttpClient(fail_detail_ids={"1", "2", "4"})
    config = Settings(target_total=3, min_per_category=1, min_categories_required=1, debug=False)

    with tempfile.TemporaryDirectory() as tmp:
        storage = RawHtmlStorage(run_raw_dir=Path(tmp), enabled=False)
        scraper = TeremokScraper(
            client=client, config=config, mode=SelectionMode.STRICT_NEWEST, run_id="err_run",
            raw_storage=storage
        )

        final_listings, report = scraper.run()

        assert len(final_listings) == 1
        assert report["hard_gate"]["passed"] is False
        assert report["dataset_status"] == "NON_CONFORMING"
