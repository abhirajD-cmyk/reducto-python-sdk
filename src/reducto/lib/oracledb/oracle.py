from __future__ import annotations

import array
from typing import Any
from decimal import Decimal
from collections.abc import Sequence

from .utils import json_dumps, to_plain_data
from .models import DocumentMetadata, NormalizedParseResult, NormalizedExtractResult
from .embeddings import EmbeddingProvider, embed_many

DOCUMENTS_TABLE = "DOCUMENTS"
EXTRACTIONS_TABLE = "DOCUMENT_EXTRACTIONS"
CHUNKS_TABLE = "DOCUMENT_CHUNKS"
TABLES_TABLE = "PARSED_TABLES"
FACTS_TABLE = "FINANCIAL_FACTS"


class OracleSchemaManager:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def create_schema(self, *, vector_dimensions: int = 384, create_text_index: bool = False) -> None:
        if vector_dimensions < 1:
            raise ValueError("vector_dimensions must be positive")

        self._create_table_if_missing(DOCUMENTS_TABLE, _documents_ddl())
        self._create_table_if_missing(EXTRACTIONS_TABLE, _extractions_ddl())
        self._create_table_if_missing(CHUNKS_TABLE, _chunks_ddl(vector_dimensions))
        self._create_table_if_missing(TABLES_TABLE, _tables_ddl())
        self._create_table_if_missing(FACTS_TABLE, _facts_ddl())
        self._resize_varchar_column_if_smaller(FACTS_TABLE, "RAW_VALUE", 4000)
        self._create_index_if_missing(
            "DOCUMENTS_COMPANY_YEAR_IDX",
            ("CREATE INDEX documents_company_year_idx ON documents(company, fiscal_year, filing_type)"),
        )
        self._create_index_if_missing(
            "CHUNKS_DOC_PAGE_IDX",
            ("CREATE INDEX chunks_doc_page_idx ON document_chunks(document_id, page_start, page_end)"),
        )
        self._create_index_if_missing(
            "FACTS_DOC_METRIC_IDX",
            "CREATE INDEX facts_doc_metric_idx ON financial_facts(document_id, metric)",
        )
        self._create_index_if_missing(
            "EXTRACTIONS_DOC_IDX",
            "CREATE INDEX extractions_doc_idx ON document_extractions(document_id)",
        )
        self._create_index_if_missing(
            "EXTRACTIONS_JOB_IDX",
            "CREATE INDEX extractions_job_idx ON document_extractions(reducto_job_id)",
        )
        if create_text_index:
            self._create_index_if_missing(
                "DOCUMENT_CHUNKS_TEXT_IDX",
                ("CREATE INDEX document_chunks_text_idx ON document_chunks(content) INDEXTYPE IS CTXSYS.CONTEXT"),
            )
        self.connection.commit()

    def _create_table_if_missing(self, table_name: str, ddl: str) -> None:
        if self._object_exists("USER_TABLES", "TABLE_NAME", table_name):
            return
        with self.connection.cursor() as cursor:
            cursor.execute(ddl)

    def _create_index_if_missing(self, index_name: str, ddl: str) -> None:
        if self._object_exists("USER_INDEXES", "INDEX_NAME", index_name):
            return
        with self.connection.cursor() as cursor:
            cursor.execute(ddl)

    def _object_exists(self, view_name: str, column_name: str, object_name: str) -> bool:
        sql = f"SELECT COUNT(*) FROM {view_name} WHERE {column_name} = :object_name"
        with self.connection.cursor() as cursor:
            cursor.execute(sql, object_name=object_name.upper())
            row = cursor.fetchone()
        return bool(row and row[0])

    def _resize_varchar_column_if_smaller(self, table_name: str, column_name: str, minimum_length: int) -> None:
        sql = """
            SELECT data_type, data_length
            FROM user_tab_columns
            WHERE table_name = :table_name
              AND column_name = :column_name
        """
        with self.connection.cursor() as cursor:
            cursor.execute(sql, table_name=table_name.upper(), column_name=column_name.upper())
            row = cursor.fetchone()
            if not row:
                return
            data_type, data_length = row
            if str(data_type).upper() != "VARCHAR2" or int(data_length) >= minimum_length:
                return
            cursor.execute(f"ALTER TABLE {table_name} MODIFY {column_name} VARCHAR2({minimum_length})")


