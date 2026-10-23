# forest-advice-bot/external_services/llm/error_policy.py

"""Классификация ответа провайдера по статусу, заголовкам и структуре тела.

Чистая функция без сети. Все признаки, зависящие от провайдера, приходят в ErrorPolicy,
которую собирает адаптер. В общем коде нет ни одного названия конкретного провайдера.
"""

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class Action(enum.Enum):
    NEXT_MODEL = "next_model"  # сбой этой попытки, модель не виновата
    QUARANTINE_KEY = "quarantine_key"  # ключ неверен или его лимит исчерпан
    QUARANTINE_PAIR = "quarantine_pair"  # пара «модель + ключ»
    QUARANTINE_MODEL = "quarantine_model"  # модель подтверждённо недоступна
    GLOBAL_PAUSE = "global_pause"  # общий лимит платформы: пауза для всех запросов
    ACCOUNT_BLOCKED = "account_blocked"  # блокировка аккаунта: ретраи бесполезны


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    retry_after: float | None = None
    reason: str = ""  # для лога: почему принято такое решение


@dataclass(frozen=True, slots=True)
class ErrorPolicy:
    """Признаки ошибок конкретного провайдера. Задаёт адаптер, а не общий код."""

    platform_rate_headers: tuple[str, ...]  # заголовки платформенного 429 (нижний регистр)
    key_limit_sources: frozenset[str]  # значения metadata.limit_source: лимит ключа
    model_missing_markers: tuple[str, ...]  # подстроки сообщения «модель не найдена»
    model_unsupported_markers: tuple[str, ...]  # подстроки «модель не принимает запрос»
    # подстроки 403, означающие «эта модель вам закрыта целиком», например «доступна только
    # в агентных средах»: ключ тут ни при чём, пробовать другие ключи бессмысленно
    model_restricted_markers: tuple[str, ...] = ()
    # metadata.limit_source временного ограничения бюджета: ждём Retry-After и повторяем,
    # аккаунт не блокируем (для OpenRouter это openrouter_in_flight_budget)
    transient_budget_sources: frozenset[str] = frozenset()
    # limit_source, при которых ретраи бесполезны для всех ключей аккаунта
    account_blocked_sources: frozenset[str] = frozenset()
    # заголовок со временем сброса платформенного лимита (метка epoch в мс или с)
    rate_reset_header: str = ""


def _lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(key).lower(): value for key, value in headers.items()}


def _retry_after(headers: Mapping[str, str]) -> float | None:
    raw = _lower_headers(headers).get("retry-after")
    try:
        value = float(raw) if raw is not None else None
    except ValueError:
        return None
    return value if value is not None and value >= 0 else None


def _reset_after(headers: Mapping[str, str], name: str, now: float | None) -> float | None:
    """Секунды до сброса лимита по заголовку вида X-RateLimit-Reset.

    Значение должно быть меткой времени (epoch, мс или с); иное игнорируется.
    """
    if not name:
        return None
    raw = _lower_headers(headers).get(name.lower())
    try:
        value = float(raw) if raw is not None else None
    except ValueError:
        return None
    if value is None or value < 0:
        return None
    if now is None:
        return None
    if value >= 1e12:  # epoch в миллисекундах
        return max(value / 1000.0 - now, 0.0)
    if value >= 1e9:  # epoch в секундах
        return max(value - now, 0.0)
    return None  # не похоже на метку времени: не гадаем


def _error_parts(body: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    error = body.get("error") if isinstance(body, Mapping) else None
    if not isinstance(error, Mapping):
        return "", {}
    message = error.get("message")
    metadata = error.get("metadata")
    return (
        message.lower() if isinstance(message, str) else "",
        metadata if isinstance(metadata, Mapping) else {},
    )


def classify(
    status: int,
    headers: Mapping[str, str],
    body: Mapping[str, Any],
    policy: ErrorPolicy,
    *,
    now: float | None = None,
) -> Decision:
    """now: текущее время в секундах epoch (нужно, чтобы разобрать метку сброса лимита)."""
    message, metadata = _error_parts(body)
    retry_after = _retry_after(headers)
    limit_source = metadata.get("limit_source")
    key_limited = isinstance(limit_source, str) and limit_source in policy.key_limit_sources

    if status == 401:
        return Decision(Action.QUARANTINE_KEY, reason="invalid or disabled key")

    if status in (402, 403):
        if isinstance(limit_source, str) and limit_source in policy.transient_budget_sources:
            return Decision(Action.GLOBAL_PAUSE, retry_after, "transient budget limit")
        if isinstance(limit_source, str) and limit_source in policy.account_blocked_sources:
            return Decision(Action.ACCOUNT_BLOCKED, retry_after, f"budget {limit_source}")
        if key_limited:
            return Decision(Action.QUARANTINE_KEY, retry_after, "key credit limit exhausted")
        if status == 402 and retry_after is not None:
            # Retry-After на 402 без распознанного источника: провайдер просит подождать
            return Decision(Action.GLOBAL_PAUSE, retry_after, "payment required, retry hinted")
        if status == 402:
            # Баланс аккаунта: другая модель и другой ключ того же аккаунта не помогут.
            return Decision(Action.ACCOUNT_BLOCKED, retry_after, "account credits")
        if any(marker in message for marker in policy.model_restricted_markers):
            return Decision(Action.QUARANTINE_MODEL, reason="model restricted for this use")
        # 403 без распознанной причины: ограничиваем ущерб парой «модель + ключ».
        return Decision(Action.QUARANTINE_PAIR, retry_after, "forbidden, cause unknown")

    if status == 429:
        present = _lower_headers(headers)
        if any(name in present for name in policy.platform_rate_headers):
            wait = retry_after
            if wait is None:
                wait = _reset_after(headers, policy.rate_reset_header, now)
            return Decision(Action.GLOBAL_PAUSE, wait, "platform rate limit")
        return Decision(Action.NEXT_MODEL, retry_after, "upstream rate limit")

    if status == 404 and any(marker in message for marker in policy.model_missing_markers):
        return Decision(Action.QUARANTINE_MODEL, reason="model not found")

    if status == 400 and any(marker in message for marker in policy.model_unsupported_markers):
        return Decision(Action.QUARANTINE_MODEL, reason="request unsupported by model")

    # 400 и 404 без подтверждённой связи с моделью, 408, 5xx, всё неизвестное:
    # следующая попытка без наказания модели. Тело 400 логируем: возможно, виноваты мы.
    return Decision(Action.NEXT_MODEL, reason=f"status {status}")
