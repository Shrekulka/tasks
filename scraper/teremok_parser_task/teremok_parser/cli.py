from __future__ import annotations

import argparse


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Teremok Classifieds Data Extractor & Pipeline")
    parser.add_argument(
        "--mode",
        choices=["strict_newest", "quota_coverage"],
        default="quota_coverage",
        help=(
            "Стратегія відбору: strict_newest (200 найновіших оголошень "
            "за глобальною датою) або quota_coverage "
            "(гарантоване покриття категорій із подальшим добором найновіших)."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Діагностичний режим: детальний DEBUG-лог та збереження RAW HTML у output/raw/<run_id>/.",
    )
    parser.add_argument(
        "--fail-fast",
        dest="fail_fast",
        action="store_true",
        default=True,
        help="Блокувати експорт файлів даних, якщо Hard Gate валідація виявила невідповідність.",
    )

    parser.add_argument(
        "--no-fail-fast",
        dest="fail_fast",
        action="store_false",
        help="Не блокувати експорт при невідповідності Hard Gate.",
    )
    return parser.parse_args()
