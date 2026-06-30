from __future__ import annotations

from typing import cast

import pytest

from reducto.lib.oracledb.embeddings import (
    CohereEmbeddingProvider,
    OracleLLMEmbeddingProvider,
    embed_many,
    embedding_provider_name,
    embedding_provider_from_env,
)


def test_cohere_embedding_provider_posts_v2_payload() -> None:
    class _Response:
        def json(self) -> dict[str, object]:
            return {"embeddings": {"float": [[0.1, 0.2, 0.3]]}}

        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self) -> None:
            self.payload: dict[str, object] | None = None

        def post(self, _url: str, **kwargs: object) -> _Response:
            self.payload = kwargs["json"]  # type: ignore[assignment]
            return _Response()

    client = _Client()
    provider = CohereEmbeddingProvider(
        api_key="test-key",
        dimensions=3,
        input_type="search_query",
        http_client=client,
    )

    assert provider.embed_text("revenue") == [0.1, 0.2, 0.3]
    assert client.payload is not None
    assert client.payload["model"] == "embed-english-light-v3.0"
    assert client.payload["texts"] == ["revenue"]
    assert client.payload["input_type"] == "search_query"
    assert client.payload["embedding_types"] == ["float"]


def test_cohere_embedding_provider_batches_requests() -> None:
    class _Response:
        def __init__(self, count: int) -> None:
            self.count = count

        def json(self) -> dict[str, object]:
            return {"embeddings": {"float": [[0.1, 0.2] for _ in range(self.count)]}}

        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def post(self, _url: str, **kwargs: object) -> _Response:
            payload = cast(dict[str, object], kwargs["json"])
            self.payloads.append(payload)
            texts = cast(list[object], payload["texts"])
            return _Response(len(texts))

    client = _Client()
    provider = CohereEmbeddingProvider(
        api_key="test-key",
        dimensions=2,
        batch_size=2,
        http_client=client,
    )

    assert provider.embed_texts(["one", "two", "three"]) == [
        [0.1, 0.2],
        [0.1, 0.2],
        [0.1, 0.2],
    ]
    assert [payload["texts"] for payload in client.payloads] == [["one", "two"], ["three"]]


def test_cohere_embedding_provider_handles_empty_batches_without_api_call() -> None:
    class _Client:
        def post(self, _url: str, **_kwargs: object) -> object:
            raise AssertionError("empty embedding batches should not call Cohere")

    provider = CohereEmbeddingProvider(api_key="test-key", dimensions=2, http_client=_Client())

    assert provider.embed_texts([]) == []


def test_cohere_embedding_provider_rejects_dimension_mismatch() -> None:
    class _Response:
        def json(self) -> dict[str, object]:
            return {"embeddings": {"float": [[0.1, 0.2]]}}

        def raise_for_status(self) -> None:
            return None

    class _Client:
        def post(self, _url: str, **_kwargs: object) -> _Response:
            return _Response()

    provider = CohereEmbeddingProvider(api_key="test-key", dimensions=3, http_client=_Client())

    with pytest.raises(ValueError, match="Cohere returned 2 dimensions"):
        provider.embed_text("revenue")


def test_cohere_embedding_provider_defaults_to_model_dimensions() -> None:
    provider = CohereEmbeddingProvider(api_key="test-key")

    assert provider.dimensions == 384


def test_oracle_llm_embedding_provider_posts_openai_payload() -> None:
    class _Response:
        def json(self) -> dict[str, object]:
            return {"data": [{"embedding": [0.1, 0.2, 0.3], "index": 0}]}

        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self) -> None:
            self.url: str | None = None
            self.payload: dict[str, object] | None = None

        def post(self, url: str, **kwargs: object) -> _Response:
            self.url = url
            self.payload = kwargs["json"]  # type: ignore[assignment]
            return _Response()

    client = _Client()
    provider = OracleLLMEmbeddingProvider(
        api_key="test-key",
        dimensions=3,
        input_type="search_document",
        embed_url="https://dbdevllms.oraclecorp.com/embeddings",
        http_client=client,
    )

    assert provider.embed_text("revenue") == [0.1, 0.2, 0.3]
    assert client.url == "https://dbdevllms.oraclecorp.com/embeddings"
    assert client.payload is not None
    assert client.payload["model"] == "nim/llama-3.2-nv-embedqa-1b-v2"
    assert client.payload["input"] == ["revenue"]
    assert client.payload["input_type"] == "passage"


def test_oracle_llm_embedding_provider_batches_query_requests() -> None:
    class _Response:
        def __init__(self, count: int) -> None:
            self.count = count

        def json(self) -> dict[str, object]:
            return {"data": [{"embedding": [0.1, 0.2], "index": index} for index in range(self.count)]}

        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def post(self, _url: str, **kwargs: object) -> _Response:
            payload = cast(dict[str, object], kwargs["json"])
            self.payloads.append(payload)
            texts = cast(list[object], payload["input"])
            return _Response(len(texts))

    client = _Client()
    provider = OracleLLMEmbeddingProvider(
        api_key="test-key",
        dimensions=2,
        input_type="search_query",
        batch_size=2,
        embed_url="https://dbdevllms.oraclecorp.com/embeddings",
        http_client=client,
    )

    assert provider.embed_texts(["one", "two", "three"]) == [
        [0.1, 0.2],
        [0.1, 0.2],
        [0.1, 0.2],
    ]
    assert [payload["input"] for payload in client.payloads] == [["one", "two"], ["three"]]
    assert all(payload["input_type"] == "query" for payload in client.payloads)


