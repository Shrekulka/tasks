from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

from teremok_parser.cli import parse_args
from teremok_parser.config import settings
from teremok_parser.export.csv_exporter import export_csv
from teremok_parser.export.excel_exporter import export_excel
from teremok_parser.export.json_exporter import export_json
from teremok_parser.export.report_exporter import export_report
from teremok_parser.http.client import HttpClient
from teremok_parser.http.session import build_session
from teremok_parser.logger_config import setup_logging
from teremok_parser.models import SelectionMode
from teremok_parser.pipeline.scraper import TeremokScraper
from teremok_parser.storage.raw_storage import RawHtmlStorage

logger = logging.getLogger("teremok_parser.main")


def main() -> None:
    args = parse_args()

    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S-%f")
    is_debug = args.debug or settings.debug
    log_level = "DEBUG" if is_debug else settings.log_level

    run_data_dir = settings.output_dir / "data" / run_id
    run_reports_dir = settings.output_dir / "reports" / run_id
    run_raw_dir = settings.output_dir / "raw" / run_id
    run_log_file = settings.log_dir / run_id / "teremok_parser.log"
    report_path = run_reports_dir / "teremok_report.json"

    setup_logging(level=log_level, log_to_file=True, log_file=run_log_file)

    run_data_dir.mkdir(parents=True, exist_ok=True)
    run_reports_dir.mkdir(parents=True, exist_ok=True)

    session = build_session(settings)
    client = HttpClient(session, settings)
    raw_storage = RawHtmlStorage(
        run_raw_dir=run_raw_dir,
        enabled=is_debug,
    )
    scraper = TeremokScraper(
        client=client,
        config=settings,
        mode=SelectionMode(args.mode),
        run_id=run_id,
        raw_storage=raw_storage,
        fail_fast=args.fail_fast,
        data_dir=run_data_dir,
        report_file=report_path,
        log_file=run_log_file,
    )

    try:
        listings, report = scraper.run()
    except Exception as e:
        logger.critical(f"Критична помилка виконання: {e}", exc_info=True)
        sys.exit(1)

    export_report(report, report_path)

    logger.info("\n" + "=" * 70)
    logger.info(f"RUN ID: {run_id} | РЕЖИМ: {report['run_metadata']['mode']} | DEBUG: {is_debug}")
    logger.info(f"СТАТУС: {'[PASS]' if report['hard_gate']['passed'] else '[NON-CONFORMING / WARN]'}")
    logger.info(
        f"Зібрано: {report['collected_total']} оголошень "
        f"у {report['categories_covered']} категоріях | "
        f"Дія: {report['hard_gate']['action']}"
    )
    logger.info(
        f"Дублікатів видалено: "
        f"{report['deduplication']['total_duplicates_removed']} "
        f"(exact={report['deduplication']['exact_duplicates_removed']}, "
        f"near={report['deduplication']['near_duplicates_removed']})"
    )
    logger.info("=" * 70)

    if report["hard_gate"]["action"] == "export_blocked":
        logger.error("Експорт файлів даних скасовано через прапорець --fail-fast.")
        sys.exit(1)

    xlsx_path = run_data_dir / "teremok_listings.xlsx"
    csv_path = run_data_dir / "teremok_listings.csv"
    json_path = run_data_dir / "teremok_listings.json"

    export_excel(listings, xlsx_path)
    export_csv(listings, csv_path)
    export_json(listings, json_path)

    logger.info(f"Дані збережено у: {run_data_dir}/ (teremok_listings.xlsx, .csv, .json)")
    logger.info(f"Звіт якості збережено у: {report_path}")
    logger.info(f"Лог запуску збережено у: {run_log_file}")
    if is_debug:
        logger.info(f"Сирий HTML збережено у: {run_raw_dir}/")


if __name__ == "__main__":
    main()
