# airadar/radar/views.py

import csv
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from django.conf import settings
from django.http import Http404, HttpResponse
from django.shortcuts import render

from radar.forms import SORT_CHOICES, parse_filters, period_choices
from radar.services.freeserp import (
    FreeSerpError, SOURCE_LABELS, clean_domain, get_ai_overview, get_stats, lookup_domain, search_sites, utc_today,
)
from radar.services.llm import llm_enabled

log = logging.getLogger(__name__)

EXPORT_LIMIT = 100

# Рядки таблиці порівняння: (поле Site, підпис). Новий рядок = один елемент списку.
COMPARE_FIELDS = [
    ("niches", "Ніші"),
    ("builder", "Білдер / стек"),
    ("dr", "DR"),
    ("first_live", "Вперше live"),
    ("http_status", "HTTP"),
    ("tld", "TLD"),
    ("summary", "Опис"),
]


def _is_ajax(request) -> bool:
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _upstream_error(request):
    """Django працює, недоступна залежність, тому 503, а не 502."""
    tpl = "radar/_error.html" if _is_ajax(request) else "radar/error.html"
    return render(request, tpl, status=503)


def _safe_stats():
    """Stats для довідкових даних (списки фільтрів). Збій API не повинен ламати сторінку."""
    try:
        return get_stats()
    except FreeSerpError:
        log.warning("stats unavailable for filter options")
        return None


def _lag_days(latest_live: str | None) -> int | None:
    """Скільки днів минуло від найсвіжішого AI-сайту в індексі (для чесного відображення затримки даних)."""
    if not latest_live:
        return None
    try:
        return max((utc_today() - date.fromisoformat(latest_live[:10])).days, 0)
    except ValueError:
        return None


def index(request):
    latest, latest_error, stats, stats_error, overview = None, False, None, False, None
    try:
        latest = search_sites(page=1, size=6, sort="went_live")
    except FreeSerpError:
        log.exception("index: search failed")
        latest_error = True
    try:
        stats = get_stats()
    except FreeSerpError:
        log.exception("index: stats failed")
        stats_error = True
    if stats is not None and stats.dr_histogram:
        try:
            newest = latest.sites[0].first_live if latest and latest.sites else None
            overview = get_ai_overview(tuple(start for start, _ in stats.dr_histogram), latest_live=newest)
        except FreeSerpError:
            log.exception("index: ai overview failed")      # графік по AI просто не показуємо
    return render(request, "radar/index.html", {
        "latest": latest,
        "latest_error": latest_error,
        "stats": stats,
        "stats_error": stats_error,
        "overview": overview,
        "lag_days": _lag_days(overview.latest_live if overview else None),
        "max_compare": settings.MAX_COMPARE,
    })


def catalog(request):
    filters = parse_filters(request.GET)
    try:
        result = search_sites(**filters.search_kwargs())
    except FreeSerpError:
        log.exception("catalog: search failed")
        return _upstream_error(request)

    ctx = {
        "filters": filters, "result": result,
        "chips": filters.chips(),
        "next_qs": filters.querystring(page=result.page + 1) if result.has_more else "",
        "export_qs": filters.querystring(page=1),
    }
    if _is_ajax(request):
        tpl = "radar/_chunk.html" if request.GET.get("append") == "1" else "radar/_results.html"
        return render(request, tpl, ctx)

    stats = _safe_stats()
    ctx.update(
        niches=stats.niche_options if stats else (),
        sources=stats.source_options if stats else (),
        tlds=stats.tld_options if stats else (),
        sort_choices=SORT_CHOICES, period_choices=period_choices(),
        max_compare=settings.MAX_COMPARE,
    )
    return render(request, "radar/catalog.html", ctx)


def _similar_sites(site):
    """Схожі сайти: та сама перша ніша, найвищий DR. Збій API не ламає сторінку сайту."""
    if not site.niches:
        return ()
    limit = settings.SIMILAR_SITES
    try:
        found = search_sites(niche=site.niches[0], sort="dr", size=limit + 1)
    except FreeSerpError:
        return ()
    return tuple(s for s in found.sites if s.domain != site.domain)[:limit]


def site_detail(request, domain):
    domain = clean_domain(domain)
    if not domain:
        raise Http404
    try:
        site = lookup_domain(domain)
    except FreeSerpError:
        log.exception("site_detail failed")
        return _upstream_error(request)
    if site is None:
        raise Http404
    return render(request, "radar/site_detail.html", {
        "site": site, "similar": _similar_sites(site), "max_compare": settings.MAX_COMPARE,
    })


def _compare_rows(sites) -> list[dict]:
    rows = []
    for field, label in COMPARE_FIELDS:
        values = [getattr(s, field) for s in sites]
        if field == "builder":
            values = [SOURCE_LABELS.get(v, v) for v in values]
        cells = [{"value": ", ".join(v) if isinstance(v, tuple) else v} for v in values]
        if field == "dr":                       # найкращий DR підсвічуємо, лише якщо значення різні
            nums = [v for v in values if v is not None]
            if len(set(nums)) > 1:
                best = max(nums)
                for cell, v in zip(cells, values):
                    cell["best"] = v == best
        rows.append({"label": label, "field": field, "cells": cells})
    return rows


def compare(request):
    domains: list[str] = []
    for raw in request.GET.getlist("d"):
        d = clean_domain(raw)
        if d and d not in domains:
            domains.append(d)
    domains = domains[:settings.MAX_COMPARE]

    sites, missing = [], []
    try:
        with ThreadPoolExecutor(max_workers=max(len(domains), 1)) as pool:
            found = list(pool.map(lookup_domain, domains))     # запити паралельно, порядок збережено
    except FreeSerpError:
        log.exception("compare failed")
        return _upstream_error(request)
    for d, site in zip(domains, found):
        if site:
            sites.append(site)
        else:
            missing.append(d)

    return render(request, "radar/compare.html", {
        "sites": sites, "missing": missing, "domains": [s.domain for s in sites],
        "rows": _compare_rows(sites),
        "llm_enabled": llm_enabled() and len(sites) >= 2,
    })


def _csv_safe(value):
    """Захист від CSV-ін'єкцій: комірки, що починаються з = + - @, Excel вважає формулами."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def export_csv(request):
    filters = parse_filters(request.GET)       # ті самі фільтри, що й у каталозі
    try:
        result = search_sites(**filters.search_kwargs(size=EXPORT_LIMIT, page=1))
    except FreeSerpError:
        log.exception("export failed")
        return HttpResponse("Джерело даних тимчасово недоступне", status=503, content_type="text/plain; charset=utf-8")

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="ai-radar.csv"'
    response.write("\ufeff")                   # BOM, щоб Excel правильно прочитав кирилицю
    writer = csv.writer(response)
    writer.writerow(["domain", "url", "title", "niches", "builder", "dr", "first_live", "http_status", "tld", "summary"])
    for s in result.sites:
        row = [s.domain, s.url or "", s.title, "; ".join(s.niches), s.builder or "",
               "" if s.dr is None else s.dr, s.first_live or "",
               "" if s.http_status is None else s.http_status, s.tld or "", s.summary]
        writer.writerow([_csv_safe(v) for v in row])
    return response


def about(request):
    return render(request, "radar/about.html", {
        "ttl_search": settings.FREESERP_TTL_SEARCH, "ttl_stats": settings.FREESERP_TTL_STATS,
    })


def verdict(request):                           # P2
    from radar.verdict_view import verdict_view
    return verdict_view(request)


def page_not_found(request, exception=None):
    return render(request, "404.html", status=404)


def server_error(request):
    return render(request, "500.html", status=500)
