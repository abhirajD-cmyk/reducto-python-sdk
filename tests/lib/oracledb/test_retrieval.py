from __future__ import annotations

from reducto.lib.oracledb.retrieval import _escape_text_query


def test_escape_text_query_removes_oracle_text_punctuation() -> None:
    query = "What were Berkshire's revenues in 2023? Compare R&D / 10-K."

    assert _escape_text_query(query) == "What were Berkshire s revenues in 2023 Compare R D 10 K"


def test_escape_text_query_limits_terms() -> None:
    query = " ".join(f"term{index}" for index in range(25))

    escaped = _escape_text_query(query)

    assert escaped == " ".join(f"term{index}" for index in range(20))
