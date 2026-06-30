from __future__ import annotations

import os
import re
import sys
import json
import time
import argparse
import tempfile
import mimetypes
from http import HTTPStatus
from typing import Any, cast
from pathlib import Path
from dataclasses import asdict, dataclass
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from typing_extensions import override

from reducto.lib.oracledb.qa import answer_from_search_results
from reducto.lib.oracledb.config import connect_oracle, vector_dimensions_from_env
from reducto.lib.oracledb.models import SearchFilters, DocumentMetadata
from reducto.lib.oracledb.oracle import OracleSchemaManager, OracleDocumentRepository
from reducto.lib.oracledb.retrieval import OracleHybridRetriever
from reducto.lib.oracledb.embeddings import embedding_provider_name, embedding_provider_from_env
from reducto.lib.oracledb.reducto_client import ReductoDocumentParser

EXAMPLE_ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY_BYTES = 120 * 1024 * 1024
DEFAULT_EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "company_name": {"type": "string"},
        "fiscal_year": {"type": "integer"},
        "filing_type": {"type": "string"},
        "total_net_sales_millions": {"type": "number"},
        "net_income_millions": {"type": "number"},
        "cash_and_cash_equivalents_millions": {"type": "number"},
        "auditor_name": {"type": "string"},
        "business_summary": {"type": "string"},
    },
    "required": ["company_name", "fiscal_year", "filing_type"],
}


class DemoError(Exception):
    def __init__(self, message: str, *, status_code: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class UploadedFile:
    filename: str
    content_type: str
    content: bytes


@dataclass(frozen=True)
class MultipartForm:
    fields: dict[str, str]
    files: dict[str, UploadedFile]


def load_env(path: Path = EXAMPLE_ROOT / ".env") -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def status_payload() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "environment": {
            "oracle_user": os.getenv("ORACLE_USER", ""),
            "oracle_dsn": os.getenv("ORACLE_DSN", ""),
            "oracle_vector_dimensions": os.getenv("ORACLE_VECTOR_DIMENSIONS", ""),
            "reducto_api_key": bool(os.getenv("REDUCTO_API_KEY")),
            "sec_user_agent": bool(os.getenv("SEC_USER_AGENT")),
            "embedding_provider": embedding_provider_name(),
        },
        "database": {
            "connected": False,
            "tables": {},
            "documents": [],
        },
    }
    try:
        connection = connect_oracle()
        try:
            OracleSchemaManager(connection).create_schema(vector_dimensions=vector_dimensions_from_env())
            payload["database"] = {
                "connected": True,
                "tables": table_counts(connection),
                "documents": list_documents(connection, limit=6),
            }
        finally:
            connection.close()
    except Exception as exc:
        payload["database"] = {
            "connected": False,
            "error": _public_error(exc),
            "tables": {},
            "documents": [],
        }
    return payload


