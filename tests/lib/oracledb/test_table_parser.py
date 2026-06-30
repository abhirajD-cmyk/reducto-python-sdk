from __future__ import annotations

from decimal import Decimal

from reducto.lib.oracledb.models import ParsedTable
from reducto.lib.oracledb.table_parser import parse_number, parse_table_rows, promote_financial_facts


def test_parse_csv_table_rows() -> None:
    rows = parse_table_rows("Metric,2023,2022\nNet sales,$383285,$394328\nNet income,96995,99803")

    assert rows == [
        ["Metric", "2023", "2022"],
        ["Net sales", "$383285", "$394328"],
        ["Net income", "96995", "99803"],
    ]


def test_parse_markdown_table_rows() -> None:
    rows = parse_table_rows("| Metric | 2023 |\n| --- | ---: |\n| Assets | 352583 |")

    assert rows == [["Metric", "2023"], ["Assets", "352583"]]


def test_parse_html_table_rows() -> None:
    rows = parse_table_rows("<table><tr><th>Metric</th><th>2023</th></tr><tr><td>Revenue</td><td>10</td></tr></table>")

    assert rows == [["Metric", "2023"], ["Revenue", "10"]]


def test_parse_number_handles_parentheses_and_currency() -> None:
    assert parse_number("$(1,234.50)") == Decimal("-1234.50")


def test_promote_financial_facts() -> None:
    table = ParsedTable(
        table_index=0,
        chunk_index=0,
        block_index=1,
        page_number=42,
        content="Consolidated statements, in millions, dollars\nMetric,2023,2022\nRevenue,$10,$8",
        rows=[
            ["Metric", "2023", "2022"],
            ["Revenue", "$10", "$8"],
        ],
    )

    facts = promote_financial_facts(table)

    assert len(facts) == 2
    assert facts[0].metric == "Revenue"
    assert facts[0].period_label == "2023"
    assert facts[0].value == Decimal("10")
    assert facts[0].currency == "USD"
    assert facts[0].scale == "millions"
    assert facts[0].page_number == 42