class OracleDocumentRepository:
    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self._oracledb = _load_oracledb()

    def store_parse_result(
        self,
        metadata: DocumentMetadata,
        parse_result: NormalizedParseResult,
        embedding_provider: EmbeddingProvider,
    ) -> int:
        with self.connection.cursor() as cursor:
            document_id = self._insert_document(
                cursor,
                metadata,
                reducto_job_id=parse_result.job_id,
                pdf_url=parse_result.pdf_url,
                studio_link=parse_result.studio_link,
                raw_reducto_output=parse_result.raw_response,
            )
            chunk_ids = self._insert_chunks(cursor, document_id, parse_result, embedding_provider)
            table_ids = self._insert_tables(cursor, document_id, parse_result, chunk_ids)
            self._insert_facts(cursor, document_id, parse_result, chunk_ids, table_ids)
        self.connection.commit()
        return document_id

    def store_extract_result(
        self,
        metadata: DocumentMetadata,
        extract_result: NormalizedExtractResult,
    ) -> tuple[int, int]:
        with self.connection.cursor() as cursor:
            document_id = self._insert_document(
                cursor,
                metadata,
                reducto_job_id=extract_result.job_id,
                pdf_url=None,
                studio_link=extract_result.studio_link,
                raw_reducto_output=extract_result.raw_response,
            )
            extraction_id = self._insert_extraction(cursor, document_id, extract_result)
        self.connection.commit()
        return document_id, extraction_id

    def _insert_document(
        self,
        cursor: Any,
        metadata: DocumentMetadata,
        *,
        reducto_job_id: str | None,
        pdf_url: str | None,
        studio_link: str | None,
        raw_reducto_output: dict[str, Any],
    ) -> int:
        document_id_var = cursor.var(self._oracledb.NUMBER)
        _set_clob_inputs(cursor, self._oracledb, "raw_reducto_output")
        cursor.execute(
            """
            INSERT INTO documents (
                company, fiscal_year, filing_type, source_uri, source_kind, title,
                reducto_job_id, pdf_url, studio_link, raw_reducto_output
            )
            VALUES (
                :company, :fiscal_year, :filing_type, :source_uri, :source_kind, :title,
                :reducto_job_id, :pdf_url, :studio_link, :raw_reducto_output
            )
            RETURNING document_id INTO :document_id
            """,
            company=metadata.company,
            fiscal_year=metadata.fiscal_year,
            filing_type=metadata.filing_type,
            source_uri=metadata.source_uri,
            source_kind=metadata.source_kind,
            title=metadata.title,
            reducto_job_id=reducto_job_id,
            pdf_url=pdf_url,
            studio_link=studio_link,
            raw_reducto_output=json_dumps(raw_reducto_output),
            document_id=document_id_var,
        )
        return _returned_number(document_id_var)

    def _insert_extraction(
        self,
        cursor: Any,
        document_id: int,
        extract_result: NormalizedExtractResult,
    ) -> int:
        extraction_id_var = cursor.var(self._oracledb.NUMBER)
        _set_clob_inputs(
            cursor,
            self._oracledb,
            "schema_json",
            "extracted_json",
            "raw_reducto_output",
        )
        cursor.execute(
            """
            INSERT INTO document_extractions (
                document_id, reducto_job_id, schema_json, extracted_json,
                raw_reducto_output, citations_enabled, studio_link
            )
            VALUES (
                :document_id, :reducto_job_id, :schema_json, :extracted_json,
                :raw_reducto_output, :citations_enabled, :studio_link
            )
            RETURNING extraction_id INTO :extraction_id
            """,
            document_id=document_id,
            reducto_job_id=extract_result.job_id,
            schema_json=json_dumps(extract_result.schema_json),
            extracted_json=json_dumps(extract_result.extracted_json),
            raw_reducto_output=json_dumps(extract_result.raw_response),
            citations_enabled=1 if extract_result.citations_enabled else 0,
            studio_link=extract_result.studio_link,
            extraction_id=extraction_id_var,
        )
        return _returned_number(extraction_id_var)

    def _insert_chunks(
        self,
        cursor: Any,
        document_id: int,
        parse_result: NormalizedParseResult,
        embedding_provider: EmbeddingProvider,
    ) -> dict[int, int]:
        chunk_ids: dict[int, int] = {}
        embedding_texts = [chunk.embedding_text or chunk.content for chunk in parse_result.chunks]
        vectors = embed_many(embedding_provider, embedding_texts)
        if len(parse_result.chunks) != len(vectors):
            raise ValueError("Embedding vector count must match the parse result chunk count.")
        for chunk, vector in zip(parse_result.chunks, vectors):
            _validate_vector(vector, embedding_provider.dimensions)
            chunk_id_var = cursor.var(self._oracledb.NUMBER)
            _set_clob_inputs(cursor, self._oracledb, "content", "embedding_text", "block_metadata")
            cursor.execute(
                """
                INSERT INTO document_chunks (
                    document_id, chunk_index, content, embedding_text, page_start,
                    page_end, block_count, block_metadata, embedding
                )
                VALUES (
                    :document_id, :chunk_index, :content, :embedding_text, :page_start,
                    :page_end, :block_count, :block_metadata, :embedding
                )
                RETURNING chunk_id INTO :chunk_id
                """,
                document_id=document_id,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                embedding_text=chunk.embedding_text,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                block_count=chunk.block_count,
                block_metadata=json_dumps(chunk.metadata),
                embedding=_to_vector(vector),
                chunk_id=chunk_id_var,
            )
            chunk_ids[chunk.chunk_index] = _returned_number(chunk_id_var)
        return chunk_ids

    def _insert_tables(
        self,
        cursor: Any,
        document_id: int,
        parse_result: NormalizedParseResult,
        chunk_ids: dict[int, int],
    ) -> dict[int, int]:
        table_ids: dict[int, int] = {}
        for table in parse_result.tables:
            table_id_var = cursor.var(self._oracledb.NUMBER)
            _set_clob_inputs(cursor, self._oracledb, "raw_content", "rows_json", "metadata")
            cursor.execute(
                """
                INSERT INTO parsed_tables (
                    document_id, chunk_id, table_index, chunk_index, block_index,
                    page_number, raw_content, rows_json, metadata
                )
                VALUES (
                    :document_id, :chunk_id, :table_index, :chunk_index, :block_index,
                    :page_number, :raw_content, :rows_json, :metadata
                )
                RETURNING table_id INTO :table_id
                """,
                document_id=document_id,
                chunk_id=chunk_ids.get(table.chunk_index),
                table_index=table.table_index,
                chunk_index=table.chunk_index,
                block_index=table.block_index,
                page_number=table.page_number,
                raw_content=table.content,
                rows_json=json_dumps(to_plain_data(table.rows)),
                metadata=json_dumps(table.metadata),
                table_id=table_id_var,
            )
            table_ids[table.table_index] = _returned_number(table_id_var)
        return table_ids

    def _insert_facts(
        self,
        cursor: Any,
        document_id: int,
        parse_result: NormalizedParseResult,
        chunk_ids: dict[int, int],
        table_ids: dict[int, int],
    ) -> None:
        rows = []
        table_by_index = {table.table_index: table for table in parse_result.tables}
        for fact in parse_result.financial_facts:
            table = table_by_index.get(fact.table_index)
            rows.append(
                {
                    "document_id": document_id,
                    "table_id": table_ids.get(fact.table_index),
                    "source_chunk_id": chunk_ids.get(table.chunk_index) if table else None,
                    "metric": fact.metric,
                    "period_label": fact.period_label,
                    "value": fact.value,
                    "raw_value": fact.raw_value,
                    "unit": fact.unit,
                    "currency": fact.currency,
                    "scale": fact.scale,
                    "row_index": fact.row_index,
                    "column_index": fact.column_index,
                    "page_number": fact.page_number,
                    "raw_row": json_dumps(to_plain_data(fact.raw_row)),
                }
            )

        if not rows:
            return
        _set_clob_inputs(cursor, self._oracledb, "raw_row")
        cursor.executemany(
            """
            INSERT INTO financial_facts (
                document_id, table_id, source_chunk_id, metric, period_label,
                value, raw_value, unit, currency, scale, row_index, column_index,
                page_number, raw_row
            )
            VALUES (
                :document_id, :table_id, :source_chunk_id, :metric, :period_label,
                :value, :raw_value, :unit, :currency, :scale, :row_index,
                :column_index, :page_number, :raw_row
            )
            """,
            rows,
        )


