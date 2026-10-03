# airadar/radar/management/commands/list_free_models.py

from django.core.management.base import BaseCommand

from radar.services.llm import fetch_free_models


class Command(BaseCommand):
    help = "Показати безкоштовні текстові моделі OpenRouter (щоб вибрати, що закріпити в OPENROUTER_MODELS)"

    def handle(self, *args, **options):
        models = fetch_free_models(force=True)
        if not models:
            self.stdout.write(self.style.ERROR("Список порожній: немає мережі або OpenRouter не відповів."))
            return
        self.stdout.write(f"Знайдено безкоштовних текстових моделей: {len(models)}\n")
        self.stdout.write(f"{'CONTEXT':>9}  {'INPUT':<18}  ID")
        for m in models:
            self.stdout.write(f"{m['context']:>9}  {','.join(m['inputs']):<18}  {m['id']}")
        self.stdout.write("\nЗакріпіть потрібні в .env: OPENROUTER_MODELS=id1,id2")