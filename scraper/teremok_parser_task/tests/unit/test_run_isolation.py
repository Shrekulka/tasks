import tempfile
from pathlib import Path

from teremok_parser.config import Settings
from teremok_parser.models import ParseStatus, SelectionMode
import teremok_parser.pipeline.scraper as scraper_module
from teremok_parser.pipeline.scraper import TeremokScraper
from teremok_parser.storage.raw_storage import RawHtmlStorage


class MockHttpClient:
    def __init__(self) -> None:
        self.session = None  # check_robots_allowed монкіпатчиться, тому не використовується

    def fetch_html(self, url: str):
        if "category" in url:
            html = """
            <div class="offerlist">
                <div class="offer bl-tb" id="message_100">
                    <div class="offer-title"><h3><a href="/ad/100.html">Товар 100</a></h3></div>
                    <div class="offer-location">Київ</div>
                    <div class="offer-details"><span class="offer-date">17 серпня 2026 в 12:00</span></div>
                </div>
            </div>
            """
            return html, ParseStatus.SUCCESS, None
        return (
            '<div class="product_info"><h1>Товар 100</h1></div><div class="product_price">100 грн</div>',
            ParseStatus.SUCCESS,
            None,
        )

    def polite_sleep(self) -> None:
        pass


def test_two_independent_runs_produce_isolated_artifacts(monkeypatch):
    """run_A та run_B не перезаписують та не змішують артефакти одне одного."""
    monkeypatch.setattr(scraper_module, "CATEGORIES", {"TestCat": "/category/"})
    monkeypatch.setattr(scraper_module, "check_robots_allowed", lambda session, url: True)

    with tempfile.TemporaryDirectory() as tmp:
        base_dir = Path(tmp)
        client = MockHttpClient()

        run_id_a = "2026-08-17_10-00-00-111111"
        run_id_b = "2026-08-17_10-00-00-222222"

        data_a = base_dir / "output" / "data" / run_id_a
        data_b = base_dir / "output" / "data" / run_id_b

        raw_a = base_dir / "output" / "raw" / run_id_a
        raw_b = base_dir / "output" / "raw" / run_id_b

        config = Settings(
            target_total=1,
            min_per_category=1,
            min_categories_required=1,
            debug=True,
        )

        storage_a = RawHtmlStorage(run_raw_dir=raw_a, enabled=True)
        scraper_a = TeremokScraper(
            client=client, config=config, mode=SelectionMode.STRICT_NEWEST, run_id=run_id_a,
            raw_storage=storage_a, data_dir=data_a
        )
        scraper_a.run()

        storage_b = RawHtmlStorage(run_raw_dir=raw_b, enabled=True)
        scraper_b = TeremokScraper(
            client=client, config=config, mode=SelectionMode.STRICT_NEWEST, run_id=run_id_b,
            raw_storage=storage_b, data_dir=data_b
        )
        scraper_b.run()

        assert raw_a.exists() and raw_b.exists()
        assert raw_a != raw_b
        assert (raw_a / "listings" / "100.html").exists()
        assert (raw_b / "listings" / "100.html").exists()
