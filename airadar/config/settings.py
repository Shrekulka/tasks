# airadar/config/settings.py

import sys
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")  # заодно кладёт значения в os.environ

# --- Безопасность ---
DEBUG = env.bool("DEBUG", default=False)

SECRET_KEY = env("SECRET_KEY", default="dev-insecure-key")
INSECURE_SECRET_KEYS = {"", "dev-insecure-key", "change-me"}
if not DEBUG and SECRET_KEY in INSECURE_SECRET_KEYS:
    raise RuntimeError("Задайте надійний SECRET_KEY у середовищі при DEBUG=False")

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# Application definition

INSTALLED_APPS = [
    'django.contrib.staticfiles',
    'radar.apps.RadarConfig'
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates']
        ,
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# --- Данные: БД нет. Источник истины FreeSerp, локально только кэш ---
DATABASES = {}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "airadar",
    }
}

# --- FreeSerp API ---
FREESERP_API_URL = env("FREESERP_API_URL", default="https://freeserp.ai/api.php")
FREESERP_AGENT = env("FREESERP_AGENT", default="AIRadar/1.0")
FREESERP_TIMEOUT = env.int("FREESERP_TIMEOUT", default=8)
FREESERP_TTL_SEARCH = env.int("FREESERP_TTL_SEARCH", default=60)
FREESERP_TTL_LOOKUP = env.int("FREESERP_TTL_LOOKUP", default=300)
FREESERP_TTL_STATS = env.int("FREESERP_TTL_STATS", default=900)
# скільки запитів до FreeSerp одночасно (docs просять «a few requests/second»)
FREESERP_MAX_PARALLEL = env.int("FREESERP_MAX_PARALLEL", default=3)

# --- Ограничения UI ---
MAX_COMPARE = env.int("MAX_COMPARE", default=3)
NICHES_ON_HOME = env.int("NICHES_ON_HOME", default=8)
SIMILAR_SITES = env.int("SIMILAR_SITES", default=4)
PERIOD_PRESETS = env.list("PERIOD_PRESETS", default=["7", "30", "90"])
NICHES_HIDDEN_ON_HOME = env.list("NICHES_HIDDEN_ON_HOME", default=["Other AI"])
OPENROUTER_DEADLINE_SECONDS = env.int("OPENROUTER_DEADLINE_SECONDS", default=45)

# --- Локаль ---
LANGUAGE_CODE = "uk"
TIME_ZONE = "Europe/Kyiv"
USE_I18N = False
USE_TZ = True

# --- Статика (radar/static подхватывается через APP_DIRS-аналог staticfiles) ---
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

# --- Прод-настройки ---
if not DEBUG:
    SECURE_CONTENT_TYPE_NOSNIFF = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=False)
    SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0)

# --- Логи: просто и читаемо ---
LOG_LEVEL = env("LOG_LEVEL", default="DEBUG" if DEBUG else "INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "stream": sys.stderr, "formatter": "plain"},
    },
    "loggers": {
        "radar": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "urllib3": {"level": "WARNING"},
    },
}
