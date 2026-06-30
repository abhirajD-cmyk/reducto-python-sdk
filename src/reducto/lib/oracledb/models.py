from __future__ import annotations

from typing import Union, Literal
from decimal import Decimal
from dataclasses import field, dataclass

JsonValue = Union[None, bool, int, float, str, list["JsonValue"], dict[str, "JsonValue"]]


@dataclass(frozen=True)
class DocumentMetadata:
    source_uri: str
    company: str | None = None
    fiscal_year: int | None = None
    filing_type: str = "10-K"
    source_kind: Literal["url", "file", "upload"] = "url"
    title: str | None = None


@dataclass(frozen=True)
class NormalizedBlock:
    chunk_index: int
    block_index: int
    block_type: str
    content: str
    page_number: int | None = None
    original_page_number: int | None = None
    bbox: dict[str, JsonValue] = field(default_factory=dict)
    confidence: str | None = None
    extra: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedChunk:
    chunk_index: int
    content: str
    embedding_text: str
    page_start: int | None
    page_end: int | None
    block_count: int
    metadata: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedTable:
    table_index: int
    chunk_index: int
    block_index: int
    page_number: int | None
    content: str
    rows: list[list[str]]
    metadata: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class FinancialFact:
    table_index: int
    row_index: int
    column_index: int
    metric: str
    period_label: str
    value: Decimal
    raw_value: str
    unit: str | None = None
    currency: str | None = None
    scale: str | None = None
    page_number: int | None = None
    raw_row: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NormalizedParseResult:
    job_id: str
    raw_response: dict[str, JsonValue]
    chunks: list[NormalizedChunk]
    blocks: list[NormalizedBlock]
    tables: list[ParsedTable]
    financial_facts: list[FinancialFact]
    duration_seconds: float | None = None
    pdf_url: str | None = None
    studio_link: str | None = None


@dataclass(frozen=True)
class NormalizedExtractResult:
    job_id: str | None
    raw_response: dict[str, JsonValue]
    schema_json: dict[str, JsonValue]
    extracted_json: JsonValue
    request_body: dict[str, JsonValue] = field(default_factory=dict)
    citations_enabled: bool = True
    duration_seconds: float | None = None
    studio_link: str | None = None


@dataclass(frozen=True)
class SearchFilters:
    company: str | None = None
    fiscal_year: int | None = None
    filing_type: str | None = None
    page_number: int | None = None


@dataclass(frozen=True)
class SearchResult:
    document_id: int
    chunk_id: int
    score: float
    content: str
    company: str | None
    fiscal_year: int | None
    filing_type: str | None
    page_start: int | None
    page_end: int | None
    source_uri: str
