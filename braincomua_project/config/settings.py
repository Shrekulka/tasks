# braincomua_project/config/settings.py

import os
from pathlib import Path

import environ

# Initialize environ
env = environ.Env()

# Path to the project root
BASE_DIR = Path(__file__).resolve().parent.parent

# Read .env (the file must be located at the project root)
environ.Env.read_env(os.path.join(BASE_DIR, '.env'))

PROJECT_ROOT = BASE_DIR.parent

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/

# ⚠️ SECURITY: store SECRET_KEY in .env, not in the source code
SECRET_KEY = env('SECRET_KEY')

# DEBUG is read as a boolean value
DEBUG = env.bool('DEBUG', default=True)

# ALLOWED_HOSTS — comma-separated list
ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=['localhost', '127.0.0.1'])

# Trusted origins for CSRF, if needed (e.g. when running behind a proxy)
CSRF_TRUSTED_ORIGINS = env.list('CSRF_TRUSTED_ORIGINS', default=[])

# Application definition

INSTALLED_APPS = [
    # 1. Admin theme and interface (MUST come before django.contrib.admin!)
    'admin_interface',
    'colorfield',

    # 2. Standard Django applications
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # 3. Additional admin panel utilities
    'import_export',  # Import/export buttons for CSV/Excel
    'cache_cleaner',  # Cache-clear button in the admin panel

    # 4. System and custom applications
    'django.contrib.postgres',
    'debug_toolbar',
    'parser_app.apps.ParserAppConfig',
]

# ↓ Settings required for django-admin-interface to work correctly
X_FRAME_OPTIONS = "SAMEORIGIN"  # Allows related popups to open as modal windows
SILENCED_SYSTEM_CHECKS = ["security.W019"]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',

    'debug_toolbar.middleware.DebugToolbarMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

# Database connection
DATABASES = {
    'default': {
        'ENGINE': f'django.db.backends.{env("DB_ENGINE", default="postgresql")}',
        'NAME': env('POSTGRES_DB'),
        'USER': env('POSTGRES_USER'),
        'PASSWORD': env('POSTGRES_PASSWORD'),
        'HOST': env('DB_HOST'),
        'PORT': env('DB_PORT'),
    }
}

# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

INTERNAL_IPS = [
    # ...
    "127.0.0.1",
    # ...
]