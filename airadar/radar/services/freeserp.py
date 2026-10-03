# airadar/radar/services/freeserp.py

"""Єдине місце, яке знає формат FreeSerp (freeserp.ai/docs.php).

Усі параметри запиту й поля відповіді, що тут використовуються, підтверджені документацією.
Налаштування (URL, timeout, TTL, agent) беруться з django.conf.settings (FREESERP_*).
"""
import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

import re

import requests
from django.conf import settings
from django.core.cache import cache

from radar.schemas import AiOverview, SearchResult, Site, Stats

log = logging.getLogger(__name__)

MAX_SIZE = 100          # docs: size 1-100
MAX_WINDOW = 10_000     # docs: from + size <= 10 000

# transport metadata, на результат не впливає, тому в ключ кешу не входить
_IGNORED_IN_KEY = {"agent"}

DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")

# подписи для показа; в запросах к API и в фильтрах остаётся исходное значение ai_source
SOURCE_LABELS = {"ai_likely": "AI (ймовірно)", "not_ai": "не AI"}


class FreeSerpError(Exception):
    """Джерело даних недоступне або повернуло помилку."""


class _Retryable(Exception):
    pass


# ---------- нормалізація ----------

def clean_domain(value) -> str | None:
    """Повертає домен у нижньому регістрі (IDN -> punycode) або None, якщо він не підходить під маршрут /site/<домен>/."""
    if not isinstance(value, str):
        return None
    value = value.strip().lower().rstrip(".")
    if not value:
        return None
    if not value.isascii():
        try:
            value = value.encode("idna").decode("ascii")      # мир.рф -> xn--h1ahn.xn--p1ai
        except UnicodeError:
            return None
    return value if DOMAIN_RE.fullmatch(value) else None


def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    if isinstance(value, dict):  # формат виду {"value": 123}
        return _as_int(value.get("value"))
    return None


def normalize_url(raw_url, domain: str) -> str | None:
    """Повертає лише безпечне http(s)-посилання без userinfo, інакше None."""
    candidates = [raw_url, f"https://{domain}" if clean_domain(domain) else None]
    for cand in candidates:
        if not isinstance(cand, str):
            continue
        cand = cand.strip()
        try:
            parsed = urlparse(cand)
            ok = (
                parsed.scheme in ("http", "https")
                and bool(parsed.hostname)
                # https://example.com@evil.com веде на evil.com, тому userinfo відкидаємо
                and parsed.username is None
                and parsed.password is None
            )
        except ValueError:
            continue
        if ok:
            return cand
    return None


def normalize_site(raw: dict) -> Site:
    """Нормалізує запис API. Якщо домен непридатний для URL, domain == "" (виклик має відкинути запис)."""
    niches = raw.get("ai_categories") or []
    if isinstance(niches, str):
        niches = [niches]
    if not isinstance(niches, (list, tuple)):
        niches = []
    domain = clean_domain(raw.get("domain")) or ""
    builder = raw.get("ai_source")
    went_live = raw.get("went_live")
    return Site(
        domain=domain,
        url=normalize_url(raw.get("url"), domain),
        title=str(raw.get("title") or domain).strip(),
        summary=str(raw.get("ai_summary") or "").strip(),
        niches=tuple(str(n).strip() for n in niches if str(n).strip()),
        builder=str(builder) if builder else None,
        dr=_as_int(raw.get("dr")),
        first_live=str(went_live) if went_live else None,
        http_status=_as_int(raw.get("http_status")),
        tld=str(raw["tld"]) if raw.get("tld") else None,
        first_seen=str(raw["first_seen"]) if raw.get("first_seen") else None,
    )


# ---------- HTTP + кеш ----------

def make_cache_key(params: dict) -> str:
    clean = {
        k: str(v).strip()  # регістр не чіпаємо: поведінка API не доведена
        for k, v in params.items()
        if v not in (None, "") and k not in _IGNORED_IN_KEY
    }
    raw = urlencode(sorted(clean.items()))
    return "freeserp:v1:" + hashlib.sha1(raw.encode()).hexdigest()


