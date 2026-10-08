# python_style_extractor/scripts/paths.py

"""Единый источник пути проекта.

Модуль импортируется и `figma_extractor.py`, и `generate_landing.py`, чтобы
структура каталогов менялась в одном месте, а не в двух.

Побочный эффект импорта: каталоги `RAW_DIR`, `SPECS_DIR`, `ASSETS_DIR` и
`OUTPUT_BASE` создаются, если их ещё нет.
"""

from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

CONFIGS_DIR = PROJECT_ROOT / "configs"  # JSON-конфиги правил и настроек
RAW_DIR = PROJECT_ROOT / "data" / "raw"  # кэш ответов Figma API и служебные файлы
SPECS_DIR = PROJECT_ROOT / "data" / "specs"  # spec-файлы, собранные figma_extractor.py
ASSETS_DIR = PROJECT_ROOT / "assets"  # скачанные ассеты и референсные PNG
OUTPUT_BASE = PROJECT_ROOT / "output"  # каталоги запусков `run_<timestamp>`
ASSET_MANIFEST_PATH = RAW_DIR / "asset_manifest.json"  # индекс скачанных ассетов
TOKEN_COOLDOWN_PATH = RAW_DIR / "_token_cooldowns.json"  # кулдауны токенов Figma API

for _dir in (RAW_DIR, SPECS_DIR, ASSETS_DIR, OUTPUT_BASE):
    _dir.mkdir(parents=True, exist_ok=True)