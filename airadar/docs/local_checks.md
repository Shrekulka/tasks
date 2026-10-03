# Локальна перевірка

Цей документ описує не тільки команди, а й те, **що саме перевіряє кожна команда** і як інтерпретувати результат.

## 1. Окреме virtualenv

У корені проєкту:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Після активації в prompt має бути видно `(.venv)`.

Встановлення всіх залежностей:

```bash
pip install -r requirements.txt
```

Критичні для production-style запуску залежності:

```text
whitenoise
gunicorn
```

`whitenoise` потрібен через backend static files, заданий у `config/settings.py`:

```python
"staticfiles": {
    "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"
}
```

`gunicorn` потрібен для запуску Django через WSGI-сервер:

```bash
gunicorn config.wsgi:application
```

Швидка перевірка імпортів:

```bash
python -c "import whitenoise, gunicorn; print('OK')"
```

Очікуваний результат:

```text
OK
```

## 2. Django system check

```bash
python manage.py check
```

Команда перевіряє конфігурацію Django без запуску сервера.

Очікувано:

```text
System check identified no issues (0 silenced).
```

## 3. Автоматичні тести

```bash
python manage.py test
```

У каталозі `radar/tests/` зібрано 90 тестів.

Файл `radar/tests/__init__.py` є навмисно, щоб стандартний test discovery стабільно находив пакет тестів у Python 3.13.

Нормальний фінал:

```text
Found 90 test(s).
...
----------------------------------------------------------------------
Ran 90 tests ...

OK
```

Логи виду `FreeSerp retry after: HTTP 502` або тестові `FreeSerpError` в середовищі test — очікувані, якщо тест перевіряє обробку upstream-помилок. Критерій успіху — фінальний `OK`.

Раніше без `radar/tests/__init__.py` Django показував:

```text
Found 0 test(s).
NO TESTS RAN
```

Це було не відсутність тестів у коді, а проблема discovery.

## 4. Збірка static files

Production-style перевірка:

```bash
DEBUG=0 SECRET_KEY=test ALLOWED_HOSTS=localhost,127.0.0.1 python manage.py collectstatic --noinput
```

Перший запуск копіює та post-process'ить файли, наступні можуть показати:

```text
N static files copied to '.../staticfiles'.
```

Це нормальний результат: Django не копіює повторно незмінені файли.

Типова початкова помилка до встановлення WhiteNoise:

```text
ModuleNotFoundError: No module named 'whitenoise'
...
InvalidStorageError:
Could not find backend 'whitenoise.storage.CompressedStaticFilesStorage'
```

Виправлення: встановити WhiteNoise та залишити його в `requirements.txt`.

## 5. Запуск через Gunicorn

```bash
gunicorn config.wsgi:application
```

Або production-style з явними змінними:

```bash
DEBUG=0 SECRET_KEY=test ALLOWED_HOSTS=localhost,127.0.0.1 gunicorn config.wsgi:application
```

Успішний запуск виглядає приблизно так:

```text
Starting gunicorn 26.2.0
Listening at: http://127.0.0.1:8000
Using worker: sync
Booting worker with pid: ...
```

Після цього сайт можна відкрити в браузері за адресою `http://127.0.0.1:8000`.

**Обов'язкова smoke-перевірка статики** (саме вона ловить відсутній `WhiteNoiseMiddleware`; сам по собі запуск Gunicorn цього не показує):

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/static/radar/app.css       # очікується 200
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/static/radar/dashboard.js  # очікується 200
```

Якщо тут 404, у `MIDDLEWARE` немає `whitenoise.middleware.WhiteNoiseMiddleware` (він має стояти одразу після `SecurityMiddleware`). Автоматичний варіант цієї перевірки: `radar/tests/test_settings.py`.

`Ctrl+C` зупиняє Gunicorn. Повідомлення `Handling signal: int` та `Shutting down` після `Ctrl+C` — штатне завершення процесу, а не помилка.

Початкова помилка до встановлення Gunicorn:

```text
zsh: command not found: gunicorn
```

Виправлення: `pip install gunicorn` або повторне встановлення з `requirements.txt`.

## 6. Перевірка списку безкоштовних моделей OpenRouter

Команда:

```bash
python manage.py list_free_models
```

Призначення: побачити **актуальний** список безкоштовних текстових моделей перед ручним пріоритезуванням у `.env`.

Команда не використовує `OPENROUTER_API_KEYS` для самого списку моделей: `fetch_free_models()` звертається до публічного endpoint `/models`.

При успішному запиті виводяться:

```text
Знайдено безкоштовних текстових моделей: N
  CONTEXT  INPUT               ID
  ...
```

`ID` — значення, яке можна помістити в:

```env
OPENROUTER_MODELS=id1,id2
```

В поточній перевірці команда успішно повернула 17 безкоштовних текстових моделей. Точний список не фіксується в документації, тому що набір OpenRouter змінюється.

## 7. Фінальна послідовність перед здачею

Рекомендований мінімальний прогін:

```bash
python manage.py check
python manage.py test
DEBUG=0 SECRET_KEY=test ALLOWED_HOSTS=localhost,127.0.0.1 python manage.py collectstatic --noinput
gunicorn config.wsgi:application
```

Окремо для перевірки P2:

```bash
python manage.py list_free_models
```

Далі, за наявності ключа OpenRouter, перевіряється AI-вердикт у самому UI. Без ключів каталог, сторінка сайту, порівняння та CSV повинні залишатися працездатними.
