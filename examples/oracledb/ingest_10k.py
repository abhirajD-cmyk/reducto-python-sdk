#!/usr/bin/env -S rye run python

from __future__ import annotations

from reducto.lib.oracledb.config import connect_oracle
from reducto.lib.oracledb.models import DocumentMetadata
from reducto.lib.oracledb.oracle import OracleDocumentRepository
from reducto.lib.oracledb.embeddings import embedding_provider_name, embedding_provider_from_env
from reducto.lib.oracledb.reducto_client import ReductoDocumentParser

TEN_K_URL = "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20230930.htm"


def main() -> None:
    parser = ReductoDocumentParser()
    parsed = parser.parse_url(TEN_K_URL)
    metadata = DocumentMetadata(
        source_uri=TEN_K_URL,
        source_kind="url",
        company="AAPL",
        fiscal_year=2023,
        filing_type="10-K",
        title="Apple 2023 Form 10-K",
    )
    repository = OracleDocumentRepository(connect_oracle())
    embedding_provider = embedding_provider_from_env(input_type="search_document")
    document_id = repository.store_parse_result(
        metadata,
        parsed,
        embedding_provider,
    )
    print(f"Stored document_id={document_id} with {embedding_provider_name(embedding_provider)}")


if __name__ == "__main__":
    main()
