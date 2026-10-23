import pytest

from services.embeddings import SentenceTransformerEmbedder, with_prefix


class FakeModel:
    """Записывает, с чем её вызвали. Возвращает по вектору из двух чисел на текст."""

    def __init__(self, dim: int = 2) -> None:
        self.dim = dim
        self.calls: list[dict] = []

    def get_sentence_embedding_dimension(self) -> int:
        return self.dim

    def encode(self, texts, **kwargs):
        self.calls.append({"texts": list(texts), **kwargs})
        return [[float(len(t)), 1.0] for t in texts]


def _embedder(model: FakeModel | None = None, **overrides):
    values = {
        "model_name": "fake/e5",
        "query_prefix": "query:",
        "passage_prefix": "passage:",
        "batch_size": 8,
    }
    values.update(overrides)
    return SentenceTransformerEmbedder(model or FakeModel(), **values)


@pytest.mark.parametrize(
    ("prefix", "text", "expected"),
    [("query:", "ліс", "query: ліс"), ("", "ліс", "ліс")],
)
def test_with_prefix(prefix, text, expected):
    assert with_prefix(prefix, text) == expected


async def test_passages_get_passage_prefix_and_options():
    model = FakeModel()

    vectors = await _embedder(model).embed_passages(["один", "два"])

    (call,) = model.calls
    assert call["texts"] == ["passage: один", "passage: два"]
    assert call["normalize_embeddings"] is True
    assert call["batch_size"] == 8
    assert call["show_progress_bar"] is False
    assert vectors == [[len("passage: один") * 1.0, 1.0], [len("passage: два") * 1.0, 1.0]]


async def test_query_gets_query_prefix():
    model = FakeModel()

    vector = await _embedder(model).embed_query("де ховати горіхи?")

    assert model.calls[0]["texts"] == ["query: де ховати горіхи?"]
    assert len(vector) == 2


async def test_empty_prefixes_leave_texts_untouched():
    model = FakeModel()
    embedder = _embedder(model, query_prefix="", passage_prefix="")

    await embedder.embed_query("q")
    await embedder.embed_passages(["p"])

    assert [c["texts"] for c in model.calls] == [["q"], ["p"]]


async def test_empty_input_does_not_call_the_model():
    model = FakeModel()

    assert await _embedder(model).embed_passages([]) == []
    assert model.calls == []


def test_dim_comes_from_the_model():
    assert _embedder(FakeModel(dim=768)).dim == 768


def test_dim_uses_new_method_name_when_available():
    class NewApiModel(FakeModel):
        def get_embedding_dimension(self) -> int:
            return 1024

    assert _embedder(NewApiModel(dim=2)).dim == 1024


class _TokenizingModel:
    """Модель с токенайзером «один символ = один токен» и окном max_seq_length."""

    max_seq_length = 20

    def __init__(self):
        self.tokenizer = lambda texts, **_: {"input_ids": [list(range(len(t))) for t in texts]}

    def get_sentence_embedding_dimension(self):
        return 2

    def encode(self, texts, batch_size, normalize_embeddings, show_progress_bar):
        return [[1.0, 0.0]] * len(texts)


async def test_texts_longer_than_model_window_are_counted_and_logged(caplog):
    embedder = _embedder(_TokenizingModel(), passage_prefix="")

    with caplog.at_level("WARNING"):
        await embedder.embed_passages(["коротко", "а" * 50])

    assert embedder.truncated_total == 1
    assert "обрезаны" in caplog.text
