# airadar/radar/forms.py

from dataclasses import dataclass
from urllib.parse import urlencode

from django import forms
from django.conf import settings

from radar.services.freeserp import days_ago

# docs: sort=went_live | dr (серед полів, що сортуються). "auto" = relevance при пошуку (дефолт API), інакше нові
SORT_CHOICES = [("auto", "Авто (релевантність при пошуку)"), ("went_live", "Найновіші"), ("dr", "За DR")]
SORT_VALUES = {v for v, _ in SORT_CHOICES}
PAGE_SIZE = 20


def period_choices() -> list[tuple[str, str]]:
    """«За весь час» + пресети з settings.PERIOD_PRESETS (додати пресет = змінити налаштування, не код)."""
    presets = [p for p in settings.PERIOD_PRESETS if p.isdigit()]
    return [("all", "За весь час")] + [(p, f"{p} днів") for p in presets]


class CatalogForm(forms.Form):
    q = forms.CharField(required=False, max_length=100, strip=True)
    niche = forms.RegexField(required=False, max_length=50, regex=r"^[\w &/+.-]*$", strip=True)
    ai_source = forms.RegexField(required=False, max_length=60, regex=r"^[\w &/+.:-]*$", strip=True)
    tld = forms.RegexField(required=False, max_length=24, regex=r"^[a-z0-9-]*$", strip=True)
    dr_min = forms.IntegerField(required=False, min_value=0, max_value=100)
    dr_max = forms.IntegerField(required=False, min_value=0, max_value=100)
    period = forms.CharField(required=False, max_length=3)
    sort = forms.ChoiceField(required=False, choices=SORT_CHOICES)
    page = forms.IntegerField(required=False, min_value=1, max_value=10_000)


@dataclass(frozen=True)
class CatalogFilters:
    q: str = ""
    niche: str = ""
    ai_source: str = ""
    tld: str = ""
    dr_min: int | None = None
    dr_max: int | None = None
    period: str = "all"
    sort: str = "auto"
    page: int = 1

    # Поля, що потрапляють в URL і в чіпи активних фільтрів (page сюди не входить)
    URL_FIELDS = ("q", "niche", "ai_source", "tld", "dr_min", "dr_max", "period", "sort")
    LABELS = {"q": "Пошук", "niche": "Ніша", "ai_source": "Білдер", "tld": "TLD",
              "dr_min": "DR від", "dr_max": "DR до", "period": "Період", "sort": "Сортування"}

    def _defaults(self) -> "CatalogFilters":
        return CatalogFilters()

    def search_kwargs(self, *, size: int = PAGE_SIZE, page: int | None = None) -> dict:
        from_date = days_ago(int(self.period)) if self.period.isdigit() else ""
        if self.sort == "auto":
            sort = "" if self.q else "went_live"     # з q віддаємо порядок релевантності самому API
        else:
            sort = self.sort
        return {
            "q": self.q, "niche": self.niche, "ai_source": self.ai_source, "tld": self.tld,
            "dr_min": self.dr_min, "dr_max": self.dr_max, "from_date": from_date, "sort": sort,
            "page": page or self.page, "size": size,
        }

    def querystring(self, *, page: int | None = None, drop: str | None = None) -> str:
        """Параметри для URL (лише не-значення-за-замовчуванням). drop=<поле> прибирає один фільтр (для чіпа «×»)."""
        defaults = self._defaults()
        data: dict = {}
        for name in self.URL_FIELDS:
            value = getattr(self, name)
            if name != drop and value != getattr(defaults, name) and value not in ("", None):
                data[name] = value
        p = page or self.page
        if p > 1 and drop is None:
            data["page"] = p
        return urlencode(data)

    def chips(self) -> list[dict]:
        """Активні фільтри для відображення: [{label, value, remove_qs}]."""
        defaults = self._defaults()
        out = []
        for name in self.URL_FIELDS:
            value = getattr(self, name)
            if name == "sort" or value in ("", None) or value == getattr(defaults, name):
                continue
            shown = f"{value} днів" if name == "period" else value
            out.append({"label": self.LABELS[name], "value": shown, "remove_qs": self.querystring(drop=name)})
        return out


def parse_filters(querydict) -> CatalogFilters:
    """Невалідні поля мовчки замінюються значеннями за замовчуванням: URL не повинен давати 500."""
    form = CatalogForm(querydict)
    form.is_valid()
    cd = form.cleaned_data          # містить лише валідні поля
    allowed_periods = {v for v, _ in period_choices()}
    period = cd.get("period") or "all"
    return CatalogFilters(
        q=cd.get("q") or "",
        niche=cd.get("niche") or "",
        ai_source=cd.get("ai_source") or "",
        tld=cd.get("tld") or "",
        dr_min=cd.get("dr_min"),
        dr_max=cd.get("dr_max"),
        period=period if period in allowed_periods else "all",
        sort=cd.get("sort") or "auto",
        page=cd.get("page") or 1,
    )