def _documents_ddl() -> str:
    return """
    CREATE TABLE documents (
        document_id NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        company VARCHAR2(255),
        fiscal_year NUMBER(4),
        filing_type VARCHAR2(30),
        source_uri VARCHAR2(2048),
        source_kind VARCHAR2(30),
        title VARCHAR2(500),
        reducto_job_id VARCHAR2(128),
        pdf_url VARCHAR2(2048),
        studio_link VARCHAR2(2048),
        raw_reducto_output JSON,
        created_at TIMESTAMP DEFAULT SYSTIMESTAMP,
        CONSTRAINT documents_reducto_job_uk UNIQUE (reducto_job_id)
    )
    """


def _extractions_ddl() -> str:
    return """
    CREATE TABLE document_extractions (
        extraction_id NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        document_id NUMBER NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
        reducto_job_id VARCHAR2(128),
        schema_json JSON,
        extracted_json JSON,
        raw_reducto_output JSON,
        citations_enabled NUMBER(1) DEFAULT 1 NOT NULL,
        studio_link VARCHAR2(2048),
        created_at TIMESTAMP DEFAULT SYSTIMESTAMP
    )
    """


def _chunks_ddl(vector_dimensions: int) -> str:
    return f"""
    CREATE TABLE document_chunks (
        chunk_id NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        document_id NUMBER NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
        chunk_index NUMBER NOT NULL,
        content CLOB,
        embedding_text CLOB,
        page_start NUMBER,
        page_end NUMBER,
        block_count NUMBER,
        block_metadata JSON,
        embedding VECTOR({vector_dimensions}, FLOAT32),
        created_at TIMESTAMP DEFAULT SYSTIMESTAMP,
        CONSTRAINT document_chunks_doc_idx_uk UNIQUE (document_id, chunk_index)
    )
    """


