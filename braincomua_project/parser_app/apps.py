# braincomua_project/parser_app/apps.py

from django.apps import AppConfig


class ParserAppConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'parser_app'
    verbose_name = 'Parser Application'
