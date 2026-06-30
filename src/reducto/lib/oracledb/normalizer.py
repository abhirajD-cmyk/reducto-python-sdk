from __future__ import annotations

from collections import Counter

from .utils import as_dict, int_or_none, to_plain_data
from .models import (
    JsonValue,
    ParsedTable,
    FinancialFact,
    NormalizedBlock,
    NormalizedChunk,
    NormalizedParseResult,
)
from .table_parser import parse_table_rows, promote_financial_facts


def normalize_parse_response(response: object) -> NormalizedParseResult:
    raw = as_dict(to_plain_data(response))
    result = as_dict(raw.get("result", raw))

    if result.get("type") == "url":
        raise ValueError("URL parse result must be fetched before normalization.")
    if result.get("type") != "full" and "chunks" not in result:
        raise ValueError("Expected a Reducto full parse result with chunks.")

    chunks_payload = result.get("chunks", [])
    if not isinstance(chunks_payload, list):
        raise ValueError("Reducto parse result has invalid chunks payload.")

    chunks: list[NormalizedChunk] = []
    blocks: list[NormalizedBlock] = []
    tables: list[ParsedTable] = []
    facts: list[FinancialFact] = []
    table_index = 0

    for chunk_index, chunk_value in enumerate(chunks_payload):
        chunk = as_dict(chunk_value)
        chunk_blocks = chunk.get("blocks", [])
        if not isinstance(chunk_blocks, list):
            chunk_blocks = []

        normalized_blocks: list[NormalizedBlock] = []
        for block_index, block_value in enumerate(chunk_blocks):
            block = as_dict(block_value)
            bbox = _json_object(block.get("bbox"))
            extra = _json_object(block.get("extra"))
            page_number = int_or_none(bbox.get("page"))
            normalized_block = NormalizedBlock(
                chunk_index=chunk_index,
                block_index=block_index,
                block_type=str(block.get("type", "Text")),
                content=str(block.get("content", "")),
                page_number=page_number,
                original_page_number=int_or_none(bbox.get("original_page")),
                bbox=bbox,
                confidence=_optional_str(block.get("confidence")),
                extra=extra,
            )
            normalized_blocks.append(normalized_block)

            if normalized_block.block_type.lower() == "table":
                rows = parse_table_rows(normalized_block.content)
                table = ParsedTable(
                    table_index=table_index,
                    chunk_index=chunk_index,
                    block_index=block_index,
                    page_number=page_number,
                    content=normalized_block.content,
                    rows=rows,
                    metadata={
                        "bbox": bbox,
                        "confidence": normalized_block.confidence,
                        "extra": extra,
                    },
                )
                tables.append(table)
                facts.extend(promote_financial_facts(table))
                table_index += 1

        blocks.extend(normalized_blocks)
        pages = [block.page_number for block in normalized_blocks if block.page_number is not None]
        block_types = Counter(block.block_type for block in normalized_blocks)
        content = str(chunk.get("content", ""))
        embedding_text = str(chunk.get("embed") or chunk.get("enriched") or content)
        chunks.append(
            NormalizedChunk(
                chunk_index=chunk_index,
                content=content,
                embedding_text=embedding_text,
                page_start=min(pages) if pages else None,
                page_end=max(pages) if pages else None,
                block_count=len(normalized_blocks),
                metadata={
                    "block_type_counts": dict(block_types),
                    "blocks": [
                        {
                            "type": block.block_type,
                            "page_number": block.page_number,
                            "original_page_number": block.original_page_number,
                            "confidence": block.confidence,
                            "bbox": block.bbox,
                        }
                        for block in normalized_blocks
                    ],
                    "enrichment_success": chunk.get("enrichment_success"),
                },
            )
        )

    return NormalizedParseResult(
        job_id=str(raw.get("job_id", "")),
        duration_seconds=_optional_float(raw.get("duration")),
        pdf_url=_optional_str(raw.get("pdf_url")),
        studio_link=_optional_str(raw.get("studio_link")),
        raw_response=raw,
        chunks=chunks,
        blocks=blocks,
        tables=tables,
        financial_facts=facts,
    )


def _json_object(value: JsonValue | object) -> dict[str, JsonValue]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def _optional_str(value: object) -> str | None:
    return str(value) if value is not None else None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
