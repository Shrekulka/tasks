# Налаштування

Усе задається через `.env` (шаблон: `.env.example`), для кожної змінної є значення за замовчуванням.
Константи, яких тут немає (розмір сторінки каталогу, ліміт CSV, `size` блоку «Останні AI-сайти», ліміт запитів вердикту), поки живуть у коді.

| Змінна | За замовчуванням | Призначення |
|---|---|---|
| `DEBUG` | `False` | режим налагодження |
| `SECRET_KEY` | — | обов'язкова при `DEBUG=False`; `change-me` і `dev-insecure-key` відхиляються |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | дозволені хости (для Gunicorn потрібен `127.0.0.1`) |
| `CSRF_TRUSTED_ORIGINS` | порожньо | довірені origin для HTTPS на хостингу |
| `LOG_LEVEL` | `INFO` (`DEBUG` при `DEBUG=True`) | рівень логів |
| `FREESERP_API_URL` | `https://freeserp.ai/api.php` | адреса API |
| `FREESERP_AGENT` | `AIRadar/1.0` | ідентифікатор клієнта (docs просять представлятися) |
| `FREESERP_TIMEOUT` | `8` | timeout запиту, секунди |
| `FREESERP_TTL_SEARCH` | `60` | кеш пошуку, секунди |
| `FREESERP_TTL_LOOKUP` | `300` | кеш сторінки сайту, секунди |
| `FREESERP_TTL_STATS` | `900` | кеш статистики й агрегатів дашборда, секунди |
| `FREESERP_MAX_PARALLEL` | `3` | скільки запитів до FreeSerp одночасно |
| `MAX_COMPARE` | `3` | скільки сайтів можна порівнювати |
| `NICHES_ON_HOME` | `8` | скільки ніш на графіку головної |
| `NICHES_HIDDEN_ON_HOME` | `Other AI` | ніші, які не показувати на графіку (у фільтрі каталогу лишаються) |
| `SIMILAR_SITES` | `4` | скільки схожих сайтів |
| `PERIOD_PRESETS` | `7,30,90` | пресети періоду, дні |
| `SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS` | вимкнено | вмикаються на хостингу з HTTPS |
| `OPENROUTER_API_KEYS` | порожньо | ключі для AI-вердикту: `key1,key2` або `["key1","key2"]` |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenAI-сумісний endpoint |
| `OPENROUTER_MODELS` | порожньо | моделі, які пробуються першими |
| `OPENROUTER_MODELS_BLOCKLIST` | порожньо | моделі, які треба виключити |
| `OPENROUTER_TIMEOUT_SECONDS` | `20` | timeout одного запиту до LLM |
| `OPENROUTER_DEADLINE_SECONDS` | `45` | загальний ліміт часу на всі спроби (менше за `timeout` у `gunicorn.conf.py`) |

## Static files і WhiteNoise

При `DEBUG=0` `/static/` віддає WhiteNoise (у `MIDDLEWARE` одразу після `SecurityMiddleware`).

```bash
DEBUG=0 SECRET_KEY=<довгий-рядок> ALLOWED_HOSTS=localhost,127.0.0.1 python manage.py collectstatic --noinput
DEBUG=0 SECRET_KEY=<довгий-рядок> ALLOWED_HOSTS=localhost,127.0.0.1 gunicorn config.wsgi:application &
curl -I http://127.0.0.1:8000/static/radar/app.css     # очікується 200
```

## Розгортання (Render)

| Поле | Значення |
|---|---|
| Build Command | `pip install -r requirements.txt && python manage.py collectstatic --noinput` |
| Start Command | `gunicorn config.wsgi:application` |
| `DEBUG` | `0` |
| `SECRET_KEY` | довгий випадковий рядок |
| `ALLOWED_HOSTS` | домен сервісу, напр. `airadar.onrender.com` |
| `CSRF_TRUSTED_ORIGINS` | `https://airadar.onrender.com` |
