# forest-advice-bot/external_services/llm/free_filter.py

"""Отбор бесплатных моделей. Принцип fail-closed: нет данных значит модель отклонена."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from external_services.llm.base import ModelInfo


@dataclass(frozen=True, slots=True)
class FreeModelPolicy:
    """Все значения задаёт адаптер провайдера и конфиг, здесь ничего не зашито."""

    free_id_suffix: str
    min_context: int
    required_input: frozenset[str]
    required_output: frozenset[str]
    required_prices: tuple[str, ...]


def _is_zero_price(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        return Decimal(str(value)) == 0
    except (InvalidOperation, ValueError):
        return False


def _is_blank_price(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _modalities(value: Any) -> frozenset[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return frozenset(str(item) for item in value)
    return frozenset()


def parse_free_model(raw: Mapping[str, Any], policy: FreeModelPolicy) -> ModelInfo | None:
    """Возвращает ModelInfo, только если ВСЕ условия подтверждены данными каталога."""
    model_id = raw.get("id")
    if not isinstance(model_id, str) or not model_id.endswith(policy.free_id_suffix):
        return None

    pricing = raw.get("pricing")
    if not isinstance(pricing, Mapping):
        return None
    if any(key not in pricing for key in policy.required_prices):
        return None
    # Обязательные цены должны быть нулевыми. Остальные поля (request, image, ...) могут быть
    # null или пустыми (так бывает в каталоге), но не могут быть ненулевыми: деньги не тратим.
    for key, value in pricing.items():
        if key in policy.required_prices:
            if not _is_zero_price(value):
                return None
        elif not _is_blank_price(value) and not _is_zero_price(value):
            return None

    architecture = raw.get("architecture")
    if not isinstance(architecture, Mapping):
        return None
    if not policy.required_input <= _modalities(architecture.get("input_modalities")):
        return None
    if not policy.required_output <= _modalities(architecture.get("output_modalities")):
        return None

    context = raw.get("context_length")
    if isinstance(context, bool) or not isinstance(context, int) or context < policy.min_context:
        return None

    return ModelInfo(
        id=model_id,
        context_length=context,
        supported_parameters=_modalities(raw.get("supported_parameters")),
    )


def select_free_models(
    catalog: Iterable[Mapping[str, Any]],
    policy: FreeModelPolicy,
    *,
    pinned: Sequence[str] = (),
    blocklist: Sequence[str] = (),
    quarantined: frozenset[str] = frozenset(),
) -> list[ModelInfo]:
    """Закреплённые модели идут первыми, но только если прошли тот же фильтр."""
    free = {info.id: info for raw in catalog if (info := parse_free_model(raw, policy)) is not None}
    blocked = set(blocklist) | quarantined
    ordered: list[ModelInfo] = [free[m] for m in dict.fromkeys(pinned) if m in free]
    rest = sorted(
        (info for info in free.values() if info not in ordered),
        key=lambda info: (-info.context_length, info.id),
    )
    return [info for info in (*ordered, *rest) if info.id not in blocked]
