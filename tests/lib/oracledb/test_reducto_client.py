from __future__ import annotations

import httpx
import pytest

from reducto.lib.oracledb.models import JsonValue
from reducto.lib.oracledb.reducto_client import ReductoDocumentParser


class _StubParse:
    def run(self, **kwargs: object) -> dict[str, object]:
        assert kwargs["input"] == "https://example.test/10k.pdf"
        return {
            "job_id": "job_large",
            "duration": 2.0,
            "result": {
                "type": "url",
                "result_id": "result_1",
                "url": "https://example.test/result.json",
            },
            "usage": {},
        }


class _StubClient:
    parse = _StubParse()


class _StubExtract:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] | None = None

    def run(self, **kwargs: object) -> dict[str, object]:
        self.kwargs = kwargs
        assert kwargs["input"] == "https://example.test/10k.pdf"
        return {
            "job_id": "job_extract",
            "studio_link": "https://studio.reducto.ai/job_extract",
            "result": {
                "company_name": {
                    "value": "ACME",
                    "citations": [{"page": 1, "text": "ACME annual report"}],
                },
                "fiscal_year": {
                    "value": 2023,
                    "citations": [{"page": 1, "text": "fiscal 2023"}],
                },
            },
            "usage": {"num_fields": 2, "num_pages": 5},
        }


class _StubExtractClient:
    def __init__(self) -> None:
        self.extract = _StubExtract()


class _StubFallbackParse:
    def __init__(self) -> None:
        self.inputs: list[object] = []

    def run(self, **kwargs: object) -> dict[str, object]:
        self.inputs.append(kwargs["input"])
        if len(self.inputs) == 1:
            raise RuntimeError(
                "Error code: 400 - {'error': {'name': 'INVALID_CONFIG', "
                "'message': \"Invalid configuration for 'document_url': "
                'Failed to download file from URL - received HTTP 403 response"}}'
            )

        assert kwargs["input"] == "uploaded:file"
        return {
            "job_id": "job_uploaded",
            "duration": 1.0,
            "result": {
                "type": "full",
                "chunks": [
                    {
                        "content": "uploaded filing",
                        "embed": "uploaded filing",
                        "blocks": [],
                    }
                ],
            },
            "usage": {},
        }


class _StubFallbackClient:
    def __init__(self) -> None:
        self.parse = _StubFallbackParse()
        self.uploaded_extension: str | None = None
        self.uploaded_content: bytes | None = None

    def upload(self, *, file: object, extension: str | None) -> str:
        self.uploaded_extension = extension
        self.uploaded_content = PathLikeBytes(file)
        return "uploaded:file"


def test_parse_url_fetches_large_result_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.test/result.json"
        return httpx.Response(
            200,
            json={
                "type": "full",
                "chunks": [
                    {
                        "content": "hello",
                        "embed": "hello",
                        "blocks": [],
                    }
                ],
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    parser = ReductoDocumentParser(client=_StubClient(), http_client=http_client)

    normalized = parser.parse_url("https://example.test/10k.pdf", force_url_result=True)

    assert normalized.job_id == "job_large"
    assert len(normalized.chunks) == 1
    assert normalized.chunks[0].content == "hello"


def test_extract_url_passes_schema_and_citation_settings() -> None:
    schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {
            "company_name": {"type": "string"},
            "fiscal_year": {"type": "integer"},
        },
    }
    client = _StubExtractClient()
    parser = ReductoDocumentParser(client=client)

    normalized = parser.extract_url(
        "https://example.test/10k.pdf",
        schema=schema,
        system_prompt="Extract audited annual filing fields.",
        deep_extract=True,
    )

    assert client.extract.kwargs is not None
    assert client.extract.kwargs["instructions"] == {
        "schema": schema,
        "system_prompt": "Extract audited annual filing fields.",
    }
    assert client.extract.kwargs["settings"] == {
        "citations": {"enabled": True, "numerical_confidence": True},
        "deep_extract": True,
    }
    assert normalized.job_id == "job_extract"
    assert normalized.schema_json == schema
    assert normalized.request_body["input"] == "https://example.test/10k.pdf"
    assert normalized.request_body["instructions"] == {
        "schema": schema,
        "system_prompt": "Extract audited annual filing fields.",
    }
    assert normalized.request_body["settings"] == {
        "citations": {"enabled": True, "numerical_confidence": True},
        "deep_extract": True,
    }
    parsing = normalized.request_body["parsing"]
    assert isinstance(parsing, dict)
    parsing_settings = parsing["settings"]
    assert isinstance(parsing_settings, dict)
    assert parsing_settings["return_ocr_data"] is False
    assert isinstance(normalized.extracted_json, dict)
    company_name = normalized.extracted_json["company_name"]
    assert isinstance(company_name, dict)
    assert company_name["value"] == "ACME"


def test_parse_url_downloads_and_uploads_when_source_url_is_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", "reducto-oracledb tests@example.test")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://www.sec.gov/example/aapl-20230930.htm"
        assert request.headers["User-Agent"] == "reducto-oracledb tests@example.test"
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html>filing</html>",
        )

    client = _StubFallbackClient()
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    parser = ReductoDocumentParser(client=client, http_client=http_client)

    normalized = parser.parse_url("https://www.sec.gov/example/aapl-20230930.htm")

    assert normalized.job_id == "job_uploaded"
    assert normalized.chunks[0].content == "uploaded filing"
    assert client.uploaded_extension == "html"
    assert client.uploaded_content == b"<html>filing</html>"


def PathLikeBytes(value: object) -> bytes:
    path = str(value)
    with open(path, "rb") as file:
        return file.read()
