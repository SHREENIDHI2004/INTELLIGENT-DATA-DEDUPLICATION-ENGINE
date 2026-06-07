from django.apps import AppConfig


class StewardConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'steward'
    verbose_name = 'Data Steward & Deduplication'
