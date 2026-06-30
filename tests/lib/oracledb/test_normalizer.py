from __future__ import annotations

from reducto.lib.oracledb.normalizer import normalize_parse_response


def test_normalize_inline_parse_response_extracts_chunks_tables_and_facts() -> None:
    response = {
        "job_id": "job_123",
        "duration": 1.5,
        "result": {
            "type": "full",
            "chunks": [
                {
                    "content": "Revenue table",
                    "embed": "revenue net sales",
                    "blocks": [
                        {
                            "type": "Text",
                            "content": "Item 8",
                            "bbox": {"page": 1, "left": 0, "top": 0, "width": 1, "height": 1},
                        },
                        {
                            "type": "Table",
                            "content": "Metric,2023,2022\nNet sales,$383285,$394328",
                            "bbox": {"page": 2, "left": 0, "top": 0, "width": 1, "height": 1},
                            "confidence": "high",
                        },
                    ],
                }
            ],
        },
        "usage": {},
    }

    normalized = normalize_parse_response(response)

    assert normalized.job_id == "job_123"
    assert len(normalized.chunks) == 1
    assert normalized.chunks[0].page_start == 1
    assert normalized.chunks[0].page_end == 2
    assert len(normalized.blocks) == 2
    assert len(normalized.tables) == 1
    assert normalized.tables[0].rows[1][0] == "Net sales"
    assert len(normalized.financial_facts) == 2


def test_normalize_rejects_unfetched_url_result() -> None:
    response = {
        "job_id": "job_url",
        "result": {"type": "url", "url": "https://example.test/result.json"},
    }

    try:
        normalize_parse_response(response)
    except ValueError as exc:
        assert "URL parse result" in str(exc)
    else:
        raise AssertionError("Expected URL result normalization to fail before fetch")
