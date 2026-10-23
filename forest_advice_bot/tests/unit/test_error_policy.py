import pytest

from external_services.llm.error_policy import Action, ErrorPolicy, classify

POLICY = ErrorPolicy(
    platform_rate_headers=("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"),
    key_limit_sources=frozenset({"key_limit"}),
    model_missing_markers=("no endpoints found",),
    model_unsupported_markers=("not supported by this model",),
    model_restricted_markers=("only available on",),
    transient_budget_sources=frozenset({"openrouter_in_flight_budget"}),
    account_blocked_sources=frozenset({"openrouter_credits", "openrouter_guardrail_budget"}),
    rate_reset_header="x-ratelimit-reset",
)


def _body(message="", **metadata):
    return {"error": {"message": message, "metadata": metadata}}


@pytest.mark.parametrize(
    ("status", "headers", "body", "action", "retry_after"),
    [
        (401, {}, {}, Action.QUARANTINE_KEY, None),
        (402, {}, _body(), Action.ACCOUNT_BLOCKED, None),
        (402, {"Retry-After": "30"}, _body(), Action.GLOBAL_PAUSE, 30.0),
        (
            402,
            {"Retry-After": "7"},
            _body(limit_source="openrouter_in_flight_budget"),
            Action.GLOBAL_PAUSE,
            7.0,
        ),
        (402, {}, _body(limit_source="openrouter_credits"), Action.ACCOUNT_BLOCKED, None),
        (403, {}, _body(limit_source="openrouter_guardrail_budget"), Action.ACCOUNT_BLOCKED, None),
        (402, {}, _body(limit_source="key_limit"), Action.QUARANTINE_KEY, None),
        (403, {}, _body(), Action.QUARANTINE_PAIR, None),
        (403, {}, _body(limit_source="key_limit"), Action.QUARANTINE_KEY, None),
        (
            403,
            {},
            _body("X:free is only available on agentic harnesses"),
            Action.QUARANTINE_MODEL,
            None,
        ),
        (429, {"X-RateLimit-Limit": "20"}, _body(), Action.GLOBAL_PAUSE, None),
        (429, {"Retry-After": "12", "x-ratelimit-reset": "1"}, _body(), Action.GLOBAL_PAUSE, 12.0),
        (429, {}, _body(), Action.NEXT_MODEL, None),
        (404, {}, _body("No endpoints found for x"), Action.QUARANTINE_MODEL, None),
        (404, {}, _body("something else"), Action.NEXT_MODEL, None),
        (400, {}, _body("Parameter is not supported by this model"), Action.QUARANTINE_MODEL, None),
        (400, {}, _body("invalid json in request"), Action.NEXT_MODEL, None),
        (503, {}, {}, Action.NEXT_MODEL, None),
        (408, {}, {}, Action.NEXT_MODEL, None),
    ],
)
def test_classification(status, headers, body, action, retry_after):
    decision = classify(status, headers, body, POLICY)

    assert decision.action is action
    assert decision.retry_after == retry_after


def test_garbage_body_does_not_crash():
    assert classify(500, {}, {"error": "text"}, POLICY).action is Action.NEXT_MODEL
    assert classify(429, {"Retry-After": "abc"}, {}, POLICY).retry_after is None


def test_platform_429_uses_reset_header_when_no_retry_after():
    headers = {
        "X-RateLimit-Limit": "20",
        "X-RateLimit-Reset": str(int((1_760_000_000 + 45) * 1000)),
    }

    decision = classify(429, headers, {}, POLICY, now=1_760_000_000.0)

    assert (decision.action, decision.retry_after) == (Action.GLOBAL_PAUSE, 45.0)


def test_platform_429_prefers_retry_after_over_reset_header():
    headers = {"X-RateLimit-Limit": "20", "Retry-After": "5", "X-RateLimit-Reset": "99999999999999"}

    decision = classify(429, headers, {}, POLICY, now=1_760_000_000.0)

    assert decision.retry_after == 5.0


def test_upstream_429_without_platform_headers_moves_to_next_model():
    decision = classify(
        429, {"Retry-After": "12"}, {"error": {"metadata": {"provider_code": 429}}}, POLICY
    )

    assert (decision.action, decision.retry_after) == (Action.NEXT_MODEL, 12.0)
