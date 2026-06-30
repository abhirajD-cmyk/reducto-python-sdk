from __future__ import annotations

import os
from pathlib import Path

import pytest

from examples.oracledb.demo.app import (
    load_env,
    ingest_url,
    extract_url,
    _safe_filename,
    parse_multipart_form,
    embedding_status_payload,
    _extract_schema_from_payload,
)
from reducto.lib.oracledb.models import (
    NormalizedParseResult,
    NormalizedExtractResult,
)


def test_load_env_sets_missing_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        """
        # comment
        DEMO_TEST_VALUE="from-file"
        DEMO_EXISTING_VALUE=from-file
        """,
        encoding="utf-8",
    )
    monkeypatch.delenv("DEMO_TEST_VALUE", raising=False)
    monkeypatch.setenv("DEMO_EXISTING_VALUE", "from-env")

    load_env(env_file)

    assert os.environ["DEMO_TEST_VALUE"] == "from-file"
    assert os.environ["DEMO_EXISTING_VALUE"] == "from-env"


def test_embedding_status_performs_live_dimension_check(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Provider:
        dimensions = 3

        def embed_text(self, text: str) -> list[float]:
            assert text == "Oracle Database vector readiness check"
            return [0.1, 0.2, 0.3]

    monkeypatch.setattr(
        "examples.oracledb.demo.app.embedding_provider_from_env",
        lambda **_kwargs: _Provider(),
    )
    monkeypatch.setattr(
        "examples.oracledb.demo.app.embedding_provider_name",
        lambda _provider=None: "oracle:test-model",
    )

    status = embedding_status_payload()

    assert status["connected"] is True
    assert status["provider"] == "oracle:test-model"
    assert status["dimensions"] == 3
    assert status["latency_ms"] >= 0


def test_embedding_status_rejects_wrong_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Provider:
        dimensions = 3

        def embed_text(self, _text: str) -> list[float]:
            return [0.1, 0.2]

    monkeypatch.setattr(
        "examples.oracledb.demo.app.embedding_provider_from_env",
        lambda **_kwargs: _Provider(),
    )
    monkeypatch.setattr(
        "examples.oracledb.demo.app.embedding_provider_name",
        lambda _provider=None: "oracle:test-model",
    )

    status = embedding_status_payload()

    assert status["connected"] is False
    assert status["dimensions"] is None
    assert "returned 2 dimensions; expected 3" in status["error"]


def test_parse_multipart_form_extracts_fields_and_file() -> None:
    boundary = "----demo-boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="company"\r\n\r\n'
        "AAPL\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="filing.html"\r\n'
        "Content-Type: text/html\r\n\r\n"
        "<html>hello</html>\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    form = parse_multipart_form(f"multipart/form-data; boundary={boundary}", body)

    assert form.fields["company"] == "AAPL"
    assert form.files["file"].filename == "filing.html"
    assert form.files["file"].content_type == "text/html"
    assert form.files["file"].content == b"<html>hello</html>"


def test_safe_filename_removes_paths_and_special_chars() -> None:
    assert _safe_filename("../../Apple Filing (2023).html") == "Apple-Filing-2023-.html"


def test_extract_schema_from_payload_accepts_json_string() -> None:
    schema = _extract_schema_from_payload(
        {
            "schema": """
            {
              "type": "object",
              "properties": {
                "company_name": { "type": "string" }
              }
            }
            """
        }
    )

    assert schema["type"] == "object"
    assert "company_name" in schema["properties"]


def test_ingest_url_returns_latency_breakdown(monkeypatch: pytest.MonkeyPatch) -> None:
    parse_result = NormalizedParseResult(
        job_id="job_123",
        raw_response={},
        chunks=[],
        blocks=[],
        tables=[],
        financial_facts=[],
        duration_seconds=1.25,
    )

    class _StubParser:
        def parse_url(self, *_args: object, **_kwargs: object) -> NormalizedParseResult:
            return parse_result

    def store_parse_result(*_args: object) -> int:
        return 99

    monkeypatch.setattr("examples.oracledb.demo.app.ReductoDocumentParser", _StubParser)
    monkeypatch.setattr("examples.oracledb.demo.app._store_parse_result", store_parse_result)

    result = ingest_url(
        {
            "url": "https://example.test/doc.pdf",
            "company": "ACME",
            "year": "2023",
            "filing_type": "Annual Report",
        }
    )

    assert result["document_id"] == 99
    assert result["latency"]["total_ms"] >= 0
    assert result["latency"]["parse_ms"] >= 0
    assert result["latency"]["store_ms"] >= 0
    assert result["latency"]["reducto_ms"] == 1250.0


def test_extract_url_returns_extract_api_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    extract_result = NormalizedExtractResult(
        job_id="job_extract_123",
        raw_response={},
        schema_json={"type": "object", "properties": {"company_name": {"type": "string"}}},
        extracted_json={"company_name": {"value": "ACME", "citations": []}},
        request_body={
            "input": "https://example.test/doc.pdf",
            "instructions": {
                "schema": {
                    "type": "object",
                    "properties": {"company_name": {"type": "string"}},
                }
            },
            "parsing": {"settings": {"return_ocr_data": False}},
            "settings": {
                "citations": {"enabled": True, "numerical_confidence": True},
                "deep_extract": False,
            },
        },
        citations_enabled=True,
        duration_seconds=2.5,
        studio_link="https://studio.reducto.ai/job/job_extract_123",
    )

    class _StubParser:
        def extract_url(self, *_args: object, **_kwargs: object) -> NormalizedExtractResult:
            return extract_result

    def store_extract_result(*_args: object) -> tuple[int, int]:
        return 101, 202

    monkeypatch.setattr("examples.oracledb.demo.app.ReductoDocumentParser", _StubParser)
    monkeypatch.setattr("examples.oracledb.demo.app._store_extract_result", store_extract_result)

    result = extract_url(
        {
            "url": "https://example.test/doc.pdf",
            "company": "ACME",
            "year": "2023",
            "schema": {
                "type": "object",
                "properties": {"company_name": {"type": "string"}},
            },
        }
    )

    assert result["route"] == "/api/extract/url"
    assert result["backend_api"] == "Reducto Extract API"
    assert result["reducto_endpoint"] == "/extract"
    assert result["sdk_call"] == "client.extract.run"
    assert result["document_id"] == 101
    assert result["extraction_id"] == 202
    assert result["request_body"]["instructions"]["schema"]["type"] == "object"
    assert result["request_body"]["parsing"]["settings"]["return_ocr_data"] is False
    assert result["request_body"]["settings"]["citations"]["enabled"] is True
    assert result["extracted_json"]["company_name"]["value"] == "ACME"
    assert result["latency"]["reducto_ms"] == 2500.0
