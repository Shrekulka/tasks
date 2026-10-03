# airadar/radar/schemas.py

from dataclasses import dataclass


@dataclass(frozen=True)
class Site:
    domain: str
    url: str | None            # None, якщо безпечного посилання немає
    title: str
    summary: str
    niches: tuple[str, ...]
    builder: str | None        # ai_source як є (в API немає поля «technologies»)
    dr: int | None
    first_live: str | None     # went_live: «Вперше live», а не дата запуску
    http_status: int | None
    tld: str | None = None
    first_seen: str | None = None


@dataclass(frozen=True)
class SearchResult:
    total: int
    sites: tuple[Site, ...]
    page: int
    size: int
    max_window: int
    applied_filters: dict | None = None   # що API реально застосував (для налагодження)

    @property
    def has_more(self) -> bool:
        return self.page * self.size < min(self.total, self.max_window)


@dataclass(frozen=True)
class Stats:
    """Агрегати stats=1. УВАГА: by_day, dr_histogram, top_sources, top_tlds рахуються по ВСЬОМУ індексу
    (усі ніші), а не лише по AI. AI-зріз дає AiOverview."""
    ai_total: int | None = None
    ai_today: int | None = None
    real_sites: int | None = None
    new_today: int | None = None
    top_niches: tuple[tuple[str, int, int], ...] = ()      # лише для картки на головній (обрізано)
    by_day: tuple[tuple[str, int, int], ...] = ()
    dr_histogram: tuple[tuple[int, int], ...] = ()
    top_sources: tuple[tuple[str, int], ...] = ()
    top_tlds: tuple[tuple[str, int], ...] = ()
    niche_options: tuple[str, ...] = ()                    # ВСІ ніші з API (для фільтра)
    source_options: tuple[str, ...] = ()                   # підказки для фільтра «Білдер»
    tld_options: tuple[str, ...] = ()                      # підказки для фільтра TLD
    generated_at: str | None = None


@dataclass(frozen=True)
class AiOverview:
    """Показники саме по AI-сайтах (ai_startups=1), рахуються через search total."""
    last_7d: int | None = None
    last_30d: int | None = None
    latest_live: str | None = None                         # went_live найсвіжішого AI-сайту
    dr_histogram: tuple[tuple[int, int], ...] = ()         # (початок бакета DR, кількість)
