from __future__ import annotations

import re
import csv
from io import StringIO
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

from .models import ParsedTable, FinancialFact


class _TableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag == "tr":
            self._current_row = []
        elif tag in {"td", "th"} and self._current_row is not None:
            self._current_cell = []
            self._in_cell = True

    def handle_data(self, data: str) -> None:
        if self._in_cell and self._current_cell is not None:
            self._current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._current_row is not None:
            cell_text = " ".join("".join(self._current_cell or []).split())
            self._current_row.append(cell_text)
            self._current_cell = None
            self._in_cell = False
        elif tag == "tr" and self._current_row is not None:
            if any(cell.strip() for cell in self._current_row):
                self.rows.append(self._current_row)
            self._current_row = None


def parse_table_rows(content: str) -> list[list[str]]:
    text = content.strip()
    if not text:
        return []

    if "<table" in text.lower() or "<tr" in text.lower():
        parser = _TableHTMLParser()
        parser.feed(text)
        if parser.rows:
            return _normalize_rows(parser.rows)

    markdown_rows = _parse_markdown_table(text)
    if markdown_rows:
        return markdown_rows

    csv_rows = list(csv.reader(StringIO(text)))
    if _looks_tabular(csv_rows):
        return _normalize_rows(csv_rows)

    return []


def promote_financial_facts(table: ParsedTable) -> list[FinancialFact]:
    rows = _normalize_rows(table.rows)
    if len(rows) < 2:
        return []

    header = _choose_header(rows)
    if not header or len(header) < 2:
        return []

    start_index = rows.index(header) + 1
    facts: list[FinancialFact] = []
    scale = _detect_scale(table.content)
    currency = _detect_currency(table.content)

    for row_index, row in enumerate(rows[start_index:], start=start_index):
        if len(row) < 2:
            continue
        metric = _clean_metric(row[0])
        if not metric or _is_total_separator(metric):
            continue
        unit = _detect_unit(" ".join(row), metric)
        for column_index, raw_value in enumerate(row[1:], start=1):
            value = parse_number(raw_value)
            if value is None:
                continue
            period_label = header[column_index] if column_index < len(header) else f"column_{column_index}"
            facts.append(
                FinancialFact(
                    table_index=table.table_index,
                    row_index=row_index,
                    column_index=column_index,
                    metric=metric,
                    period_label=period_label,
                    value=value,
                    raw_value=raw_value,
                    unit=unit,
                    currency=currency,
                    scale=scale,
                    page_number=table.page_number,
                    raw_row=row,
                )
            )

    return facts


def parse_number(raw_value: str) -> Decimal | None:
    text = raw_value.strip()
    if not text or text in {"-", "--", "—", "n/a", "N/A"}:
        return None

    negative = "(" in text and ")" in text
    cleaned = text.replace(",", "").replace("$", "").replace("%", "")
    cleaned = cleaned.replace("(", "").replace(")", "")
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    if not match:
        return None
    try:
        value = Decimal(match.group(0))
    except InvalidOperation:
        return None
    if negative and value > 0:
        return -value
    return value


def _parse_markdown_table(text: str) -> list[list[str]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    pipe_lines = [line for line in lines if "|" in line]
    if len(pipe_lines) < 2:
        return []

    rows: list[list[str]] = []
    for line in pipe_lines:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells):
            continue
        rows.append(cells)

    return _normalize_rows(rows) if _looks_tabular(rows) else []


def _looks_tabular(rows: list[list[str]]) -> bool:
    normalized = _normalize_rows(rows)
    if len(normalized) < 2:
        return False
    return max(len(row) for row in normalized) > 1


def _normalize_rows(rows: list[list[str]]) -> list[list[str]]:
    normalized: list[list[str]] = []
    for row in rows:
        cleaned = [" ".join(cell.strip().split()) for cell in row]
        if any(cleaned):
            normalized.append(cleaned)
    return normalized


def _choose_header(rows: list[list[str]]) -> list[str] | None:
    for row in rows[:3]:
        if len(row) > 1 and any(not parse_number(cell) for cell in row[1:]):
            return row
    return rows[0] if rows else None


def _clean_metric(value: str) -> str:
    metric = re.sub(r"\s+", " ", value).strip(" .:")
    metric = re.sub(r"^\d+\s+", "", metric)
    return metric


def _is_total_separator(metric: str) -> bool:
    return set(metric) <= {"-", "_", "="}


def _detect_scale(content: str) -> str | None:
    lowered = content.lower()
    if "in millions" in lowered or "millions" in lowered:
        return "millions"
    if "in thousands" in lowered or "thousands" in lowered:
        return "thousands"
    return None


def _detect_currency(content: str) -> str | None:
    lowered = content.lower()
    if "$" in content or "dollar" in lowered or "usd" in lowered:
        return "USD"
    return None


def _detect_unit(row_text: str, metric: str) -> str | None:
    if "%" in row_text or "margin" in metric.lower() or "rate" in metric.lower():
        return "percent"
    return None
