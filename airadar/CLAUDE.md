# AI Radar: project rules for the assistant

## Constraints
- Django, no database (`DATABASES = {}`), no models. FreeSerp (`index=sites`) is the primary data source.
- Only `radar/services/freeserp.py` knows the API format. Views and templates use dataclasses from `schemas.py`.
- No invented API fields: check freeserp.ai/docs.php, fixtures and `?help=1` first. Aggregates (`by_day`, `dr`, `top_tld`, `top_ai_source`) in `stats=1` cover the WHOLE index, not only AI: never label them as AI.
- Anything that can change (filter options, niches, DR buckets, sort presets, limits) comes from the API or `settings`, not from literals in code.
- All external data is untrusted: templates autoescape, never use `|safe`, links only `http(s)` without userinfo.
- No secrets in code; keys only from env. Never log API keys.
- Business logic stays out of templates.
- No new dependency without justification. LLM (P2) is optional, called via plain `requests` (no SDK), and must not break the app when disabled.
- `whitenoise` serves `/static/` under Gunicorn: `WhiteNoiseMiddleware` must stay right after `SecurityMiddleware` (guarded by `test_settings.py`).
- `gunicorn` is required for production-style WSGI startup (`gunicorn config.wsgi:application`).
- Test discovery must find `radar/tests/`; keep `radar/tests/__init__.py` present.
- Prefer the simplest solution; every feature needs a clear user value.
- UI wording: "AI-сайти", "Вперше live" (`went_live`), "Білдер / стек" (`ai_source`).
- Do not assert API behaviour that was not verified with curl or fixtures (stats shape, size limits, case sensitivity).

## OpenRouter P2 rules
- The public `/models` endpoint is used to discover free models dynamically; do not hardcode a permanent global model list in code.
- A model is eligible only when prompt/completion pricing is zero, id ends with `:free`, text is supported as input/output, and context is at least 8k.
- Cache the model list for one hour.
- `OPENROUTER_MODELS` is a manual priority list, not the only source of candidates.
- `OPENROUTER_MODELS_BLOCKLIST` excludes known bad models.
- Keep the `model × key` fallback bounded by `MAX_MODELS_TRIED`.
- `429`, timeout and `5xx` should allow fallback to another key/model. `400/404` for a model should quarantine that model temporarily; `401` is a key problem, so try the next key; `403` on every key means the model is unavailable (quarantine it).
- Never print or log the value of `OPENROUTER_API_KEYS`.
- `list_free_models` must use `fetch_free_models(force=True)` so the command shows a fresh list.

## Workflow
- Order: P0 -> P1 -> P2. Do not start P1 until the catalog works.
- After each step: run `python manage.py test`, then make a small commit.
- Review pass before commit: extra upstream calls, XSS, CSV injection, crashes on empty/null data, secrets in logs.
- Before delivery also run `python manage.py check`, `collectstatic` with `DEBUG=0`, and a Gunicorn startup check.
