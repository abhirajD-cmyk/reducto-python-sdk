from __future__ import annotations

from typing import Any

import pytest

from reducto.lib.oracledb.models import JsonValue, DocumentMetadata, NormalizedExtractResult
from reducto.lib.oracledb.oracle import OracleSchemaManager, OracleDocumentRepository
from reducto.lib.oracledb.embeddings import CohereEmbeddingProvider
from reducto.lib.oracledb.normalizer import normalize_parse_response


class _StubVar:
    def __init__(self) -> None:
        self.value: int | None = None

    def setvalue(self, value: int) -> None:
        self.value = value

    def getvalue(self) -> list[int]:
        assert self.value is not None
        return [self.value]


class _StubCursor:
    def __init__(self, connection: _StubConnection) -> None:
        self.connection = connection

    def __enter__(self) -> _StubCursor:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def var(self, oracle_type: object) -> _StubVar:
        del oracle_type
        return _StubVar()

    def execute(self, sql: str, **kwargs: Any) -> None:
        self.connection.executed.append((sql, kwargs))
        returning_key = _returning_key(sql)
        if returning_key:
            self.connection.next_id += 1
            kwargs[returning_key].setvalue(self.connection.next_id)

    def executemany(self, sql: str, rows: list[dict[str, Any]]) -> None:
        self.connection.executemany_calls.append((sql, rows))


class _StubConnection:
    def __init__(self) -> None:
        self.next_id = 0
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.executemany_calls: list[tuple[str, list[dict[str, Any]]]] = []
        self.committed = False

    def cursor(self) -> _StubCursor:
        return _StubCursor(self)

    def commit(self) -> None:
        self.committed = True


class _CohereResponse:
    def json(self) -> dict[str, object]:
        return {"embeddings": {"float": [[0.1 for _ in range(8)]]}}

    def raise_for_status(self) -> None:
        return None


class _CohereClient:
    def post(self, _url: str, **_kwargs: object) -> _CohereResponse:
        return _CohereResponse()


class _SchemaCursor:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.row = row

    def __enter__(self) -> _SchemaCursor:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def execute(self, _sql: str, **_kwargs: object) -> None:
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        return self.row


class _SchemaConnection:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.row = row

    def cursor(self) -> _SchemaCursor:
        return _SchemaCursor(self.row)


def test_schema_manager_accepts_matching_vector_dimensions() -> None:
    manager = OracleSchemaManager(_SchemaConnection(("VECTOR", "VECTOR(2048,FLOAT32,DENSE)")))

    manager._validate_vector_column_dimensions("DOCUMENT_CHUNKS", "EMBEDDING", 2048)


def test_schema_manager_rejects_mismatched_vector_dimensions() -> None:
    manager = OracleSchemaManager(_SchemaConnection(("VECTOR", "VECTOR(384,FLOAT32,DENSE)")))

    with pytest.raises(RuntimeError, match=r"VECTOR\(384\).+requires VECTOR\(2048\)"):
        manager._validate_vector_column_dimensions("DOCUMENT_CHUNKS", "EMBEDDING", 2048)


def test_store_parse_result_inserts_document_chunks_tables_and_facts() -> None:
    parse_result = normalize_parse_response(
        {
            "job_id": "job_store",
            "result": {
                "type": "full",
                "chunks": [
                    {
                        "content": "Revenue table",
                        "embed": "revenue table",
                        "blocks": [
                            {
                                "type": "Table",
                                "content": "Metric,2023\nRevenue,$10",
                                "bbox": {"page": 7},
                            }
                        ],
                    }
                ],
            },
        }
    )
    connection = _StubConnection()
    repository = OracleDocumentRepository(connection)

    document_id = repository.store_parse_result(
        DocumentMetadata(
            source_uri="https://example.test/10k.pdf",
            company="ACME",
            fiscal_year=2023,
            filing_type="10-K",
        ),
        parse_result,
        CohereEmbeddingProvider(
            api_key="test-key",
            dimensions=8,
            http_client=_CohereClient(),
        ),
    )

    assert document_id == 1
    assert connection.committed
    assert any("INSERT INTO documents" in sql for sql, _ in connection.executed)
    assert any("INSERT INTO document_chunks" in sql for sql, _ in connection.executed)
    assert any("INSERT INTO parsed_tables" in sql for sql, _ in connection.executed)
    assert len(connection.executemany_calls) == 1
    fact_rows = connection.executemany_calls[0][1]
    assert fact_rows[0]["metric"] == "Revenue"
    assert fact_rows[0]["page_number"] == 7


def test_store_extract_result_inserts_document_and_extraction_json() -> None:
    schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {
            "company_name": {"type": "string"},
            "total_revenue": {"type": "number"},
        },
    }
    extract_result = NormalizedExtractResult(
        job_id="job_extract_store",
        raw_response={
            "job_id": "job_extract_store",
            "result": {
                "company_name": {"value": "ACME", "citations": []},
                "total_revenue": {"value": 10, "citations": []},
            },
        },
        schema_json=schema,
        extracted_json={
            "company_name": {"value": "ACME", "citations": []},
            "total_revenue": {"value": 10, "citations": []},
        },
        citations_enabled=True,
        studio_link="https://studio.reducto.ai/job_extract_store",
    )
    connection = _StubConnection()
    repository = OracleDocumentRepository(connection)

    document_id, extraction_id = repository.store_extract_result(
        DocumentMetadata(
            source_uri="https://example.test/10k.pdf",
            company="ACME",
            fiscal_year=2023,
            filing_type="10-K",
        ),
        extract_result,
    )

    assert document_id == 1
    assert extraction_id == 2
    assert connection.committed
    extraction_inserts = [kwargs for sql, kwargs in connection.executed if "INSERT INTO document_extractions" in sql]
    assert len(extraction_inserts) == 1
    assert '"company_name"' in extraction_inserts[0]["schema_json"]
    assert '"total_revenue"' in extraction_inserts[0]["extracted_json"]
    assert extraction_inserts[0]["citations_enabled"] == 1


def _returning_key(sql: str) -> str | None:
    if "RETURNING document_id" in sql:
        return "document_id"
    if "RETURNING extraction_id" in sql:
        return "extraction_id"
    if "RETURNING chunk_id" in sql:
        return "chunk_id"
    if "RETURNING table_id" in sql:
        return "table_id"
    return None
