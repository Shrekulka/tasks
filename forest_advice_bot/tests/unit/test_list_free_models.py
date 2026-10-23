from scripts.list_free_models import fetch_free_models
from tests.fake_openrouter import make_llm_config, model_entry


async def test_lists_only_free_models_by_context_desc(fake_api, http_session):
    fake_api.catalog(
        model_entry("small/one:free", context=16000),
        model_entry("paid/two:free", price="0.5"),
        model_entry("big/three:free", context=128000),
    )

    models = await fetch_free_models(make_llm_config(fake_api.base_url), http_session)

    assert [m.id for m in models] == ["big/three:free", "small/one:free"]
