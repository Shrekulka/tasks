import pytest

from external_services.llm.free_filter import FreeModelPolicy, parse_free_model, select_free_models

POLICY = FreeModelPolicy(
    free_id_suffix=":free",
    min_context=8000,
    required_input=frozenset({"text"}),
    required_output=frozenset({"text"}),
    required_prices=("prompt", "completion"),
)


def _model(model_id="vendor/model:free", **overrides):
    base = {
        "id": model_id,
        "pricing": {"prompt": "0", "completion": "0"},
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        "context_length": 32000,
    }
    base.update(overrides)
    return base


def test_valid_free_model_is_accepted():
    assert parse_free_model(_model(), POLICY).id == "vendor/model:free"


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "vendor/model"},  # нет суффикса :free
        {"pricing": None},  # нет цены: fail-closed
        {"pricing": {"prompt": "0"}},  # нет обязательного поля
        {"pricing": {"prompt": "0.000001", "completion": "0"}},  # платная
        {"pricing": {"prompt": "0", "completion": "abc"}},  # мусор вместо числа
        {"architecture": {"input_modalities": ["image"], "output_modalities": ["text"]}},
        {"architecture": None},
        {"context_length": 4000},
        {"context_length": "32000"},
        {"context_length": True},
    ],
)
def test_unconfirmed_model_is_rejected(overrides):
    assert parse_free_model(_model(**overrides), POLICY) is None


def test_pinned_paid_model_is_not_allowed():
    paid = _model("vendor/paid:free", pricing={"prompt": "0.1", "completion": "0"})
    ids = [m.id for m in select_free_models([_model(), paid], POLICY, pinned=["vendor/paid:free"])]
    assert ids == ["vendor/model:free"]


def test_pinned_first_then_by_context_desc():
    big = _model("vendor/big:free", context_length=128000)
    ids = [m.id for m in select_free_models([big, _model()], POLICY, pinned=["vendor/model:free"])]
    assert ids == ["vendor/model:free", "vendor/big:free"]


def test_blocklist_and_quarantine_are_excluded():
    other = _model("vendor/other:free")
    ids = [
        m.id
        for m in select_free_models(
            [_model(), other],
            POLICY,
            blocklist=["vendor/model:free"],
            quarantined=frozenset({"vendor/other:free"}),
        )
    ]
    assert ids == []