def _request(params: dict, ttl: int) -> dict:
    key = make_cache_key(params)
    hit = cache.get(key)
    if hit is not None:
        return hit

    agent = settings.FREESERP_AGENT
    full_params = {**params, "agent": agent}
    for attempt in (1, 2):
        try:
            resp = requests.get(settings.FREESERP_API_URL, params=full_params,
                                timeout=settings.FREESERP_TIMEOUT, headers={"X-Agent": agent})
            if resp.status_code >= 500:
                raise _Retryable(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            data = resp.json()
        except (requests.Timeout, requests.ConnectionError, _Retryable) as exc:
            if attempt == 1:
                log.warning("FreeSerp retry after: %s", exc)
                time.sleep(0.7)
                continue
            raise FreeSerpError(str(exc)) from exc
        except (requests.RequestException, ValueError) as exc:
            raise FreeSerpError(str(exc)) from exc

        if not isinstance(data, dict) or data.get("ok") is False:
            detail = data.get("detail", "api error") if isinstance(data, dict) else "bad payload"
            raise FreeSerpError(str(detail))
        log.debug("FreeSerp %s -> applied filters: %s", params.get("index") or "stats", data.get("filters"))
        cache.set(key, data, ttl)
        return data
    raise FreeSerpError("unreachable")


# ---------- публічні функції ----------

def utc_today() -> date:
    """API рахує дні в UTC, тому й from_date будуємо від UTC, а не від TIME_ZONE сервера."""
    return datetime.now(timezone.utc).date()


def days_ago(n: int) -> str:
    return (utc_today() - timedelta(days=n)).isoformat()


def search_sites(*, q: str = "", niche: str = "", dr_min: int | None = None, dr_max: int | None = None,
                 ai_source: str = "", tld: str = "", from_date: str = "",
                 sort: str = "went_live", order: str = "desc",
                 page: int = 1, size: int = 20, ttl: int | None = None) -> SearchResult:
    size = min(max(int(size), 1), MAX_SIZE)
    max_page = max(MAX_WINDOW // size, 1)
    page = min(max(int(page), 1), max_page)
    params = {
        "index": "sites", "ai_startups": 1,
        "size": size, "from": (page - 1) * size,
    }
    if sort:                       # порожній sort = дефолт API (relevance для q, інакше за його правилами)
        params["sort"] = sort
        params["order"] = order
    optional = {
        "q": q, "ai_categories": niche, "dr_min": dr_min, "dr_max": dr_max,
        "ai_source": ai_source, "tld": tld, "from_date": from_date,
    }
    params.update({k: v for k, v in optional.items() if v not in (None, "")})

    data = _request(params, ttl if ttl is not None else settings.FREESERP_TTL_SEARCH)

    raw_results = data.get("results") or []
    # записи з непридатним доменом відкидаємо тут: інакше {% url 'radar:site' %} валить усю сторінку
    sites = tuple(s for s in (normalize_site(x) for x in raw_results if isinstance(x, dict)) if s.domain)
    return SearchResult(
        total=_as_int(data.get("total")) or 0,
        sites=sites, page=page, size=size, max_window=MAX_WINDOW,
        applied_filters=data.get("filters") if isinstance(data.get("filters"), dict) else None,
    )


def count_sites(**filters) -> int:
    """Скільки AI-сайтів відповідає фільтрам (читаємо лише total, size=1).

    Це агрегати для дашборда, а не живий список: кешуємо їх як статистику (FREESERP_TTL_STATS),
    щоб холодна головна не робила пачку з ~12 запитів щохвилини (docs: «a few requests/second»).
    """
    return search_sites(size=1, ttl=settings.FREESERP_TTL_STATS, **filters).total


def lookup_domain(domain: str) -> Site | None:
    domain = clean_domain(domain)
    if not domain:
        return None
    data = _request({"index": "sites", "q": domain, "all": 1, "size": 5}, settings.FREESERP_TTL_LOOKUP)
    for item in data.get("results") or []:
        if isinstance(item, dict) and str(item.get("domain") or "").strip().lower() == domain:
            site = normalize_site(item)
            return site if site.domain else None
    return None


def _pairs(obj) -> list[tuple[str, int]]:
    """Агрегати FreeSerp -> [(ключ, число)]. Формат з docs: список {"key": ..., "count": ...}; також приймаємо dict {ключ: число}."""
    out: list[tuple[str, int]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            n = _as_int(v)
            if n is not None:
                out.append((str(k), n))
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict) and "key" in item:
                n = _as_int(item.get("count"))
                if n is not None:
                    out.append((str(item["key"]), n))
    return out


def _format_stat_day(value: str) -> str:
    """2026-09-03T00:00:00.000Z -> 03.09"""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d.%m")
    except (TypeError, ValueError):
        return value[:10]


def parse_stats(data: dict) -> Stats:
    """Перетворює відповідь stats=1 на дані для відображення."""
    root = data.get("stats") if isinstance(data.get("stats"), dict) else data

    ai = root.get("ai_startups")
    ai_total = _as_int(ai.get("total")) if isinstance(ai, dict) else _as_int(ai)
    ai_today = _as_int(ai.get("today")) if isinstance(ai, dict) else None

    totals = root.get("totals")
    new = root.get("new")
    real_sites = _as_int(totals.get("real_sites")) if isinstance(totals, dict) else None
    new_today = _as_int(new.get("today")) if isinstance(new, dict) else None

    # ніші: повний список для фільтра, обрізаний для картки
    all_niches = _pairs(root.get("top_ai_categories"))
    hidden = set(getattr(settings, "NICHES_HIDDEN_ON_HOME", ()))      # напр. «Other AI»: не інформативна ніша
    visible = [pair for pair in all_niches if pair[0] not in hidden]
    shown = visible[:max(int(getattr(settings, "NICHES_ON_HOME", 8)), 1)]
    niche_peak = max((c for _, c in shown), default=0) or 1
    niches = tuple((name, count, round(count * 100 / niche_peak)) for name, count in shown)

    # по днях: від старих до нових, порядок не залежить від порядку API
    days = sorted(_pairs(root.get("by_day")), key=lambda x: x[0])[-30:]
    day_peak = max((c for _, c in days), default=0) or 1
    by_day = tuple((_format_stat_day(d), c, round(c * 100 / day_peak)) for d, c in days)

    dr = root.get("dr")
    dr_histogram: tuple[tuple[int, int], ...] = ()
    if isinstance(dr, dict):
        hist = []
        for key, count in _pairs(dr.get("histogram")):
            start = _as_int(key)
            if start is not None:
                hist.append((start, count))
        dr_histogram = tuple(sorted(hist))

    sources = _pairs(root.get("top_ai_source"))
    tlds = _pairs(root.get("top_tld"))
    generated_at = data.get("generated_at")
    if isinstance(generated_at, str):
        generated_at = generated_at.replace("T", " ")[:16]       # 2026-10-02T16:30:11+00:00 -> 2026-10-02 16:30

    return Stats(
        ai_total=ai_total, ai_today=ai_today, real_sites=real_sites, new_today=new_today,
        top_niches=niches, by_day=by_day, dr_histogram=dr_histogram,
        top_sources=tuple(sources[:8]), top_tlds=tuple(tlds[:8]),
        niche_options=tuple(n for n, _ in all_niches),
        source_options=tuple(n for n, _ in sources),
        tld_options=tuple(n for n, _ in tlds),
        generated_at=generated_at if isinstance(generated_at, str) else None,
    )


def get_stats() -> Stats:
    return parse_stats(_request({"stats": 1}, settings.FREESERP_TTL_STATS))


def get_ai_overview(bucket_starts: tuple[int, ...], latest_live: str | None = None) -> AiOverview:
    """AI-зріз, якого немає в stats=1: нові за 7/30 днів, найсвіжіший went_live, гістограма DR по AI.

    Межі бакетів DR беруться з гістограми самого API (bucket_starts), а не з коду.
    latest_live: якщо виклик уже знає найсвіжіший went_live (головна), зайвий запит не робимо.
    Кожен виклик кешується в _request, тому це не навантажує API при повторних відвідинах.
    """
    starts = sorted(set(bucket_starts))
    ranges = []
    for i, start in enumerate(starts):
        end = starts[i + 1] - 1 if i + 1 < len(starts) else 100
        ranges.append((start, end))

    with ThreadPoolExecutor(max_workers=settings.FREESERP_MAX_PARALLEL) as pool:
        f7 = pool.submit(count_sites, from_date=days_ago(7))
        f30 = pool.submit(count_sites, from_date=days_ago(30))
        flast = None if latest_live else pool.submit(search_sites, size=1, sort="went_live")
        fdr = [pool.submit(count_sites, dr_min=lo, dr_max=hi) for lo, hi in ranges]
        if flast is not None:
            latest = flast.result().sites
            latest_live = latest[0].first_live if latest else None
        return AiOverview(
            last_7d=f7.result(), last_30d=f30.result(),
            latest_live=latest_live,
            dr_histogram=tuple((lo, f.result()) for (lo, _), f in zip(ranges, fdr)),
        )
