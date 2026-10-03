# airadar/radar/services/llm.py

"""LLM через OpenAI-сумісний endpoint (OpenRouter, безкоштовні моделі). Це опційна функція P2.

Список безкоштовних моделей береться з OpenRouter динамічно (кеш 1 година).
Алгоритм вибору: models x keys, перша робоча пара.
Виклик чату зроблено через requests: окремий SDK/langchain заради одного POST не потрібен.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import requests
from django.conf import settings
from django.core.cache import cache

log = logging.getLogger(__name__)

BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
MODELS_CACHE_KEY = "openrouter:free-models:v1"
MODELS_TTL = 3600
BAD_MODEL_TTL = 600  # модель, которая вернула 404/400, откладываем
MIN_CONTEXT = 8_000
MAX_MODELS_TRIED = 5
# відповідь з ієрогліфами/хангилем/арабською вважаємо невдалою і пробуємо наступну пару модель+ключ
FOREIGN_SCRIPT = re.compile(r"[\u0590-\u06ff\u0e00-\u0e7f\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


# ---------- модели ----------

def _env_list(name: str) -> list[str]:
    return [m.strip() for m in os.environ.get(name, "").split(",") if m.strip()]


def _is_free(model: dict) -> bool:
    pricing = model.get("pricing") or {}
    try:
        zero = float(pricing.get("prompt", 1)) == 0 and float(pricing.get("completion", 1)) == 0
    except (TypeError, ValueError):
        return False
    return zero and str(model.get("id", "")).endswith(":free")


def fetch_free_models(*, force: bool = False) -> list[dict]:
    """Бесплатные текстовые модели OpenRouter, отсортированные по размеру контекста."""
    if not force:
        cached = cache.get(MODELS_CACHE_KEY)
        if cached is not None:
            return cached
    try:
        resp = requests.get(f"{BASE_URL}/models", timeout=10)
        resp.raise_for_status()
        raw = resp.json().get("data") or []
    except (requests.RequestException, ValueError) as exc:
        log.warning("OpenRouter models list failed: %s", type(exc).__name__)
        return []

    out: list[dict] = []
    for m in raw:
        if not isinstance(m, dict) or not _is_free(m):
            continue
        arch = m.get("architecture") or {}
        inputs = arch.get("input_modalities") or ["text"]
        outputs = arch.get("output_modalities") or ["text"]
        if "text" not in inputs or "text" not in outputs:
            continue
        ctx = int(m.get("context_length") or 0)
        if ctx < MIN_CONTEXT:
            continue
        out.append({"id": m["id"], "name": m.get("name") or m["id"], "context": ctx,
                    "inputs": inputs})
    out.sort(key=lambda x: (-x["context"], x["id"]))
    cache.set(MODELS_CACHE_KEY, out, MODELS_TTL)
    return out


def candidate_models() -> list[str]:
    """Сначала вручную закреплённые, потом найденные. Недавно упавшие пропускаем."""
    blocked = set(_env_list("OPENROUTER_MODELS_BLOCKLIST"))
    ordered: list[str] = []
    for mid in _env_list("OPENROUTER_MODELS") + [m["id"] for m in fetch_free_models()]:
        if mid in blocked or mid in ordered:
            continue
        if cache.get(f"llm:bad:{mid}"):
            continue
        ordered.append(mid)
    return ordered[:MAX_MODELS_TRIED]


# ---------- ключі та виклик ----------

def get_llm_cfg() -> dict:
    return {
        "api_key_env": "OPENROUTER_API_KEYS",
        "base_url": BASE_URL,
        "temperature": 0.2,
        "timeout_seconds": int(os.environ.get("OPENROUTER_TIMEOUT_SECONDS", "20")),
    }


class LLMFactory:

    @staticmethod
    def get_keys(llm_cfg: dict) -> list[str]:
        env_var = llm_cfg.get("api_key_env")
        if not isinstance(env_var, str) or not env_var:
            log.error("В llm_cfg відсутній 'api_key_env'.")
            return []
        raw_val = os.environ.get(env_var, "").strip()
        if not raw_val:
            return []

        # ["key1","key2"]
        if raw_val.startswith("[") and raw_val.endswith("]"):
            try:
                parsed = json.loads(raw_val)
                if isinstance(parsed, list):
                    return [str(k).strip() for k in parsed if str(k).strip()]
            except (json.JSONDecodeError, TypeError) as err:
                log.debug("API-ключі не розібрано як JSON: %s", err)

        # key1,key2,key3
        return [k.strip().strip("'\"") for k in raw_val.split(",") if k.strip().strip("'\"")]


class LLMCallError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def chat_completion(cfg: dict, model: str, api_key: str, messages: list[dict]) -> str:
    """Один виклик /chat/completions. Піднімає LLMCallError зі статусом HTTP (якщо він є)."""
    try:
        resp = requests.post(
            f"{cfg['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": messages, "temperature": cfg["temperature"]},
            timeout=cfg["timeout_seconds"],
        )
    except requests.RequestException as exc:
        raise LLMCallError(type(exc).__name__) from exc
    if resp.status_code != 200:
        raise LLMCallError(f"HTTP {resp.status_code}", status=resp.status_code)
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMCallError("bad payload") from exc
    return content.strip() if isinstance(content, str) else ""


def llm_enabled() -> bool:
    """Дешева перевірка без мережі: достатньо наявності ключів. Моделі підбираються під час запиту."""
    return bool(LLMFactory.get_keys(get_llm_cfg()))


def ask_with_fallback(messages: list[dict]) -> str | None:
    """Перебирає моделі та ключі, повертає текст першої успішної пари або None.

    Загальний дедлайн (OPENROUTER_DEADLINE_SECONDS) не дає циклу models x keys
    перевищити timeout воркера Gunicorn.
    messages: [{"role": "system"|"user", "content": "..."}].
    """
    cfg = get_llm_cfg()
    keys = LLMFactory.get_keys(cfg)
    if not keys:
        return None

    deadline = time.monotonic() + settings.OPENROUTER_DEADLINE_SECONDS
    for model_name in candidate_models():
        denied = 0  # скільки ключів отримали 403 на цій моделі
        for idx, api_key in enumerate(keys, start=1):
            remaining = deadline - time.monotonic()
            if remaining < 1:
                log.warning("LLM deadline exceeded, stop trying")
                return None
            call_cfg = {**cfg, "timeout_seconds": min(cfg["timeout_seconds"], remaining)}
            try:
                text = chat_completion(call_cfg, model_name, api_key, messages)
            except LLMCallError as exc:
                log.warning("LLM model=%s key#%d failed: %s", model_name, idx, exc)
                if exc.status in (400, 404):
                    cache.set(f"llm:bad:{model_name}", 1, BAD_MODEL_TTL)
                    break
                if exc.status == 403:
                    denied += 1
                continue
            if text and not FOREIGN_SCRIPT.search(text):
                log.info("LLM ok: model=%s key#%d", model_name, idx)
                return text
            log.warning("LLM empty or foreign-script answer: model=%s key#%d", model_name, idx)
        if denied == len(keys):  # усі ключі отримали 403: модель недоступна, а не ключ
            cache.set(f"llm:bad:{model_name}", 1, BAD_MODEL_TTL)
    return None