def table_counts(connection: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in (
        "DOCUMENTS",
        "DOCUMENT_EXTRACTIONS",
        "DOCUMENT_CHUNKS",
        "PARSED_TABLES",
        "FINANCIAL_FACTS",
    ):
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            row = cursor.fetchone()
            counts[table] = int(row[0]) if row else 0
    return counts


def list_documents(connection: Any, *, limit: int = 20) -> list[dict[str, Any]]:
    sql = f"""
        SELECT d.document_id,
               d.company,
               d.fiscal_year,
               d.filing_type,
               d.title,
               d.source_uri,
               TO_CHAR(d.created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at,
               (SELECT COUNT(*) FROM document_chunks c WHERE c.document_id = d.document_id),
               (SELECT COUNT(*) FROM parsed_tables t WHERE t.document_id = d.document_id),
               (SELECT COUNT(*) FROM financial_facts f WHERE f.document_id = d.document_id),
               (SELECT COUNT(*) FROM document_extractions e WHERE e.document_id = d.document_id)
        FROM documents d
        ORDER BY d.created_at DESC
        FETCH FIRST {max(1, int(limit))} ROWS ONLY
    """
    with connection.cursor() as cursor:
        cursor.execute(sql)
        rows = cursor.fetchall()
    return [
        {
            "document_id": int(row[0]),
            "company": row[1],
            "fiscal_year": int(row[2]) if row[2] is not None else None,
            "filing_type": row[3],
            "title": row[4],
            "source_uri": row[5],
            "created_at": row[6],
            "chunks": int(row[7]),
            "tables": int(row[8]),
            "financial_facts": int(row[9]),
            "extractions": int(row[10]),
        }
        for row in rows
    ]


def extract_url(payload: dict[str, Any]) -> dict[str, Any]:
    url = _required_str(payload, "url")
    metadata = _metadata_from_payload(payload, source_uri=url, source_kind="url")
    schema = _extract_schema_from_payload(payload)
    citations_enabled = not _bool(payload.get("disable_citations"))
    numerical_confidence = not _bool(payload.get("disable_numerical_confidence"))
    deep_extract = _bool(payload.get("deep_extract"))
    system_prompt = _optional_str(payload.get("system_prompt"))

    started = time.perf_counter()
    extract_started = time.perf_counter()
    extract_result = ReductoDocumentParser().extract_url(
        url,
        schema=schema,
        citations=citations_enabled,
        numerical_confidence=numerical_confidence,
        deep_extract=deep_extract,
        system_prompt=system_prompt,
        agentic_tables=_bool(payload.get("agentic_tables")),
    )
    extract_ms = _elapsed_ms(extract_started)
    store_started = time.perf_counter()
    document_id, extraction_id = _store_extract_result(metadata, extract_result)
    store_ms = _elapsed_ms(store_started)

    return {
        "route": "/api/extract/url",
        "backend_api": "Reducto Extract API",
        "reducto_endpoint": "/extract",
        "sdk_call": "client.extract.run",
        "document_id": document_id,
        "extraction_id": extraction_id,
        "reducto_job_id": extract_result.job_id,
        "studio_link": extract_result.studio_link,
        "citations_enabled": extract_result.citations_enabled,
        "schema_fields": _schema_field_count(schema),
        "request_body": extract_result.request_body,
        "extracted_json": extract_result.extracted_json,
        "elapsed_seconds": round((time.perf_counter() - started), 2),
        "latency": {
            "total_ms": _elapsed_ms(started),
            "extract_ms": extract_ms,
            "store_ms": store_ms,
            "reducto_ms": _seconds_to_ms(extract_result.duration_seconds),
        },
    }


def ingest_url(payload: dict[str, Any]) -> dict[str, Any]:
    url = _required_str(payload, "url")
    metadata = _metadata_from_payload(payload, source_uri=url, source_kind="url")
    started = time.perf_counter()
    parse_started = time.perf_counter()
    parse_result = ReductoDocumentParser().parse_url(
        url,
        force_url_result=_bool(payload.get("force_url_result")),
        agentic_tables=_bool(payload.get("agentic_tables")),
    )
    parse_ms = _elapsed_ms(parse_started)
    store_started = time.perf_counter()
    document_id = _store_parse_result(metadata, parse_result)
    store_ms = _elapsed_ms(store_started)
    return {
        "document_id": document_id,
        "reducto_job_id": parse_result.job_id,
        "chunks": len(parse_result.chunks),
        "tables": len(parse_result.tables),
        "financial_facts": len(parse_result.financial_facts),
        "elapsed_seconds": round((time.perf_counter() - started), 2),
        "latency": {
            "total_ms": _elapsed_ms(started),
            "parse_ms": parse_ms,
            "store_ms": store_ms,
            "reducto_ms": _seconds_to_ms(parse_result.duration_seconds),
        },
    }


def ingest_file(fields: dict[str, str], uploaded_file: UploadedFile) -> dict[str, Any]:
    if not uploaded_file.content:
        raise DemoError("Uploaded file was empty.")

    started = time.perf_counter()
    parse_ms = 0.0
    with tempfile.TemporaryDirectory(prefix="reducto-demo-") as temp_dir:
        safe_name = _safe_filename(uploaded_file.filename)
        path = Path(temp_dir) / safe_name
        path.write_bytes(uploaded_file.content)
        metadata = _metadata_from_payload(fields, source_uri=safe_name, source_kind="file")
        parse_started = time.perf_counter()
        parse_result = ReductoDocumentParser().parse_file(
            path,
            force_url_result=_bool(fields.get("force_url_result")),
            agentic_tables=_bool(fields.get("agentic_tables")),
        )
        parse_ms = _elapsed_ms(parse_started)
    store_started = time.perf_counter()
    document_id = _store_parse_result(metadata, parse_result)
    store_ms = _elapsed_ms(store_started)
    return {
        "document_id": document_id,
        "filename": safe_name,
        "reducto_job_id": parse_result.job_id,
        "chunks": len(parse_result.chunks),
        "tables": len(parse_result.tables),
        "financial_facts": len(parse_result.financial_facts),
        "elapsed_seconds": round((time.perf_counter() - started), 2),
        "latency": {
            "total_ms": _elapsed_ms(started),
            "parse_ms": parse_ms,
            "store_ms": store_ms,
            "reducto_ms": _seconds_to_ms(parse_result.duration_seconds),
        },
    }


def ask_question(payload: dict[str, Any]) -> dict[str, Any]:
    question = _required_str(payload, "question")
    mode = str(payload.get("mode") or "semantic")
    if mode not in {"semantic", "hybrid"}:
        raise DemoError("mode must be semantic or hybrid.")

    started = time.perf_counter()
    connection = connect_oracle()
    try:
        retriever = OracleHybridRetriever(
            connection,
            embedding_provider_from_env(input_type="search_query"),
        )
        filters = _filters_from_payload(payload)
        limit = _positive_int(payload.get("limit"), default=5)
        retrieval_started = time.perf_counter()
        if mode == "hybrid":
            results = retriever.hybrid_search(question, filters=filters, limit=limit)
        else:
            results = retriever.semantic_search(question, filters=filters, limit=limit)
        retrieval_ms = _elapsed_ms(retrieval_started)
        answer_started = time.perf_counter()
        answer = answer_from_search_results(
            question,
            results,
            evidence_limit=_positive_int(payload.get("evidence_limit"), default=3),
        )
        answer_ms = _elapsed_ms(answer_started)
    finally:
        connection.close()

    return {
        "question": answer.question,
        "answer": answer.answer,
        "evidence": [asdict(item) for item in answer.evidence],
        "latency": {
            "total_ms": _elapsed_ms(started),
            "retrieval_ms": retrieval_ms,
            "answer_ms": answer_ms,
            "result_count": len(results),
        },
    }


def parse_multipart_form(content_type: str, body: bytes) -> MultipartForm:
    boundary = _multipart_boundary(content_type)
    delimiter = b"--" + boundary
    fields: dict[str, str] = {}
    files: dict[str, UploadedFile] = {}

    for raw_part in body.split(delimiter)[1:]:
        part = raw_part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if part.endswith(b"--"):
            part = part[:-2].strip(b"\r\n")
        if b"\r\n\r\n" not in part:
            continue
        header_blob, content = part.split(b"\r\n\r\n", 1)
        headers = _parse_part_headers(header_blob)
        disposition = headers.get("content-disposition", "")
        params = _header_params(disposition)
        name = params.get("name")
        if not name:
            continue
        filename = params.get("filename")
        content = content.removesuffix(b"\r\n")
        if filename is not None and filename != "":
            files[name] = UploadedFile(
                filename=filename,
                content_type=headers.get("content-type", "application/octet-stream"),
                content=content,
            )
        else:
            fields[name] = content.decode("utf-8", errors="replace")
    return MultipartForm(fields=fields, files=files)


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "ReductoOracleDemo/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self._send_static(STATIC / "index.html")
            elif parsed.path.startswith("/static/"):
                self._send_static(STATIC / parsed.path.removeprefix("/static/"))
            elif parsed.path == "/api/status":
                self._send_json(status_payload())
            elif parsed.path == "/api/documents":
                connection = connect_oracle()
                try:
                    self._send_json({"documents": list_documents(connection)})
                finally:
                    connection.close()
            else:
                self._send_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._handle_exception(exc)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/ask":
                self._send_json(ask_question(self._read_json()))
            elif parsed.path == "/api/extract/url":
                self._send_json(extract_url(self._read_json()))
            elif parsed.path == "/api/ingest/url":
                self._send_json(ingest_url(self._read_json()))
            elif parsed.path == "/api/ingest/file":
                form = parse_multipart_form(
                    self.headers.get("Content-Type", ""),
                    self._read_body(),
                )
                uploaded_file = form.files.get("file")
                if uploaded_file is None:
                    raise DemoError("Missing uploaded file.")
                self._send_json(ingest_file(form.fields, uploaded_file))
            else:
                self._send_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._handle_exception(exc)

    @override
    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write(f"{self.address_string()} - {format % args}\n")

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY_BYTES:
            raise DemoError(
                "Request body is too large.",
                status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        return self.rfile.read(length)

    def _read_json(self) -> dict[str, Any]:
        try:
            data: object = json.loads(self._read_body().decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DemoError("Request body must be valid JSON.") from exc
        if not isinstance(data, dict):
            raise DemoError("JSON request body must be an object.")
        return cast(dict[str, Any], data)

    def _send_json(self, payload: dict[str, Any], *, status: int = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_static(self, path: Path) -> None:
        resolved = path.resolve()
        if not str(resolved).startswith(str(STATIC.resolve())) or not resolved.exists():
            self._send_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        body = resolved.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_exception(self, exc: Exception) -> None:
        if isinstance(exc, DemoError):
            self._send_json({"error": str(exc)}, status=exc.status_code)
            return
        self._send_json(
            {"error": _public_error(exc)},
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )


def run(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    load_env()
    server, selected_port = _make_server(host, port)
    print(f"Reducto OracleDB demo running at http://{host}:{selected_port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Reducto OracleDB demo site.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    run(host=str(args.host), port=int(args.port))
    return 0


def _make_server(host: str, port: int) -> tuple[ThreadingHTTPServer, int]:
    for selected_port in range(port, port + 20):
        try:
            return ThreadingHTTPServer((host, selected_port), DemoHandler), selected_port
        except OSError:
            continue
    raise RuntimeError(f"No available port from {port} to {port + 19}.")


def _store_parse_result(metadata: DocumentMetadata, parse_result: Any) -> int:
    connection = connect_oracle()
    try:
        OracleSchemaManager(connection).create_schema(vector_dimensions=vector_dimensions_from_env())
        return OracleDocumentRepository(connection).store_parse_result(
            metadata,
            parse_result,
            embedding_provider_from_env(input_type="search_document"),
        )
    finally:
        connection.close()


def _store_extract_result(metadata: DocumentMetadata, extract_result: Any) -> tuple[int, int]:
    connection = connect_oracle()
    try:
        OracleSchemaManager(connection).create_schema(vector_dimensions=vector_dimensions_from_env())
        return OracleDocumentRepository(connection).store_extract_result(
            metadata,
            extract_result,
        )
    finally:
        connection.close()


def _extract_schema_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_schema = payload.get("schema") or payload.get("schema_json")
    if raw_schema is None or raw_schema == "":
        return DEFAULT_EXTRACT_SCHEMA
    if isinstance(raw_schema, dict):
        return cast(dict[str, Any], raw_schema)
    if isinstance(raw_schema, str):
        try:
            schema: object = json.loads(raw_schema)
        except json.JSONDecodeError as exc:
            raise DemoError("schema must be valid JSON.") from exc
        if isinstance(schema, dict):
            return cast(dict[str, Any], schema)
    raise DemoError("schema must be a JSON object.")


def _schema_field_count(schema: dict[str, Any]) -> int:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        return len(cast(dict[object, object], properties))
    return len(schema)


def _metadata_from_payload(
    payload: dict[str, Any] | dict[str, str],
    *,
    source_uri: str,
    source_kind: str,
) -> DocumentMetadata:
    return DocumentMetadata(
        source_uri=source_uri,
        source_kind="file" if source_kind == "file" else "url",
        company=_optional_str(payload.get("company")),
        fiscal_year=_optional_int(payload.get("fiscal_year") or payload.get("year")),
        filing_type=_optional_str(payload.get("filing_type")) or "10-K",
        title=_optional_str(payload.get("title")),
    )


def _filters_from_payload(payload: dict[str, Any]) -> SearchFilters:
    return SearchFilters(
        company=_optional_str(payload.get("company")),
        fiscal_year=_optional_int(payload.get("fiscal_year") or payload.get("year")),
        filing_type=_optional_str(payload.get("filing_type")),
        page_number=_optional_int(payload.get("page_number")),
    )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _seconds_to_ms(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value * 1000, 2)


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DemoError(f"Missing required field: {key}.")
    return value.strip()


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _positive_int(value: object, *, default: int) -> int:
    parsed = _optional_int(value)
    if parsed is None:
        return default
    return max(1, parsed)


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _multipart_boundary(content_type: str) -> bytes:
    for part in content_type.split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key.lower() == "boundary":
            return value.strip().strip('"').encode("utf-8")
    raise DemoError("Missing multipart boundary.")


def _parse_part_headers(header_blob: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in header_blob.decode("utf-8", errors="replace").split("\r\n"):
        key, separator, value = line.partition(":")
        if separator:
            headers[key.strip().lower()] = value.strip()
    return headers


def _header_params(value: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for part in value.split(";")[1:]:
        key, separator, raw_value = part.strip().partition("=")
        if separator:
            params[key.strip().lower()] = raw_value.strip().strip('"')
    return params


def _safe_filename(filename: str) -> str:
    name = Path(filename).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return name or "upload.bin"


def _public_error(exc: Exception) -> str:
    text = str(exc).strip()
    if not text:
        return type(exc).__name__
    return text.splitlines()[0]


if __name__ == "__main__":
    raise SystemExit(main())
