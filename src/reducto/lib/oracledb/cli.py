from __future__ import annotations

import json
import array
import argparse
from typing import Any, Literal
from pathlib import Path
from collections.abc import Sequence

from .qa import format_answer, snippet_for_query, answer_from_search_results
from .utils import read_lob
from .config import connect_oracle, vector_dimensions_from_env
from .models import SearchFilters, DocumentMetadata
from .oracle import OracleSchemaManager, OracleDocumentRepository
from .retrieval import OracleHybridRetriever
from .embeddings import (
    embed_many,
    embedding_provider_name,
    embedding_provider_from_env,
)
from .reducto_client import ReductoDocumentParser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reducto-oracledb")
    subparsers = parser.add_subparsers(required=True)

    init_db = subparsers.add_parser("init-db", help="Create Oracle tables and indexes.")
    init_db.add_argument("--vector-dimensions", type=int, default=vector_dimensions_from_env())
    init_db.add_argument("--text-index", action="store_true", help="Create Oracle Text index.")
    init_db.set_defaults(func=_init_db)

    ingest = subparsers.add_parser("ingest", help="Parse or extract a document into Oracle.")
    source = ingest.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="Public or presigned document URL.")
    source.add_argument("--file", type=Path, help="Local filing PDF or document path.")
    ingest.add_argument("--mode", choices=["parse", "extract"], default="parse")
    ingest.add_argument("--company")
    ingest.add_argument("--year", type=int, dest="fiscal_year")
    ingest.add_argument("--filing-type", default="10-K")
    ingest.add_argument("--title")
    ingest.add_argument("--force-url-result", action="store_true")
    ingest.add_argument("--agentic-tables", action="store_true")
    ingest.add_argument("--schema-file", type=Path, help="JSON schema file for extract mode.")
    ingest.add_argument("--extract-system-prompt", help="Optional Reducto Extract system prompt.")
    ingest.add_argument("--deep-extract", action="store_true", help="Use Reducto Deep Extract.")
    ingest.add_argument(
        "--no-citations",
        action="store_true",
        help="Disable Reducto Extract citations on typed values.",
    )
    ingest.add_argument(
        "--no-numerical-confidence",
        action="store_true",
        help="Disable numeric citation confidence scores.",
    )
    ingest.set_defaults(func=_ingest)

    search = subparsers.add_parser("search", help="Search chunks using Oracle vectors.")
    search.add_argument("query")
    search.add_argument("--mode", choices=["semantic", "hybrid"], default="semantic")
    search.add_argument("--company")
    search.add_argument("--year", type=int, dest="fiscal_year")
    search.add_argument("--filing-type")
    search.add_argument("--page", type=int, dest="page_number")
    search.add_argument("--limit", type=int, default=5)
    search.set_defaults(func=_search)

    ask = subparsers.add_parser("ask", help="Ask a question and print a concise answer.")
    ask.add_argument("query")
    ask.add_argument("--mode", choices=["semantic", "hybrid"], default="semantic")
    ask.add_argument("--company")
    ask.add_argument("--year", type=int, dest="fiscal_year")
    ask.add_argument("--filing-type")
    ask.add_argument("--page", type=int, dest="page_number")
    ask.add_argument("--limit", type=int, default=5, help="Number of chunks to retrieve.")
    ask.add_argument("--evidence-limit", type=int, default=3, help="Number of evidence snippets.")
    ask.set_defaults(func=_ask)

    facts = subparsers.add_parser("facts", help="Query promoted financial table facts.")
    facts.add_argument("--metric")
    facts.add_argument("--company")
    facts.add_argument("--year", type=int, dest="fiscal_year")
    facts.add_argument("--filing-type")
    facts.add_argument("--limit", type=int, default=20)
    facts.set_defaults(func=_facts)

    reembed = subparsers.add_parser("reembed", help="Recompute stored chunk embeddings.")
    reembed.add_argument("--company")
    reembed.add_argument("--year", type=int, dest="fiscal_year")
    reembed.add_argument("--filing-type")
    reembed.add_argument("--batch-size", type=int, default=32)
    reembed.add_argument("--chunk-id-after", type=int)
    reembed.set_defaults(func=_reembed)

    return parser


