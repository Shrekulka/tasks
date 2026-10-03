# airadar/radar/tests/test_settings.py

from django.conf import settings
from django.test import SimpleTestCase


class SettingsTests(SimpleTestCase):
    def test_whitenoise_serves_static_under_gunicorn(self):
        """Без цього middleware при DEBUG=0 /static/ віддає 404 і сайт відкривається без CSS."""
        self.assertIn("whitenoise.middleware.WhiteNoiseMiddleware", settings.MIDDLEWARE)
        sec = settings.MIDDLEWARE.index("django.middleware.security.SecurityMiddleware")
        wn = settings.MIDDLEWARE.index("whitenoise.middleware.WhiteNoiseMiddleware")
        self.assertEqual(wn, sec + 1)

    def test_no_apps_that_need_a_database(self):
        for app in ("django.contrib.admin", "django.contrib.auth", "django.contrib.sessions"):
            self.assertNotIn(app, settings.INSTALLED_APPS)
        self.assertEqual(settings.DATABASES["default"]["ENGINE"], "django.db.backends.dummy")

    def test_admin_url_is_gone(self):
        self.assertEqual(self.client.get("/admin/login/").status_code, 404)

    def test_required_settings_defined(self):
        for name in ("FREESERP_API_URL", "FREESERP_TIMEOUT", "FREESERP_TTL_SEARCH",
                     "FREESERP_TTL_LOOKUP", "FREESERP_TTL_STATS", "MAX_COMPARE",
                     "NICHES_ON_HOME", "SIMILAR_SITES", "PERIOD_PRESETS"):
            self.assertTrue(hasattr(settings, name), name)


class SecretKeyTests(SimpleTestCase):
    def test_placeholder_secret_keys_are_rejected(self):
        from config.settings import INSECURE_SECRET_KEYS
        self.assertTrue({"change-me", "dev-insecure-key", ""} <= INSECURE_SECRET_KEYS)
