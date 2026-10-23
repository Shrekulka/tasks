# forest-advice-bot/external_services/llm/llm_client.py

"""Клиент LLM: перебор бесплатных моделей, карантины, лимиты, квота.

Зависит только от интерфейса провайдера и политик из ProviderBundle. Время и ожидание
подаются снаружи, поэтому тесты проходят мгновенно.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from config_data.config import LLMConfig
from errors.errors import (
    LLMAccountBlocked,
    LLMQuotaExceeded,
    LLMUnavailable,
)
from external_services.llm.base import ModelInfo, ProviderTransportError
from external_services.llm.error_policy import Action, classify
from external_services.llm.free_filter import select_free_models
from external_services.llm.quota import QuotaTracker
from external_services.llm.registry import ProviderBundle
from utils.utils import SlidingWindowLimiter, TTLSet

log = logging.getLogger(__name__)

Validator = Callable[[str], bool]
Messages = Sequence[Mapping[str, str]]

_RATE_WINDOW_SECONDS = 60.0  # LLM_MAX_RPM задаётся «в минуту»: окно и есть минута


_ERROR_DETAIL_LIMIT = 200  # длина фрагмента сообщения об ошибке провайдера в логе


def _error_detail(body: Mapping[str, Any]) -> str:
    """Короткая причина из тела ответа для лога (без ключей и без текста вопроса)."""
    error = body.get("error") if isinstance(body, Mapping) else None
    message = error.get("message") if isinstance(error, Mapping) else None
    if not isinstance(message, str) or not message.strip():
        return "тело ответа без message"
    return " ".join(message.split())[:_ERROR_DETAIL_LIMIT]


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    model: str
    key_index: int


@dataclass(frozen=True, slots=True)
class _Attempt:
    result: LLMResult | None = None
    upstream_limited: bool = False  # закончилось 429 поставщика модели
    retry_after: float | None = None  # подсказка поставщика, сколько ждать
    give_up: bool = False  # ответ не прошёл проверку и после строгого повтора: дальше не идём


@dataclass(slots=True)
class _AskState:
    """Бюджет одного вызова ask: строгий повтор разрешён один раз на весь вызов."""

    strict_retries_left: int
    last_retry_after: float | None = None


class LLMClient:
    def __init__(
        self,
        bundle: ProviderBundle,
        config: LLMConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._utc_now = utc_now
        self._provider = bundle.provider
        self._free_policy = bundle.free_policy
        self._error_policy = bundle.error_policy
        self._json_mode = bundle.json_mode if config.json_mode == "auto" else None
        self._config = config
        self._clock = clock
        self._sleep = sleep

        self._bad_keys: TTLSet[int] = TTLSet(clock)
        self._bad_models: TTLSet[str] = TTLSet(clock)
        self._bad_pairs: TTLSet[tuple[str, int]] = TTLSet(clock)
        self._json_off: TTLSet[str] = TTLSet(clock)  # модели, отвергшие JSON-режим (400)
        self._limiter = SlidingWindowLimiter(config.max_rpm, _RATE_WINDOW_SECONDS, clock)
        self._limiter_lock = asyncio.Lock()
        self._quota = QuotaTracker(
            ttl_seconds=config.quota_cache_seconds,
            fallback_limit=config.daily_request_limit,
            clock=clock,
            utc_now=utc_now,
        )

        self._catalog: list[Mapping[str, Any]] = []
        self._catalog_at: float | None = None
        self._catalog_retry_at = 0.0
        self._catalog_lock = asyncio.Lock()

        self._pause_until = 0.0
        self._pause_is_account = False
        self._pause_daily = False

    # ------------------------------------------------------------------ публичное

    async def ask(
        self,
        messages: Messages,
        *,
        validate: Validator | None = None,
        options: Mapping[str, Any] | None = None,
        retry_messages: Messages | None = None,
    ) -> LLMResult:
        """Возвращает первый ответ, прошедший validate. Иначе бросает LLMError.

        retry_messages: более строгий вариант запроса для одного повтора на той же модели,
        если ответ не прошёл validate (LLM_INVALID_RESPONSE_STRATEGY=strict_retry).

        LLMQuotaExceeded: лимит. LLMAccountBlocked: аккаунт заблокирован.
        LLMUnavailable: технические причины, перебор исчерпан или дедлайн.
        """
        if self._config.deadline_seconds <= 0:
            raise LLMUnavailable("дедлайн равен нулю")
        deadline = self._clock() + self._config.deadline_seconds
        self._raise_if_paused()
        if not self._alive_keys():
            raise LLMUnavailable("нет рабочих ключей")

        models = await self._candidate_models(deadline)
        if not models:
            raise LLMUnavailable("нет подходящих бесплатных моделей")

        await self._ensure_quota(deadline)

        strict = self._config.invalid_response_strategy == "strict_retry"
        state = _AskState(strict_retries_left=1 if strict and retry_messages else 0)
        limited = 0  # сколько попыток закончились 429 поставщика
        for model in models:
            if deadline - self._clock() <= 0:
                break
            attempt = await self._try_model(
                model, messages, validate, options or {}, deadline, retry_messages, state
            )
            if attempt.result is not None:
                return attempt.result
            if attempt.give_up:
                raise LLMUnavailable("ответ не прошёл проверку после строгого повтора")
            if attempt.upstream_limited:
                limited += 1
                if limited >= self._config.max_limited_attempts:
                    break  # D30: дальше только тратим суточный лимит

        if limited:
            self._on_upstream_exhausted(limited, state)
        raise LLMUnavailable("ни одна модель не дала годного ответа")

    def _on_upstream_exhausted(self, limited: int, state: _AskState) -> None:
        """Поставщики ответили 429: пауза по их Retry-After, иначе по настройке."""
        pause = state.last_retry_after or self._config.limit_pause_seconds
        reached = limited >= self._config.max_limited_attempts
        if reached and pause > 0:
            self._start_pause(pause, account=False)
        raise LLMQuotaExceeded(
            pause or self._config.rate_pause_seconds,
            "лимит поставщика моделей",
            daily=pause >= self._config.daily_reset_threshold_seconds,
        )

    # ------------------------------------------------------------------ перебор

    async def _try_model(
        self,
        model: ModelInfo,
        messages: Messages,
        validate: Validator | None,
        options: Mapping[str, Any],
        deadline: float,
        retry_messages: Messages | None,
        state: _AskState,
    ) -> _Attempt:
        keys = self._config.api_keys
        model_id = model.id
        request_options = self._options_for(model, options)
        current = messages
        pair_failures = 0  # сколько ключей уже получили отказ именно от этой модели
        while True:
            key_index = self._pick_key(model_id)
            if key_index is None:
                if self._alive_keys():  # ключи живы, но все отказали именно для этой модели
                    self._bad_models.add(model_id, self._config.model_quarantine_seconds)
                return _Attempt()

            await self._wait_for_slot(deadline)
            remaining = deadline - self._clock()
            if remaining <= 0:
                return _Attempt()

            try:
                response = await self._provider.complete(
                    model_id,
                    current,
                    api_key=keys[key_index],
                    timeout_seconds=min(self._config.timeout_seconds, remaining),
                    options=request_options,
                )
            except ProviderTransportError as exc:
                self._charge(None)  # запрос мог дойти до провайдера и быть засчитан
                log.warning("LLM: нет ответа (%s), модель %s, key#%d", exc, model_id, key_index + 1)
                return _Attempt()
            self._charge(response.status)

            if response.status == 200:
                attempt = self._accept(model_id, key_index, response.body, validate)
                if not attempt.give_up:
                    return attempt
                # Ответ не прошёл проверку (JSON, язык). Строгий повтор один на весь вызов.
                if state.strict_retries_left > 0 and retry_messages is not None:
                    state.strict_retries_left -= 1
                    current = retry_messages
                    log.info("LLM: строгий повтор, модель %s", model_id)
                    continue
                return attempt

            if response.status == 400 and self._json_active(model, options, request_options):
                # Модель заявила JSON-режим, но отвергла запрос: повторяем без него и
                # запоминаем. Это не квота и не повод отключать модель.
                self._json_off.add(model_id, self._config.model_quarantine_seconds)
                request_options = self._options_for(model, options)
                log.warning("LLM: модель %s отвергла JSON-режим, повтор без него", model_id)
                continue

            decision = classify(
                response.status,
                response.headers,
                response.body,
                self._error_policy,
                now=self._utc_now().timestamp(),
            )
            log.warning(
                "LLM: HTTP %d, модель %s, key#%d: %s (%s)",
                response.status,
                model_id,
                key_index + 1,
                decision.action.value,
                f"{decision.reason}; {_error_detail(response.body)}",
            )
            match decision.action:
                case Action.QUARANTINE_KEY:
                    self._bad_keys.add(key_index, self._ttl(decision.retry_after, "key"))
                case Action.QUARANTINE_PAIR:
                    self._bad_pairs.add(
                        (model_id, key_index), self._config.model_quarantine_seconds
                    )
                    pair_failures += 1
                    if pair_failures >= self._config.pair_failures_before_model_quarantine:
                        # Отказ у нескольких разных ключей: дело в модели, а не в ключе.
                        # Остальные ключи не тратим, модель отключается целиком.
                        self._bad_models.add(model_id, self._config.model_quarantine_seconds)
                        return _Attempt()
                case Action.QUARANTINE_MODEL:
                    self._bad_models.add(model_id, self._config.model_quarantine_seconds)
                    return _Attempt()
                case Action.NEXT_MODEL:
                    if response.status != 429:
                        return _Attempt()
                    # 429 поставщика этой модели: другая модель может ответить. Эту
                    # отключаем ненадолго (с учётом Retry-After), глобальной паузы нет.
                    quarantine = self._config.upstream_quarantine_seconds
                    if quarantine > 0:
                        self._bad_models.add(model_id, max(quarantine, decision.retry_after or 0))
                    state.last_retry_after = decision.retry_after or state.last_retry_after
                    return _Attempt(upstream_limited=True, retry_after=decision.retry_after)
                case Action.GLOBAL_PAUSE:
                    wait = decision.retry_after or self._config.rate_pause_seconds
                    self._start_pause(wait, account=False)
                    raise LLMQuotaExceeded(
                        wait,
                        decision.reason,
                        daily=wait >= self._config.daily_reset_threshold_seconds,
                    )
                case Action.ACCOUNT_BLOCKED:
                    wait = decision.retry_after or self._config.account_pause_seconds
                    self._start_pause(wait, account=True)
                    raise LLMAccountBlocked(wait)

    def _charge(self, status: int | None) -> None:
        """Локальный счётчик дневной квоты: приближение к счётчику провайдера.

        Всегда: ответ 200. Дополнительно при LLM_COUNT_FAILED_REQUESTS (по умолчанию да):
        таймауты, 429, 5xx и прочее, кроме 401 (ключ отвергнут до учёта). Консервативно:
        лучше считать лишнее, чем исчерпать лимит провайдера. Остаток провайдера
        перечитывается из GET /key и перекрывает локальную оценку.
        """
        if status == 200 or (self._config.count_failed_requests and status != 401):
            self._quota.consume()

    def _json_active(
        self, model: ModelInfo, options: Mapping[str, Any], request_options: Mapping[str, Any]
    ) -> bool:
        return self._json_mode is not None and request_options is not options

    def _accept(
        self,
        model: str,
        key_index: int,
        body: Mapping[str, Any],
        validate: Validator | None,
    ) -> _Attempt:
        text = self._provider.extract_text(body)
        if text is None:
            log.warning("LLM: пустой ответ, модель %s", model)
            return _Attempt()
        if validate is not None and not validate(text):
            log.warning("LLM: ответ не прошёл проверку, модель %s", model)
            # give_up здесь означает «недействительный ответ»; решает вызывающий код
            if self._config.invalid_response_strategy == "next_model":
                return _Attempt()
            return _Attempt(give_up=True)
        return _Attempt(result=LLMResult(text=text, model=model, key_index=key_index))

    def _options_for(self, model: ModelInfo, options: Mapping[str, Any]) -> Mapping[str, Any]:
        """Опции запроса. JSON-режим добавляется, только если модель заявила его поддержку."""
        spec = self._json_mode
        if spec is None or not model.supports(spec.capability) or model.id in self._json_off:
            return options
        return {**spec.options, **options}  # явные options вызывающего важнее

    # ------------------------------------------------------------------ состояние

    def _alive_keys(self) -> list[int]:
        return [i for i in range(len(self._config.api_keys)) if i not in self._bad_keys]

    def _pick_key(self, model: str) -> int | None:
        for index in self._alive_keys():
            if (model, index) not in self._bad_pairs:
                return index
        return None

    def _ttl(self, retry_after: float | None, kind: str) -> float:
        # Если провайдер назвал время сброса, ждём не меньше его, но не короче настройки.
        base = (
            self._config.key_quarantine_seconds
            if kind == "key"
            else (self._config.model_quarantine_seconds)
        )
        return max(base, retry_after or 0.0)

    def _start_pause(self, seconds: float, *, account: bool) -> None:
        self._pause_until = self._clock() + seconds
        self._pause_is_account = account
        self._pause_daily = seconds >= self._config.daily_reset_threshold_seconds

    def _raise_if_paused(self) -> None:
        remaining = self._pause_until - self._clock()
        if remaining <= 0:
            return
        if self._pause_is_account:
            raise LLMAccountBlocked(remaining)
        raise LLMQuotaExceeded(remaining, "общая пауза", daily=self._pause_daily)

    async def _wait_for_slot(self, deadline: float) -> None:
        """Ждёт свободный слот минутного лимита, но не дольше дедлайна."""
        async with self._limiter_lock:
            while True:
                wait = self._limiter.try_acquire()
                if wait <= 0:
                    return
                if self._clock() + wait > deadline:
                    raise LLMQuotaExceeded(wait, "минутный лимит")
                await self._sleep(wait)

    # ------------------------------------------------------------------ каталог и квота

    async def _candidate_models(self, deadline: float) -> list[ModelInfo]:
        catalog = await self._get_catalog(deadline)
        models = select_free_models(
            catalog,
            self._free_policy,
            pinned=self._config.models,
            blocklist=self._config.models_blocklist,
            quarantined=self._bad_models.active(),
        )
        if catalog and not models:
            log.warning(
                "LLM: каталог вернул %d моделей, но ни одна не прошла фильтр бесплатных "
                "(блоклист, карантин, цена, контекст): проверьте scripts.list_free_models",
                len(catalog),
            )
        return models[: self._config.max_models_tried]

    async def _get_catalog(self, deadline: float) -> list[Mapping[str, Any]]:
        async with self._catalog_lock:
            now = self._clock()
            fresh = (
                self._catalog_at is not None
                and now - self._catalog_at < self._config.catalog_ttl_seconds
            )
            if fresh or now < self._catalog_retry_at:
                return self._catalog
            try:
                data = await self._provider.list_models(
                    timeout_seconds=max(min(self._config.timeout_seconds, deadline - now), 0.001)
                )
            except ProviderTransportError as exc:
                log.warning("LLM: каталог моделей недоступен (%s), берём прошлый список", exc)
                self._catalog_retry_at = now + self._config.rate_pause_seconds
                return self._catalog
            self._catalog = list(data)
            self._catalog_at = now
            return self._catalog

    async def _ensure_quota(self, deadline: float) -> None:
        if not self._quota.is_fresh():
            alive = self._alive_keys()
            remaining = deadline - self._clock()
            quota = None
            if alive and remaining > 0:
                try:
                    quota = await self._provider.fetch_quota(
                        api_key=self._config.api_keys[alive[0]],
                        timeout_seconds=min(self._config.timeout_seconds, remaining),
                    )
                except ProviderTransportError as exc:
                    log.warning("LLM: остаток квоты не получен (%s)", exc)
            self._quota.observe(quota)
        if self._quota.exhausted():
            raise LLMQuotaExceeded(
                self._quota.seconds_until_reset(), "дневная квота исчерпана", daily=True
            )
