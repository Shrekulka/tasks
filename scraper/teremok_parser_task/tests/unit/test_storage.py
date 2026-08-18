import tempfile
from pathlib import Path

from teremok_parser.storage.raw_storage import RawHtmlStorage


def test_raw_storage_debug_off_creates_no_files_or_directories():
    """Тест: DEBUG=False -> каталог output/raw не створюється взагалі."""
    with tempfile.TemporaryDirectory() as tmp:
        raw_dir = Path(tmp) / "output" / "raw" / "run_test_off"
        storage = RawHtmlStorage(run_raw_dir=raw_dir, enabled=False)

        res_cat = storage.save_category_page("Квартири, кімнати", 1, "<html>category</html>")
        res_list = storage.save_listing_page("12345", "<html>listing</html>")

        assert res_cat is None
        assert res_list is None
        assert not raw_dir.exists()


def test_raw_storage_debug_on_saves_category_and_listing_html():
    """Тест: DEBUG=True -> зберігаються і сторінки категорій, і картки оголошень."""
    with tempfile.TemporaryDirectory() as tmp:
        raw_dir = Path(tmp) / "output" / "raw" / "run_test_on"
        storage = RawHtmlStorage(run_raw_dir=raw_dir, enabled=True)

        cat_path = storage.save_category_page("Мобільні телефони", 1, "<html>category page 1</html>")
        list_path = storage.save_listing_page("501667", "<html>detail 501667</html>")

        assert raw_dir.exists()
        assert cat_path is not None and cat_path.exists()
        assert "mobilni_telefony" in str(cat_path)
        assert cat_path.read_text(encoding="utf-8") == "<html>category page 1</html>"

        assert list_path is not None and list_path.exists()
        assert list_path.name == "501667.html"
        assert list_path.read_text(encoding="utf-8") == "<html>detail 501667</html>"


def test_raw_storage_deduplication_prevents_duplicate_writes():
    """Тест блокування повторного запису тих самих сторінок у межах запуску."""
    with tempfile.TemporaryDirectory() as tmp:
        raw_dir = Path(tmp) / "output" / "raw" / "run_test_dedup"
        storage = RawHtmlStorage(run_raw_dir=raw_dir, enabled=True)

        p1 = storage.save_category_page("Продаж транспорту", 1, "<html>page 1</html>")
        p2 = storage.save_category_page("Продаж транспорту", 1, "<html>page 1 duplicate</html>")
        assert p1 is not None
        assert p2 is None

        l1 = storage.save_listing_page("99999", "<html>item 99999</html>")
        l2 = storage.save_listing_page("99999", "<html>item 99999 duplicate</html>")
        assert l1 is not None
        assert l2 is None
