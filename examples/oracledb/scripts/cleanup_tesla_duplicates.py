from __future__ import annotations

import os
import argparse
from typing import Any
from pathlib import Path
from dataclasses import dataclass

import oracledb

EXAMPLE_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class DocumentRow:
    document_id: int
    company: str | None
    fiscal_year: int | None
    filing_type: str | None
    source_kind: str | None
    title: str | None
    source_uri: str | None
    pdf_url: str | None
    chunks: int
    tables: int
    facts: int
    created_at: str

    @property
    def is_pdf(self) -> bool:
        return _looks_like_pdf(self.source_uri) or _looks_like_pdf(self.pdf_url)


def main() -> int:
    args = _parse_args()
    _load_env(Path(args.env_file))

    conn = oracledb.connect(
        user=os.environ["ORACLE_USER"],
        password=os.environ["ORACLE_PASSWORD"],
        dsn=os.environ["ORACLE_DSN"],
    )
    try:
        rows = _tesla_documents(conn, args.company)
        if not rows:
            print(f"No {args.company} documents found.")
            return 0

        pdf_rows = [row for row in rows if row.is_pdf]
        if not pdf_rows:
            print(f"Found {len(rows)} {args.company} document(s), but none are .pdf sources.")
            print("Refusing to delete because there is no PDF document to keep.")
            _print_documents(rows, keep_id=None)
            return 1

        keep = max(pdf_rows, key=lambda row: row.document_id)
        delete_ids = [row.document_id for row in rows if row.document_id != keep.document_id]

        print(f"Found {len(rows)} {args.company} document(s).")
        print(f"Keeping PDF document_id={keep.document_id}.")
        print(f"Deleting {len(delete_ids)} duplicate document(s): {delete_ids or 'none'}")
        print()
        _print_documents(rows, keep_id=keep.document_id)

        before = _totals(rows)
        print()
        print(f"Before cleanup: documents={before[0]}, chunks={before[1]}, tables={before[2]}, facts={before[3]}")

        if delete_ids and args.apply:
            with conn.cursor() as cur:
                cur.executemany(
                    "DELETE FROM documents WHERE document_id = :document_id",
                    [{"document_id": doc_id} for doc_id in delete_ids],
                )
            conn.commit()
        elif args.apply:
            print("Nothing to delete.")
        else:
            print("Dry run only. Re-run with --apply to delete these rows.")

        after_rows = _tesla_documents(conn, args.company)
        after = _totals(after_rows)
        print(f"After cleanup:  documents={after[0]}, chunks={after[1]}, tables={after[2]}, facts={after[3]}")
        return 0
    finally:
        conn.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Keep the newest Tesla PDF ingest and delete duplicate Tesla documents."
    )
    parser.add_argument("--company", default="TSLA")
    parser.add_argument("--env-file", default=str(EXAMPLE_ROOT / ".env"))
    parser.add_argument("--apply", action="store_true", help="Actually delete duplicate rows.")
    return parser.parse_args()


def _load_env(path: Path) -> None:
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _tesla_documents(conn: Any, company: str) -> list[DocumentRow]:
    aliases = _company_aliases(company)
    needles = [f"%{alias.lower()}%" for alias in aliases]
    alias_binds = ", ".join(f":alias_{index}" for index in range(len(aliases)))
    needle_predicates = " OR ".join(
        f"LOWER(NVL(d.source_uri, '')) LIKE :needle_{index} OR LOWER(NVL(d.title, '')) LIKE :needle_{index}"
        for index in range(len(needles))
    )
    sql = f"""
        SELECT d.document_id,
               d.company,
               d.fiscal_year,
               d.filing_type,
               d.source_kind,
               d.title,
               d.source_uri,
               d.pdf_url,
               (SELECT COUNT(*) FROM document_chunks c WHERE c.document_id = d.document_id),
               (SELECT COUNT(*) FROM parsed_tables t WHERE t.document_id = d.document_id),
               (SELECT COUNT(*) FROM financial_facts f WHERE f.document_id = d.document_id),
               TO_CHAR(d.created_at, 'YYYY-MM-DD HH24:MI:SS')
        FROM documents d
        WHERE UPPER(NVL(d.company, '')) IN ({alias_binds})
           OR {needle_predicates}
        ORDER BY d.created_at, d.document_id
    """
    binds = {f"alias_{index}": alias.upper() for index, alias in enumerate(aliases)}
    binds.update({f"needle_{index}": needle for index, needle in enumerate(needles)})
    with conn.cursor() as cur:
        cur.execute(sql, binds)
        return [
            DocumentRow(
                document_id=int(row[0]),
                company=row[1],
                fiscal_year=row[2],
                filing_type=row[3],
                source_kind=row[4],
                title=row[5],
                source_uri=row[6],
                pdf_url=row[7],
                chunks=int(row[8] or 0),
                tables=int(row[9] or 0),
                facts=int(row[10] or 0),
                created_at=str(row[11]),
            )
            for row in cur.fetchall()
        ]


def _print_documents(rows: list[DocumentRow], *, keep_id: int | None) -> None:
    for row in rows:
        marker = "KEEP" if row.document_id == keep_id else "DELETE"
        if keep_id is None:
            marker = "FOUND"
        pdf_marker = "pdf" if row.is_pdf else "non-pdf"
        print(
            f"{marker:6} doc_id={row.document_id} {pdf_marker:7} "
            f"company={row.company} year={row.fiscal_year} "
            f"chunks={row.chunks} tables={row.tables} facts={row.facts} "
            f"created={row.created_at}"
        )
        print(f"       title={row.title}")
        print(f"       source={row.source_uri}")


def _totals(rows: list[DocumentRow]) -> tuple[int, int, int, int]:
    return (
        len(rows),
        sum(row.chunks for row in rows),
        sum(row.tables for row in rows),
        sum(row.facts for row in rows),
    )


def _looks_like_pdf(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.lower().split("?", 1)[0].split("#", 1)[0]
    return normalized.endswith(".pdf")


def _company_aliases(company: str) -> list[str]:
    aliases = {company.upper(), company.lower(), company.title()}
    if company.upper() == "TSLA":
        aliases.update({"TESLA", "tesla", "Tesla"})
    return sorted(aliases)


if __name__ == "__main__":
    raise SystemExit(main())
