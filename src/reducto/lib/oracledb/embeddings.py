from __future__ import annotations

import os
import time
from typing import Any, Literal, Protocol
from collections.abc import Sequence

import httpx

from .config import vector_dimensions_from_env

COHERE_EMBED_URL = "https://api.cohere.com/v2/embed"
DEFAULT_COHERE_EMBED_MODEL = "embed-english-light-v3.0"
DEFAULT_COHERE_DIMENSIONS = 384
DEFAULT_COHERE_BATCH_SIZE = 32
DEFAULT_ORACLE_LLM_BASE_URL = "https://dbdevllms.oraclecorp.com"
DEFAULT_ORACLE_LLM_EMBED_MODEL = "nim/llama-3.2-nv-embedqa-1b-v2"
DEFAULT_ORACLE_LLM_DIMENSIONS = 2048
DEFAULT_ORACLE_LLM_BATCH_SIZE = 1
DEFAULT_ORACLE_LLM_MAX_INPUT_CHARS = 16_000


class EmbeddingProvider(Protocol):
    """Small protocol so Oracle can plug in its approved embedding service."""

    dimensions: int

    def embed_text(self, text: str) -> Sequence[float]:
        """Return one embedding vector."""


def embed_many(provider: EmbeddingProvider, texts: Sequence[str]) -> list[Sequence[float]]:
    if not texts:
        return []
    batch_embed = getattr(provider, "embed_texts", None)
    if callable(batch_embed):
        vectors = list(batch_embed(texts))
    else:
        vectors = [provider.embed_text(text) for text in texts]
    if len(vectors) != len(texts):
        raise ValueError(f"Embedding provider returned {len(vectors)} vectors for {len(texts)} texts.")
    return vectors