def _init_db(args: argparse.Namespace) -> int:
    connection = connect_oracle()
    OracleSchemaManager(connection).create_schema(
        vector_dimensions=args.vector_dimensions,
        create_text_index=args.text_index,
    )
    print("Oracle schema is ready.")
    return 0


def _ingest(args: argparse.Namespace) -> int:
    parser = ReductoDocumentParser()
    source_kind: Literal["url", "file", "upload"]
    if args.url:
        source_uri = args.url
        source_kind = "url"
    else:
        source_uri = str(args.file)
        source_kind = "file"

    metadata = DocumentMetadata(
        source_uri=source_uri,
        source_kind=source_kind,
        company=args.company,
        fiscal_year=args.fiscal_year,
        filing_type=args.filing_type,
        title=args.title,
    )

    if args.mode == "extract":
        if args.schema_file is None:
            raise SystemExit("--schema-file is required when --mode extract")
        schema = _load_json_schema(args.schema_file)
        if args.url:
            extract_result = parser.extract_url(
                args.url,
                schema=schema,
                citations=not args.no_citations,
                numerical_confidence=not args.no_numerical_confidence,
                deep_extract=args.deep_extract,
                system_prompt=args.extract_system_prompt,
                agentic_tables=args.agentic_tables,
            )
        else:
            extract_result = parser.extract_file(
                args.file,
                schema=schema,
                citations=not args.no_citations,
                numerical_confidence=not args.no_numerical_confidence,
                deep_extract=args.deep_extract,
                system_prompt=args.extract_system_prompt,
                agentic_tables=args.agentic_tables,
            )

        connection = connect_oracle()
        document_id, extraction_id = OracleDocumentRepository(connection).store_extract_result(
            metadata,
            extract_result,
        )
        print(
            json.dumps(
                {
                    "document_id": document_id,
                    "extraction_id": extraction_id,
                    "reducto_job_id": extract_result.job_id,
                    "citations_enabled": extract_result.citations_enabled,
                    "schema_fields": _schema_field_count(schema),
                    "result_type": type(extract_result.extracted_json).__name__,
                },
                indent=2,
            )
        )
        return 0

    if args.url:
        parse_result = parser.parse_url(
            args.url,
            force_url_result=args.force_url_result,
            agentic_tables=args.agentic_tables,
        )
    else:
        parse_result = parser.parse_file(
            args.file,
            force_url_result=args.force_url_result,
            agentic_tables=args.agentic_tables,
        )

    embedding_provider = embedding_provider_from_env(input_type="search_document")
    connection = connect_oracle()
    document_id = OracleDocumentRepository(connection).store_parse_result(
        metadata,
        parse_result,
        embedding_provider,
    )
    print(
        json.dumps(
            {
                "document_id": document_id,
                "reducto_job_id": parse_result.job_id,
                "chunks": len(parse_result.chunks),
                "tables": len(parse_result.tables),
                "financial_facts": len(parse_result.financial_facts),
                "embedding_provider": embedding_provider_name(embedding_provider),
            },
            indent=2,
        )
    )
    return 0