def _tables_ddl() -> str:
    return """
    CREATE TABLE parsed_tables (
        table_id NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        document_id NUMBER NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
        chunk_id NUMBER REFERENCES document_chunks(chunk_id) ON DELETE SET NULL,
        table_index NUMBER NOT NULL,
        chunk_index NUMBER NOT NULL,
        block_index NUMBER NOT NULL,
        page_number NUMBER,
        raw_content CLOB,
        rows_json JSON,
        metadata JSON,
        created_at TIMESTAMP DEFAULT SYSTIMESTAMP,
        CONSTRAINT parsed_tables_doc_idx_uk UNIQUE (document_id, table_index)
    )
    """


def _facts_ddl() -> str:
    return """
    CREATE TABLE financial_facts (
        fact_id NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        document_id NUMBER NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
        table_id NUMBER REFERENCES parsed_tables(table_id) ON DELETE SET NULL,
        source_chunk_id NUMBER REFERENCES document_chunks(chunk_id) ON DELETE SET NULL,
        metric VARCHAR2(500) NOT NULL,
        period_label VARCHAR2(255),
        value NUMBER,
        raw_value VARCHAR2(4000),
        unit VARCHAR2(50),
        currency VARCHAR2(20),
        scale VARCHAR2(30),
        row_index NUMBER,
        column_index NUMBER,
        page_number NUMBER,
        raw_row JSON,
        created_at TIMESTAMP DEFAULT SYSTIMESTAMP
    )
    """


def _load_oracledb() -> Any:
    import oracledb

    return oracledb


def _returned_number(variable: Any) -> int:
    value = variable.getvalue()
    if isinstance(value, list):
        value = value[0]
    return int(value)


def _to_vector(values: Sequence[float]) -> array.array[float]:
    return array.array("f", values)


def _validate_vector(values: Sequence[float], dimensions: int) -> None:
    if len(values) != dimensions:
        raise ValueError(f"Embedding has {len(values)} dimensions; expected {dimensions}.")

    for value in values:
        if not isinstance(value, (int, float, Decimal)):
            raise TypeError("Embedding vectors must contain numeric values.")


def _set_clob_inputs(cursor: Any, oracledb_module: Any, *names: str) -> None:
    if not hasattr(cursor, "setinputsizes"):
        return
    cursor.setinputsizes(**{name: oracledb_module.DB_TYPE_CLOB for name in names})