def test_oracle_llm_embedding_provider_truncates_oversized_inputs() -> None:
    class _Response:
        def json(self) -> dict[str, object]:
            return {"data": [{"embedding": [0.1, 0.2], "index": 0}]}

        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self) -> None:
            self.payload: dict[str, object] | None = None

        def post(self, _url: str, **kwargs: object) -> _Response:
            self.payload = kwargs["json"]  # type: ignore[assignment]
            return _Response()

    client = _Client()
    provider = OracleLLMEmbeddingProvider(
        api_key="test-key",
        dimensions=2,
        max_input_chars=5,
        embed_url="https://dbdevllms.oraclecorp.com/embeddings",
        http_client=client,
    )

    assert provider.embed_text("abcdefghij") == [0.1, 0.2]
    assert client.payload is not None
    assert client.payload["input"] == ["abcde"]


def test_oracle_llm_embedding_provider_rejects_dimension_mismatch() -> None:
    class _Response:
        def json(self) -> dict[str, object]:
            return {"data": [{"embedding": [0.1, 0.2], "index": 0}]}

        def raise_for_status(self) -> None:
            return None

    class _Client:
        def post(self, _url: str, **_kwargs: object) -> _Response:
            return _Response()

    provider = OracleLLMEmbeddingProvider(
        api_key="test-key",
        dimensions=3,
        embed_url="https://dbdevllms.oraclecorp.com/embeddings",
        http_client=_Client(),
    )

    with pytest.raises(ValueError, match="Oracle LLMs returned 2 dimensions"):
        provider.embed_text("revenue")


def test_oracle_llm_embedding_provider_defaults_to_model_dimensions() -> None:
    provider = OracleLLMEmbeddingProvider(api_key="test-key")

    assert provider.dimensions == 2048


def test_embed_many_rejects_provider_count_mismatch() -> None:
    class _BadBatchProvider:
        dimensions = 2

        def embed_text(self, text: str) -> list[float]:
            del text
            return [0.0, 0.0]

        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            del texts
            return [[0.0, 0.0]]

    with pytest.raises(ValueError, match="returned 1 vectors for 2 texts"):
        embed_many(_BadBatchProvider(), ["one", "two"])


def test_embedding_provider_from_env_uses_cohere_when_key_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("ORACLE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DBDEV_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DBDEV_LLMS_API_KEY", raising=False)
    monkeypatch.setenv("CO_API_KEY", "test-key")
    monkeypatch.setenv("ORACLE_VECTOR_DIMENSIONS", "384")

    provider = embedding_provider_from_env(input_type="search_document")

    assert isinstance(provider, CohereEmbeddingProvider)
    assert provider.input_type == "search_document"


def test_embedding_provider_from_env_uses_oracle_when_key_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("CO_API_KEY", raising=False)
    monkeypatch.setenv("ORACLE_LLM_API_KEY", "test-key")
    monkeypatch.setenv("ORACLE_VECTOR_DIMENSIONS", "384")

    provider = embedding_provider_from_env(input_type="search_query")

    assert isinstance(provider, OracleLLMEmbeddingProvider)
    assert provider.input_type == "search_query"
    assert provider.oracle_input_type == "query"


def test_embedding_provider_from_env_defaults_oracle_dimensions_to_model_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("CO_API_KEY", raising=False)
    monkeypatch.delenv("ORACLE_VECTOR_DIMENSIONS", raising=False)
    monkeypatch.setenv("ORACLE_LLM_API_KEY", "test-key")

    provider = embedding_provider_from_env(input_type="search_document")

    assert isinstance(provider, OracleLLMEmbeddingProvider)
    assert provider.dimensions == 2048


def test_embedding_provider_from_env_sets_oracle_max_input_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("CO_API_KEY", raising=False)
    monkeypatch.setenv("ORACLE_LLM_API_KEY", "test-key")
    monkeypatch.setenv("ORACLE_LLM_EMBED_MAX_CHARS", "1234")

    provider = embedding_provider_from_env(input_type="search_document")

    assert isinstance(provider, OracleLLMEmbeddingProvider)
    assert provider.max_input_chars == 1234


def test_embedding_provider_from_env_respects_explicit_cohere_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "cohere")
    monkeypatch.setenv("CO_API_KEY", "cohere-key")
    monkeypatch.setenv("ORACLE_LLM_API_KEY", "oracle-key")

    provider = embedding_provider_from_env(input_type="search_document")

    assert isinstance(provider, CohereEmbeddingProvider)


def test_embedding_provider_from_env_requires_selected_provider_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "oracle")
    monkeypatch.delenv("CO_API_KEY", raising=False)
    monkeypatch.delenv("ORACLE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DBDEV_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DBDEV_LLMS_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="ORACLE_LLM_API_KEY"):
        embedding_provider_from_env(input_type="search_document")


def test_embedding_provider_name_accepts_provider_instance() -> None:
    provider = CohereEmbeddingProvider(api_key="test-key", dimensions=384)

    assert embedding_provider_name(provider) == "cohere:embed-english-light-v3.0"


def test_embedding_provider_name_accepts_oracle_provider_instance() -> None:
    provider = OracleLLMEmbeddingProvider(api_key="test-key", dimensions=384)

    assert embedding_provider_name(provider) == "oracle:nim/llama-3.2-nv-embedqa-1b-v2"


def test_embedding_provider_name_reports_missing_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("CO_API_KEY", raising=False)
    monkeypatch.delenv("ORACLE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DBDEV_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DBDEV_LLMS_API_KEY", raising=False)

    assert embedding_provider_name() == "embeddings:not-configured"
