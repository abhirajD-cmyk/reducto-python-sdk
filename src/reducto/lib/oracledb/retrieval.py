from __future__ import annotations

import re
import array
from typing import Any
from collections.abc import Sequence

from .utils import read_lob
from .models import SearchResult, SearchFilters
from .embeddings import EmbeddingProvider

_TEXT_QUERY_TERM_RE = re.compile(r"[A-Za-z0-9]+")


class OracleHybridRetriever:
    def __init__(self, connection: Any, embedding_provider: EmbeddingProvider) -> None:
        self.connection = connection
        self.embedding_provider = embedding_provider

    def semantic_search(
        self,
        query: str,
        *,
        filters: SearchFilters | None = None,
        limit: int = 5,
    ) -> list[SearchResult]:
        query_vector = self.embedding_provider.embed_text(query)
        where_sql, params = _build_filters(filters)
        params["query_vector"] = _to_vector(query_vector)
        sql = f"""
            SELECT d.document_id,
                   c.chunk_id,
                   1 - VECTOR_DISTANCE(c.embedding, :query_vector, COSINE) AS score,
                   c.content,
                   d.company,
                   d.fiscal_year,
                   d.filing_type,
                   c.page_start,
                   c.page_end,
                   d.source_uri
            FROM document_chunks c
            JOIN documents d ON d.document_id = c.document_id
            WHERE c.embedding IS NOT NULL
              {where_sql}
            ORDER BY VECTOR_DISTANCE(c.embedding, :query_vector, COSINE)
            FETCH FIRST {max(1, int(limit))} ROWS ONLY
        """
        return _fetch_search_results(self.connection, sql, params)

    def hybrid_search(
        self,
        query: str,
        *,
        filters: SearchFilters | None = None,
        limit: int = 5,
    ) -> list[SearchResult]:
        query_vector = self.embedding_provider.embed_text(query)
        where_sql, params = _build_filters(filters)
        params["query_vector"] = _to_vector(query_vector)
        text_query = _escape_text_query(query)
        if not text_query:
            return self.semantic_search(query, filters=filters, limit=limit)
        params["text_query"] = text_query
        row_limit = max(1, int(limit))
        sql = f"""
            WITH vector_candidates AS (
                SELECT c.chunk_id,
                       1 - VECTOR_DISTANCE(c.embedding, :query_vector, COSINE) AS vector_score,
                       0 AS text_score
                FROM document_chunks c
                JOIN documents d ON d.document_id = c.document_id
                WHERE c.embedding IS NOT NULL
                  {where_sql}
                ORDER BY VECTOR_DISTANCE(c.embedding, :query_vector, COSINE)
                FETCH FIRST {row_limit} ROWS ONLY
            ),
            text_candidates AS (
                SELECT c.chunk_id,
                       0 AS vector_score,
                       SCORE(1) / 100 AS text_score
                FROM document_chunks c
                JOIN documents d ON d.document_id = c.document_id
                WHERE CONTAINS(c.content, :text_query, 1) > 0
                  {where_sql}
                ORDER BY SCORE(1) DESC
                FETCH FIRST {row_limit} ROWS ONLY
            ),
            ranked AS (
                SELECT chunk_id, MAX(vector_score) + MAX(text_score) AS score
                FROM (
                    SELECT chunk_id, vector_score, text_score FROM vector_candidates
                    UNION ALL
                    SELECT chunk_id, vector_score, text_score FROM text_candidates
                )
                GROUP BY chunk_id
                ORDER BY score DESC
                FETCH FIRST {row_limit} ROWS ONLY
            )
            SELECT d.document_id,
                   c.chunk_id,
                   ranked.score,
                   c.content,
                   d.company,
                   d.fiscal_year,
                   d.filing_type,
                   c.page_start,
                   c.page_end,
                   d.source_uri
            FROM ranked
            JOIN document_chunks c ON c.chunk_id = ranked.chunk_id
            JOIN documents d ON d.document_id = c.document_id
            ORDER BY ranked.score DESC
        """
        return _fetch_search_results(self.connection, sql, params)

    def financial_facts(
        self,
        *,
        metric: str | None = None,
        filters: SearchFilters | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        where_sql, params = _build_filters(filters, alias="d")
        metric_sql = ""
        if metric:
            metric_sql = "AND LOWER(f.metric) LIKE LOWER(:metric)"
            params["metric"] = f"%{metric}%"

        sql = f"""
            SELECT d.company,
                   d.fiscal_year,
                   d.filing_type,
                   f.metric,
                   f.period_label,
                   f.value,
                   f.raw_value,
                   f.currency,
                   f.scale,
                   f.page_number,
                   d.source_uri
            FROM financial_facts f
            JOIN documents d ON d.document_id = f.document_id
            WHERE 1 = 1
              {where_sql}
              {metric_sql}
            ORDER BY d.company, d.fiscal_year DESC, f.metric, f.period_label
            FETCH FIRST {max(1, int(limit))} ROWS ONLY
        """
        with self.connection.cursor() as cursor:
            cursor.execute(sql, params)
            return [
                {
                    "company": row[0],
                    "fiscal_year": row[1],
                    "filing_type": row[2],
                    "metric": row[3],
                    "period_label": row[4],
                    "value": row[5],
                    "raw_value": row[6],
                    "currency": row[7],
                    "scale": row[8],
                    "page_number": row[9],
                    "source_uri": row[10],
                }
                for row in cursor.fetchall()
            ]


def _fetch_search_results(
    connection: Any,
    sql: str,
    params: dict[str, object],
) -> list[SearchResult]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    return [
        SearchResult(
            document_id=int(row[0]),
            chunk_id=int(row[1]),
            score=float(row[2]),
            content=str(read_lob(row[3]) or ""),
            company=row[4],
            fiscal_year=int(row[5]) if row[5] is not None else None,
            filing_type=row[6],
            page_start=int(row[7]) if row[7] is not None else None,
            page_end=int(row[8]) if row[8] is not None else None,
            source_uri=str(row[9] or ""),
        )
        for row in rows
    ]


def _build_filters(filters: SearchFilters | None, alias: str = "d") -> tuple[str, dict[str, object]]:
    if filters is None:
        return "", {}

    clauses: list[str] = []
    params: dict[str, object] = {}
    if filters.company:
        clauses.append(f"AND UPPER({alias}.company) = UPPER(:company)")
        params["company"] = filters.company
    if filters.fiscal_year is not None:
        clauses.append(f"AND {alias}.fiscal_year = :fiscal_year")
        params["fiscal_year"] = filters.fiscal_year
    if filters.filing_type:
        clauses.append(f"AND UPPER({alias}.filing_type) = UPPER(:filing_type)")
        params["filing_type"] = filters.filing_type
    if filters.page_number is not None:
        clauses.append("AND c.page_start <= :page_number AND c.page_end >= :page_number")
        params["page_number"] = filters.page_number
    return "\n".join(clauses), params


def _to_vector(values: Sequence[float]) -> array.array[float]:
    return array.array("f", values)


def _escape_text_query(query: str) -> str:
    terms = _TEXT_QUERY_TERM_RE.findall(query)
    return " ".join(terms[:20])
