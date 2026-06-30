from __future__ import annotations

import os
from uuid import uuid4
from pathlib import Path

import pytest

from reducto.lib.oracledb.qa import answer_from_search_results
from reducto.lib.oracledb.config import connect_oracle, vector_dimensions_from_env
from reducto.lib.oracledb.models import SearchFilters, DocumentMetadata
from reducto.lib.oracledb.oracle import OracleSchemaManager, OracleDocumentRepository
from reducto.lib.oracledb.retrieval import OracleHybridRetriever
from reducto.lib.oracledb.embeddings import embedding_provider_from_env
from reducto.lib.oracledb.reducto_client import ReductoDocumentParser

EXAMPLE_ROOT = Path(__file__).resolve().parents[2] / "examples" / "oracledb"


def load_env(path: Path = EXAMPLE_ROOT / ".env") -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip("'\"")


load_env()


pytestmark = pytest.mark.integration


def test_real_reducto_oracle_embeddings_e2e(tmp_path: Path) -> None:
    if os.getenv("RUN_E2E_INTEGRATION") != "1":
        pytest.skip("Set RUN_E2E_INTEGRATION=1 to run live Reducto/Oracle/embedding E2E tests.")

    required_env = (
        "ORACLE_USER",
        "ORACLE_PASSWORD",
        "ORACLE_DSN",
        "REDUCTO_API_KEY",
    )
    missing = [name for name in required_env if not os.getenv(name)]
    if missing:
        pytest.skip(f"Missing live integration environment variables: {', '.join(missing)}")
    try:
        document_embedding_provider = embedding_provider_from_env(input_type="search_document")
        query_embedding_provider = embedding_provider_from_env(input_type="search_query")
    except RuntimeError as exc:
        pytest.skip(str(exc))

    marker = uuid4().hex[:10].upper()
    company = f"E2E_{marker}"
    source_path = tmp_path / f"reducto-oracledb-e2e-{marker}.html"
    source_path.write_text(
        """
        <!doctype html>
        <html>
          <body>
            <h1>Reducto OracleDB E2E Test Record</h1>
            <p>Patient: Alex Test.</p>
            <p>
              Medication: Metformin is used to treat type 2 diabetes and control blood sugar.
            </p>
            <p>
              Precaution: Monitor kidney function because metformin can rarely cause lactic
              acidosis.
            </p>
            <table>
              <tr><th>Measure</th><th>Value</th></tr>
              <tr><td>A1C</td><td>7.2%</td></tr>
            </table>
          </body>
        </html>
        """,
        encoding="utf-8",
    )

    connection = connect_oracle()
    document_id: int | None = None
    try:
        OracleSchemaManager(connection).create_schema(vector_dimensions=vector_dimensions_from_env())
        parse_result = ReductoDocumentParser().parse_file(source_path)
        assert parse_result.chunks, "Reducto returned no chunks for the E2E fixture."

        document_id = OracleDocumentRepository(connection).store_parse_result(
            DocumentMetadata(
                source_uri=str(source_path),
                source_kind="file",
                company=company,
                fiscal_year=2026,
                filing_type="E2E",
                title="Reducto OracleDB live E2E fixture",
            ),
            parse_result,
            document_embedding_provider,
        )

        retriever = OracleHybridRetriever(
            connection,
            query_embedding_provider,
        )
        results = retriever.semantic_search(
            "What medication treats type 2 diabetes and what precaution is mentioned?",
            filters=SearchFilters(company=company, fiscal_year=2026, filing_type="E2E"),
            limit=3,
        )
        assert results, "Oracle vector search returned no results for the stored E2E document."

        evidence_text = " ".join(result.content for result in results).lower()
        assert "metformin" in evidence_text
        assert "type 2 diabetes" in evidence_text
        assert "lactic acidosis" in evidence_text or "kidney" in evidence_text

        answer = answer_from_search_results(
            "What medication treats type 2 diabetes and what precaution is mentioned?",
            results,
            evidence_limit=3,
        )
        assert answer.evidence
        assert any(item.source_uri == str(source_path) for item in answer.evidence)
    finally:
        if document_id is not None:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM documents WHERE document_id = :document_id",
                    [document_id],
                )
            connection.commit()
        connection.close()
