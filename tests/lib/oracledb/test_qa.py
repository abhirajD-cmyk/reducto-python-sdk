from __future__ import annotations

from reducto.lib.oracledb.qa import format_answer, snippet_for_query, answer_from_search_results
from reducto.lib.oracledb.models import SearchResult


def test_answer_from_search_results_picks_concise_financial_sentence() -> None:
    content = (
        """
    [[START OF PAGE 16]]
    During 2023, the Company's net sales through its direct and indirect
    distribution channels accounted for 37% and 63%, respectively, of total net sales.
    [[END OF PAGE 16]]
    """
        + ("filler text. " * 200)
        + """
    [[START OF PAGE 54]]
    Fiscal Year Highlights
    The Company's total net sales were $383.3 billion and net income was
    $97.0 billion during 2023. The Company introduced several new products.
    [[END OF PAGE 54]]
    """
    )
    result = SearchResult(
        document_id=1,
        chunk_id=10,
        score=0.1,
        content=content,
        company="AAPL",
        fiscal_year=2023,
        filing_type="10-K",
        page_start=1,
        page_end=100,
        source_uri="https://example.test/aapl-2023.htm",
    )

    answer = answer_from_search_results(
        "What were Apple's net sales in 2023?",
        [result],
    )

    assert answer.answer == (
        "The Company's total net sales were $383.3 billion and net income was $97.0 billion during 2023."
    )
    assert answer.evidence[0].page_number == 54
    assert "$383.3 billion" in answer.evidence[0].text


def test_format_answer_outputs_readable_sections() -> None:
    result = SearchResult(
        document_id=1,
        chunk_id=10,
        score=0.1,
        content="[[START OF PAGE 2]] The answer is in this relevant sentence.",
        company="ACME",
        fiscal_year=2024,
        filing_type="10-K",
        page_start=2,
        page_end=2,
        source_uri="https://example.test/acme.htm",
    )

    formatted = format_answer(answer_from_search_results("Where is the answer?", [result]))

    assert "Question" in formatted
    assert "Answer" in formatted
    assert "Evidence" in formatted
    assert "Source" in formatted


def test_driver_question_prefers_sales_explanation_over_table_artifact() -> None:
    content = (
        """
    [[START OF PAGE 66]]
    3", "P e r c e n t a g e o f t o t a l",,,7,,%,,,,,,,,,,6,,%
    "n e t s a l e s",,,,,,,,,,,,,,,,,,,,,,,,,,,,,
    "T o t a l o p e r a t i n g e x p e n s e s",,,$,"5 4 , 8 4 7"
    Research and Development The year-over-year growth in R&D expense in 2023 was
    driven primarily by increases in headcount-related expenses.
    [[END OF PAGE 66]]
    """
        + ("filler text. " * 200)
        + """
    [[START OF PAGE 54]]
    The Company's total net sales decreased 3% or $11.0 billion during 2023
    compared to 2022. The weakness in foreign currencies relative to the U.S.
    dollar accounted for more than the entire year-over-year decrease in total
    net sales, which consisted primarily of lower net sales of Mac and iPhone,
    partially offset by higher net sales of Services.
    [[END OF PAGE 54]]
    """
    )
    result = SearchResult(
        document_id=1,
        chunk_id=10,
        score=0.1,
        content=content,
        company="AAPL",
        fiscal_year=2023,
        filing_type="10-K",
        page_start=1,
        page_end=100,
        source_uri="https://example.test/aapl-2023.htm",
    )

    answer = answer_from_search_results("What drove revenue growth?", [result])

    assert "total net sales decreased 3%" in answer.answer
    assert "foreign currencies" in answer.answer
    assert "P e r c e n t a g e" not in answer.answer
    assert "R&D expense" not in answer.answer


def test_legal_decision_question_prefers_holding_over_case_caption() -> None:
    content = """
    [[START OF PAGE 1]]
    # SUPREME COURT OF THE UNITED STATES No. 23-719# DONALD J. TRUMP,
    PETITIONER v. NORMA ANDERSON, ET AL. ON WRIT OF CERTIORARI TO THE
    SUPREME COURT OF COLORADO [March 4, 2024] PER CURIAM. A group of
    Colorado voters filed suit.
    [[END OF PAGE 1]]
    [[START OF PAGE 14]]
    # SUPREME COURT OF THE UNITED STATES No. 23-719# DONALD J. TRUMP,
    PETITIONER v. NORMA ANDERSON, ET AL. ON WRIT OF CERTIORARI TO THE
    SUPREME COURT OF COLORADO [March 4, 2024] JUSTICE BARRETT, concurring
    in part and concurring in the judgment. I agree that States lack the
    power to enforce Section 3 against Presiden- tial candidates. That
    principle is sufficient to resolve this case.
    [[END OF PAGE 14]]
    """
    result = SearchResult(
        document_id=1,
        chunk_id=10,
        score=0.1,
        content=content,
        company="SCOTUS",
        fiscal_year=2024,
        filing_type="Opinion",
        page_start=1,
        page_end=14,
        source_uri="https://example.test/trump-v-anderson.pdf",
    )

    answer = answer_from_search_results(
        "What did the Supreme Court decide in Trump v. Anderson?",
        [result],
    )

    assert "States lack the power to enforce Section 3" in answer.answer
    assert "Presidential candidates" in answer.answer
    assert "ON WRIT OF CERTIORARI" not in answer.answer


def test_snippet_for_query_uses_query_position_not_document_start() -> None:
    text = (
        "Document cover page and raw header. "
        + "middle filler " * 80
        + "The Company's total net sales were $383.3 billion."
    )

    snippet = snippet_for_query(text, "net sales")

    assert "total net sales" in snippet
    assert "Document cover page" not in snippet