def _load_json_schema(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        schema = json.load(file)
    if not isinstance(schema, dict):
        raise SystemExit(f"{path} must contain a JSON object schema.")
    return schema


def _schema_field_count(schema: dict[str, Any]) -> int:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        return len(properties)
    return len(schema)


def _search(args: argparse.Namespace) -> int:
    connection = connect_oracle()
    retriever = OracleHybridRetriever(
        connection,
        embedding_provider_from_env(input_type="search_query"),
    )
    filters = _filters_from_args(args)
    if args.mode == "hybrid":
        results = retriever.hybrid_search(args.query, filters=filters, limit=args.limit)
    else:
        results = retriever.semantic_search(args.query, filters=filters, limit=args.limit)

    print(
        json.dumps(
            [
                {
                    "score": result.score,
                    "company": result.company,
                    "fiscal_year": result.fiscal_year,
                    "filing_type": result.filing_type,
                    "page_start": result.page_start,
                    "page_end": result.page_end,
                    "source_uri": result.source_uri,
                    "content": snippet_for_query(result.content, args.query),
                }
                for result in results
            ],
            indent=2,
        )
    )
    return 0


def _ask(args: argparse.Namespace) -> int:
    connection = connect_oracle()
    retriever = OracleHybridRetriever(
        connection,
        embedding_provider_from_env(input_type="search_query"),
    )
    filters = _filters_from_args(args)
    if args.mode == "hybrid":
        results = retriever.hybrid_search(args.query, filters=filters, limit=args.limit)
    else:
        results = retriever.semantic_search(args.query, filters=filters, limit=args.limit)

    answer = answer_from_search_results(
        args.query,
        results,
        evidence_limit=args.evidence_limit,
    )
    print(format_answer(answer))
    return 0


def _facts(args: argparse.Namespace) -> int:
    connection = connect_oracle()
    retriever = OracleHybridRetriever(
        connection,
        embedding_provider_from_env(input_type="search_query"),
    )
    rows = retriever.financial_facts(
        metric=args.metric,
        filters=_filters_from_args(args),
        limit=args.limit,
    )
    print(json.dumps(rows, indent=2, default=str))
    return 0


def _reembed(args: argparse.Namespace) -> int:
    provider = embedding_provider_from_env(input_type="search_document")
    provider_name = embedding_provider_name(provider)
    filters = _filters_from_args(args)
    where_sql, params = _document_filters(filters)
    if args.chunk_id_after is not None:
        where_sql = f"{where_sql}\nAND c.chunk_id > :chunk_id_after"
        params["chunk_id_after"] = args.chunk_id_after
    batch_size = max(1, int(args.batch_size))
    connection = connect_oracle()
    updated = 0
    try:
        with connection.cursor() as read_cursor, connection.cursor() as write_cursor:
            read_cursor.execute(
                f"""
                SELECT c.chunk_id,
                       c.embedding_text,
                       c.content
                FROM document_chunks c
                JOIN documents d ON d.document_id = c.document_id
                WHERE 1 = 1
                  {where_sql}
                ORDER BY c.chunk_id
                """,
                params,
            )
            while rows := read_cursor.fetchmany(batch_size):
                chunk_ids: list[int] = []
                texts: list[str] = []
                for row in rows:
                    chunk_ids.append(int(row[0]))
                    texts.append(str(read_lob(row[1]) or read_lob(row[2]) or ""))

                vectors = embed_many(provider, texts)
                if len(chunk_ids) != len(vectors):
                    raise ValueError("Embedding vector count must match the selected chunk count.")
                write_cursor.executemany(
                    "UPDATE document_chunks SET embedding = :embedding WHERE chunk_id = :chunk_id",
                    [
                        {"embedding": array.array("f", vector), "chunk_id": chunk_id}
                        for chunk_id, vector in zip(chunk_ids, vectors)
                    ],
                )
                connection.commit()
                updated += len(chunk_ids)
                print(f"Re-embedded {updated} chunks with {provider_name}.")
    finally:
        connection.close()

    print(json.dumps({"updated_chunks": updated, "provider": provider_name}, indent=2))
    return 0


def _filters_from_args(args: argparse.Namespace) -> SearchFilters:
    return SearchFilters(
        company=getattr(args, "company", None),
        fiscal_year=getattr(args, "fiscal_year", None),
        filing_type=getattr(args, "filing_type", None),
        page_number=getattr(args, "page_number", None),
    )


def _document_filters(filters: SearchFilters) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if filters.company:
        clauses.append("AND UPPER(d.company) = UPPER(:company)")
        params["company"] = filters.company
    if filters.fiscal_year is not None:
        clauses.append("AND d.fiscal_year = :fiscal_year")
        params["fiscal_year"] = filters.fiscal_year
    if filters.filing_type:
        clauses.append("AND UPPER(d.filing_type) = UPPER(:filing_type)")
        params["filing_type"] = filters.filing_type
    return "\n".join(clauses), params


if __name__ == "__main__":
    raise SystemExit(main())
