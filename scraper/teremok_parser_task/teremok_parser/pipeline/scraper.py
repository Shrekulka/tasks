from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

from teremok_parser.config import Settings
from teremok_parser.constants import CATEGORIES
from teremok_parser.dedup import Deduplicator
from teremok_parser.exceptions import RobotsDeniedError
from teremok_parser.http.client import HttpClient
from teremok_parser.http.robots import check_robots_allowed
from teremok_parser.models import Listing, ListingCandidate, ParseStatus, SelectionMode
from teremok_parser.parser.category import parse_category_page_html
from teremok_parser.parser.date import sort_key
from teremok_parser.parser.listing import parse_listing_html
from teremok_parser.selection.selector import select_candidates
from teremok_parser.storage.raw_storage import RawHtmlStorage
from teremok_parser.validation.business_rules import validate_invariants
from teremok_parser.validation.metrics import ConsecutiveErrorGuard, RunMetrics

logger = logging.getLogger("teremok_parser.scraper")


class TeremokScraper:
    def __init__(
        self,
        client: HttpClient,
        config: Settings,
        mode: SelectionMode,
        run_id: str,
        raw_storage: RawHtmlStorage,
        fail_fast: bool = True,
        data_dir: Optional[Path] = None,
        report_file: Optional[Path] = None,
        log_file: Optional[Path] = None,
    ):
        self.client = client
        self.config = config
        self.mode = mode
        self.run_id = run_id
        self.raw_storage = raw_storage
        self.fail_fast = fail_fast
        self.data_dir = data_dir
        self.report_file = report_file
        self.log_file = log_file
        self.metrics = RunMetrics()
        self.dedup = Deduplicator(self.metrics)

    def _collect_category(self, category_name: str, path: str) -> list[ListingCandidate]:
        candidates: list[ListingCandidate] = []
        seen_ids: set[str] = set()
        url = urljoin(self.config.base_url, path)
        page_num = 1
        visited: set[str] = set()

        while url and url not in visited and page_num <= self.config.max_pages_per_category:
            visited.add(url)
            html, status, err = self.client.fetch_html(url)
            if html is None:
                self.metrics.record_error(status, url, err)
                break

            self.raw_storage.save_category_page(category_name, page_num, html)

            page_candidates, pagination_links = parse_category_page_html(
                html=html,
                page_url=url,
                category_name=category_name,
            )

            found = 0
            for cand in page_candidates:
                if cand.id not in seen_ids:
                    seen_ids.add(cand.id)
                    candidates.append(cand)
                    found += 1
                if len(candidates) >= self.config.candidates_per_category:
                    break

            logger.info(f"  [{category_name}] Стор. {page_num}: +{found} (всього: {len(candidates)})")
            if len(candidates) >= self.config.candidates_per_category:
                break

            url = next((link for link in pagination_links if link not in visited), None)
            page_num += 1
            self.client.polite_sleep()

        return candidates

    def _fetch_and_parse(self, url: str, listing_id: str, source_category: str):
        html, status, err = self.client.fetch_html(url)
        if html is None:
            return None, status, err

        self.raw_storage.save_listing_page(listing_id, html)

        res = parse_listing_html(html, url, listing_id, source_category)
        if res.status != ParseStatus.SUCCESS or res.listing is None:
            return None, res.status, res.error_msg
        return res.listing, ParseStatus.SUCCESS, None

    def _process_reserve_candidate(
        self,
        cand: ListingCandidate,
        final_listings: list[Listing],
        guard: ConsecutiveErrorGuard,
    ) -> bool:
        self.metrics.reserve_attempted += 1
        listing, status, err = self._fetch_and_parse(cand.url, cand.id, cand.preview_category)
        if listing is not None:
            self.metrics.reserve_successful += 1
            guard.record_success()
            if self.dedup.process(listing):
                final_listings.append(listing)
        else:
            self.metrics.reserve_failed += 1
            self.metrics.record_error(status, cand.url, err)
            if guard.record_failure():
                self.client.polite_sleep()
                return True
        self.client.polite_sleep()
        return False

    def run(self) -> tuple[list[Listing], dict[str, Any]]:
        logger.info(
            f"Старт скрапінгу Teremok | Run ID: {self.run_id} | "
            f"Режим: {self.mode.value} | Debug RAW: {self.raw_storage.enabled}"
        )

        if not check_robots_allowed(self.client.session, self.config.base_url):
            raise RobotsDeniedError("Сканування зупинено: доступ заборонено правилами robots.txt або джерело недоступне.")

        # 1. Збір кандидатів
        all_candidates: list[ListingCandidate] = []
        for name, path in CATEGORIES.items():
            all_candidates.extend(self._collect_category(name, path))

        self.metrics.raw_candidates_count = len(all_candidates)
        unique_map = {c.id: c for c in all_candidates}
        unique_candidates = list(unique_map.values())
        self.metrics.unique_candidates_count = len(unique_candidates)

        category_pool_size: dict[str, int] = dict(
            Counter(c.preview_category for c in unique_candidates)
        )

        selected, reserve_pool = select_candidates(
            unique_candidates,
            self.mode,
            target_total=self.config.target_total,
            min_per_category=self.config.min_per_category,
        )

        logger.info(f"Кандидатів унікальних: {len(unique_candidates)}, відібрано: {len(selected)}")

        # 2. Детальний парсинг основної вибірки
        final_listings: list[Listing] = []
        attempted_ids: set[str] = set()
        guard = ConsecutiveErrorGuard(limit=self.config.max_consecutive_errors, label="Detail Parsing")

        for i, cand in enumerate(selected, 1):
            attempted_ids.add(cand.id)
            self.metrics.detail_attempted += 1

            listing, status, err = self._fetch_and_parse(cand.url, cand.id, cand.preview_category)
            if listing is not None:
                self.metrics.detail_successful += 1
                guard.record_success()
                if self.dedup.process(listing):
                    final_listings.append(listing)
            else:
                self.metrics.detail_failed += 1
                self.metrics.record_error(status, cand.url, err)
                if guard.record_failure():
                    break

            if i % 25 == 0 or i == len(selected):
                logger.info(f"  Опрацьовано карток: {i}/{len(selected)} (унікальних зібрано: {len(final_listings)})")
            self.client.polite_sleep()

        # 3. Резервний добір
        if len(final_listings) < self.config.target_total:
            reserve_guard = ConsecutiveErrorGuard(
                limit=self.config.max_consecutive_errors,
                label="Reserve Backfill",
            )
            guard_tripped = False

            # 3a. QUOTA_COVERAGE: компенсація квоти кожної категорії
            if self.mode == SelectionMode.QUOTA_COVERAGE and not guard_tripped:
                current_counts = Counter(item.source_category for item in final_listings)
                logger.info(
                    f"Запуск резерву (фаза 1/2 — компенсація квоти категорій): "
                    f"зібрано {len(final_listings)}/{self.config.target_total}."
                )
                for cat, reserve_items in reserve_pool.items():
                    if guard_tripped or len(final_listings) >= self.config.target_total:
                        break
                    sorted_items = sorted(reserve_items, key=lambda x: sort_key(x.preview_dt), reverse=True)
                    for cand in sorted_items:
                        if (
                            current_counts.get(cat, 0) >= self.config.min_per_category
                            or len(final_listings) >= self.config.target_total
                        ):
                            break
                        if cand.id in attempted_ids:
                            continue
                        attempted_ids.add(cand.id)
                        before = len(final_listings)
                        if self._process_reserve_candidate(cand, final_listings, reserve_guard):
                            guard_tripped = True
                            break
                        if len(final_listings) > before:
                            current_counts[cat] = current_counts.get(cat, 0) + 1

            # 3b. Добір залишку за свіжістю
            if not guard_tripped and len(final_listings) < self.config.target_total:
                logger.info(
                    f"Запуск резерву (фаза 2/2 — добір за свіжістю): "
                    f"зібрано {len(final_listings)}/{self.config.target_total}."
                )
                flat_reserves = [
                    c for items in reserve_pool.values() for c in items if c.id not in attempted_ids
                ]
                flat_reserves.sort(key=lambda x: sort_key(x.preview_dt), reverse=True)

                for cand in flat_reserves:
                    if len(final_listings) >= self.config.target_total:
                        break
                    if cand.id in attempted_ids:
                        continue
                    attempted_ids.add(cand.id)
                    if self._process_reserve_candidate(cand, final_listings, reserve_guard):
                        break

        # 4. Фінальне сортування за точною датою оголошення
        final_listings.sort(key=lambda x: sort_key(x.published_at), reverse=True)
        final_listings = final_listings[:self.config.target_total]

        # 5. Валідація
        report = validate_invariants(
            listings=final_listings,
            mode=self.mode,
            target_total=self.config.target_total,
            min_categories=self.config.min_categories_required,
            fail_fast=self.fail_fast,
            metrics=self.metrics,
            base_url=self.config.base_url,
            run_id=self.run_id,
            debug=self.raw_storage.enabled,
            min_per_category=self.config.min_per_category,
            category_pool_size=category_pool_size,
            raw_dir=self.raw_storage.run_raw_dir,
            data_dir=self.data_dir,
            report_file=self.report_file,
            log_file=self.log_file,
        )

        return final_listings, report