class CohereEmbeddingProvider:
    """Cohere embedding provider for Oracle vector search.

    Defaults to `embed-english-light-v3.0` because it returns 384-dimensional
    vectors, matching the default Oracle VECTOR schema.
    """

    def __init__(
        self,
        *,
        api_key: str,
        dimensions: int = DEFAULT_COHERE_DIMENSIONS,
        input_type: Literal["search_document", "search_query"] = "search_document",
        model: str = DEFAULT_COHERE_EMBED_MODEL,
        timeout: float = 30.0,
        max_retries: int = 4,
        batch_size: int = DEFAULT_COHERE_BATCH_SIZE,
        embed_url: str = COHERE_EMBED_URL,
        http_client: Any | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must be provided")
        if dimensions < 1:
            raise ValueError("dimensions must be positive")
        if input_type not in {"search_document", "search_query"}:
            raise ValueError("input_type must be search_document or search_query")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._api_key = api_key
        self.dimensions = dimensions
        self.input_type = input_type
        self.model = model
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.batch_size = batch_size
        self.embed_url = embed_url
        self._http_client = http_client

    def embed_text(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        normalized_texts = [str(text) for text in texts]
        if not normalized_texts:
            return []

        all_vectors: list[list[float]] = []
        for batch in _batched(normalized_texts, self.batch_size):
            payload: dict[str, object] = {
                "model": self.model,
                "texts": batch,
                "input_type": self.input_type,
                "embedding_types": ["float"],
            }
            if self.model.startswith("embed-v4"):
                payload["output_dimension"] = self.dimensions

            response = self._post(payload)
            vectors = _cohere_float_embeddings(response.json())
            if len(vectors) != len(batch):
                raise ValueError(f"Cohere returned {len(vectors)} vectors for {len(batch)} input texts.")
            for vector in vectors:
                if len(vector) != self.dimensions:
                    raise ValueError(
                        f"Cohere returned {len(vector)} dimensions; expected {self.dimensions}. "
                        "Set ORACLE_VECTOR_DIMENSIONS to match the Cohere model or choose a "
                        "model/output dimension that matches the Oracle VECTOR column."
                    )
            all_vectors.extend(vectors)
        return all_vectors

    def _post(self, payload: dict[str, object]) -> Any:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self._http_client is not None:
            return self._post_with_retries(self._http_client, payload, headers)

        with httpx.Client(timeout=self.timeout) as client:
            return self._post_with_retries(client, payload, headers)

    def _post_with_retries(
        self,
        client: Any,
        payload: dict[str, object],
        headers: dict[str, str],
    ) -> Any:
        for attempt in range(self.max_retries + 1):
            response = client.post(
                self.embed_url,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                if attempt >= self.max_retries or status_code not in {429, 500, 502, 503, 504}:
                    raise
                time.sleep(_retry_delay_seconds(exc.response, attempt))
                continue
            return response
        raise RuntimeError("Cohere embedding request failed after retries.")


class OracleLLMEmbeddingProvider:
    """Oracle DBDev LLMs/OpenAI-compatible embedding provider.

    The DBDev LLMs service exposes an OpenAI-compatible embeddings API. The
    provider maps retrieval's document/query input types to the service's
    embedding `input_type` values.
    """

    def __init__(
        self,
        *,
        api_key: str,
        dimensions: int = DEFAULT_ORACLE_LLM_DIMENSIONS,
        input_type: Literal["search_document", "search_query"] = "search_document",
        model: str = DEFAULT_ORACLE_LLM_EMBED_MODEL,
        timeout: float = 30.0,
        max_retries: int = 4,
        batch_size: int = DEFAULT_ORACLE_LLM_BATCH_SIZE,
        max_input_chars: int = DEFAULT_ORACLE_LLM_MAX_INPUT_CHARS,
        embed_url: str | None = None,
        http_client: Any | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must be provided")
        if dimensions < 1:
            raise ValueError("dimensions must be positive")
        if input_type not in {"search_document", "search_query"}:
            raise ValueError("input_type must be search_document or search_query")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if max_input_chars < 1:
            raise ValueError("max_input_chars must be positive")
        self._api_key = api_key
        self.dimensions = dimensions
        self.input_type = input_type
        self.oracle_input_type = _oracle_llm_input_type(input_type)
        self.model = model
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.batch_size = batch_size
        self.max_input_chars = max_input_chars
        self.embed_url = embed_url or _oracle_llm_embed_url_from_env()
        self._http_client = http_client

    def embed_text(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        normalized_texts = [_truncate_embedding_input(str(text), self.max_input_chars) for text in texts]
        if not normalized_texts:
            return []

        all_vectors: list[list[float]] = []
        for batch in _batched(normalized_texts, self.batch_size):
            payload: dict[str, object] = {
                "model": self.model,
                "input": batch,
                "input_type": self.oracle_input_type,
            }
            response = self._post(payload)
            vectors = _openai_embedding_vectors(response.json())
            if len(vectors) != len(batch):
                raise ValueError(f"Oracle LLMs returned {len(vectors)} vectors for {len(batch)} input texts.")
            for vector in vectors:
                if len(vector) != self.dimensions:
                    raise ValueError(
                        f"Oracle LLMs returned {len(vector)} dimensions; expected "
                        f"{self.dimensions}. Set ORACLE_VECTOR_DIMENSIONS to match the "
                        "Oracle embedding model and recreate/re-embed the Oracle VECTOR column."
                    )
            all_vectors.extend(vectors)
        return all_vectors

    def _post(self, payload: dict[str, object]) -> Any:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self._http_client is not None:
            return self._post_with_retries(self._http_client, payload, headers)

        with httpx.Client(timeout=self.timeout) as client:
            return self._post_with_retries(client, payload, headers)

    def _post_with_retries(
        self,
        client: Any,
        payload: dict[str, object],
        headers: dict[str, str],
    ) -> Any:
        for attempt in range(self.max_retries + 1):
            response = client.post(
                self.embed_url,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                if attempt >= self.max_retries or status_code not in {429, 500, 502, 503, 504}:
                    raise
                time.sleep(_retry_delay_seconds(exc.response, attempt))
                continue
            return response
        raise RuntimeError("Oracle LLMs embedding request failed after retries.")


def embedding_provider_from_env(
    *,
    input_type: Literal["search_document", "search_query"] = "search_document",
    default_dimensions: int = 384,
) -> EmbeddingProvider:
    provider = _embedding_provider_choice_from_env()
    cohere_api_key = _cohere_api_key_from_env()
    oracle_api_key = _oracle_llm_api_key_from_env()

    if provider == "oracle" or (provider == "auto" and oracle_api_key):
        dimensions = vector_dimensions_from_env(DEFAULT_ORACLE_LLM_DIMENSIONS)
        if not oracle_api_key:
            raise RuntimeError("Missing required environment variable: ORACLE_LLM_API_KEY")
        return OracleLLMEmbeddingProvider(
            api_key=oracle_api_key,
            dimensions=dimensions,
            input_type=input_type,
            model=os.getenv("ORACLE_LLM_EMBED_MODEL", DEFAULT_ORACLE_LLM_EMBED_MODEL),
            timeout=_float_from_env("ORACLE_LLM_EMBED_TIMEOUT_SECONDS", 30.0),
            max_retries=_int_from_env("ORACLE_LLM_EMBED_MAX_RETRIES", 4),
            batch_size=_int_from_env("ORACLE_LLM_EMBED_BATCH_SIZE", DEFAULT_ORACLE_LLM_BATCH_SIZE),
            max_input_chars=_int_from_env(
                "ORACLE_LLM_EMBED_MAX_CHARS",
                DEFAULT_ORACLE_LLM_MAX_INPUT_CHARS,
            ),
        )

    if provider == "cohere" or (provider == "auto" and cohere_api_key):
        dimensions = vector_dimensions_from_env(default_dimensions)
        if not cohere_api_key:
            raise RuntimeError("Missing required environment variable: CO_API_KEY")
        return CohereEmbeddingProvider(
            api_key=cohere_api_key,
            dimensions=dimensions,
            input_type=input_type,
            model=os.getenv("COHERE_EMBED_MODEL", DEFAULT_COHERE_EMBED_MODEL),
            timeout=_float_from_env("COHERE_EMBED_TIMEOUT_SECONDS", 30.0),
            max_retries=_int_from_env("COHERE_EMBED_MAX_RETRIES", 4),
            batch_size=_int_from_env("COHERE_EMBED_BATCH_SIZE", DEFAULT_COHERE_BATCH_SIZE),
        )
    raise RuntimeError(
        "Missing embedding provider credentials. Set EMBEDDING_PROVIDER=oracle with "
        "ORACLE_LLM_API_KEY, or EMBEDDING_PROVIDER=cohere with CO_API_KEY."
    )


def embedding_provider_name(provider: EmbeddingProvider | None = None) -> str:
    if isinstance(provider, OracleLLMEmbeddingProvider):
        return f"oracle:{provider.model}"
    if isinstance(provider, CohereEmbeddingProvider):
        return f"cohere:{provider.model}"
    choice = _embedding_provider_choice_from_env()
    if choice in {"oracle", "auto"} and _oracle_llm_api_key_from_env():
        return f"oracle:{os.getenv('ORACLE_LLM_EMBED_MODEL', DEFAULT_ORACLE_LLM_EMBED_MODEL)}"
    if choice == "oracle":
        return "oracle:not-configured"
    if _cohere_api_key_from_env():
        return f"cohere:{os.getenv('COHERE_EMBED_MODEL', DEFAULT_COHERE_EMBED_MODEL)}"
    if choice == "cohere":
        return "cohere:not-configured"
    return "embeddings:not-configured"


def _embedding_provider_choice_from_env() -> Literal["auto", "cohere", "oracle"]:
    value = os.getenv("EMBEDDING_PROVIDER", "auto").strip().lower()
    if value in {"", "auto"}:
        return "auto"
    if value == "cohere":
        return "cohere"
    if value in {"oracle", "dbdev", "dbdev_llm", "dbdev_llms"}:
        return "oracle"
    raise ValueError("EMBEDDING_PROVIDER must be one of: auto, cohere, oracle.")


def _cohere_api_key_from_env() -> str | None:
    return os.getenv("CO_API_KEY")


def _oracle_llm_api_key_from_env() -> str | None:
    return os.getenv("ORACLE_LLM_API_KEY") or os.getenv("DBDEV_LLM_API_KEY") or os.getenv("DBDEV_LLMS_API_KEY")


def _oracle_llm_embed_url_from_env() -> str:
    explicit_url = os.getenv("ORACLE_LLM_EMBED_URL")
    if explicit_url:
        return explicit_url
    base_url = os.getenv("ORACLE_LLM_BASE_URL", DEFAULT_ORACLE_LLM_BASE_URL).rstrip("/")
    return f"{base_url}/embeddings"


def _oracle_llm_input_type(
    input_type: Literal["search_document", "search_query"],
) -> Literal["passage", "query"]:
    if input_type == "search_query":
        return "query"
    return "passage"


def _int_from_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if value < 1:
        raise ValueError(f"{name} must be positive.")
    return value


def _float_from_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number.") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive.")
    return value


def _batched(values: Sequence[str], batch_size: int) -> list[list[str]]:
    return [list(values[index : index + batch_size]) for index in range(0, len(values), batch_size)]


def _truncate_embedding_input(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def _retry_delay_seconds(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.5, min(float(retry_after), 30.0))
        except ValueError:
            pass
    return min(2.0 * (attempt + 1), 20.0)


def _cohere_float_embeddings(data: object) -> list[list[float]]:
    if not isinstance(data, dict):
        raise ValueError("Cohere embedding response was not a JSON object.")

    embeddings = data.get("embeddings")
    if isinstance(embeddings, dict):
        values = embeddings.get("float")
        if isinstance(values, list) and (not values or isinstance(values[0], list)):
            return [[float(value) for value in vector] for vector in values]

    if isinstance(embeddings, list) and (not embeddings or isinstance(embeddings[0], list)):
        return [[float(value) for value in vector] for vector in embeddings]

    raise ValueError("Cohere embedding response did not contain float embeddings.")


def _openai_embedding_vectors(data: object) -> list[list[float]]:
    if not isinstance(data, dict):
        raise ValueError("Oracle LLMs embedding response was not a JSON object.")

    items = data.get("data")
    if not isinstance(items, list):
        raise ValueError("Oracle LLMs embedding response did not contain a data list.")

    vectors: list[list[float]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Oracle LLMs embedding response contained a non-object item.")
        embedding = item.get("embedding")
        if not isinstance(embedding, list):
            raise ValueError("Oracle LLMs embedding response item did not contain an embedding.")
        vectors.append([float(value) for value in embedding])
    return vectors
