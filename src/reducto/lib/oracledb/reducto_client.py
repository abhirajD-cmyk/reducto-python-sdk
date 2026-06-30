from __future__ import annotations

import os
import tempfile
from typing import Any
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from .utils import as_dict, to_plain_data
from .config import reducto_environment
from .models import JsonValue, NormalizedParseResult, NormalizedExtractResult
from .normalizer import normalize_parse_response


def financial_parse_options(
    *,
    force_url_result: bool = False,
    agentic_tables: bool = False,
) -> dict[str, Any]:
    enhance: dict[str, Any] = {
        "intelligent_ordering": True,
        "summarize_figures": False,
    }
    if agentic_tables:
        enhance["agentic"] = [
            {
                "scope": "table",
                "prompt": (
                    "Preserve financial statement table structure, row labels, "
                    "period headers, footnotes, and numeric values."
                ),
            }
        ]

    return {
        "enhance": enhance,
        "formatting": {
            "add_page_markers": True,
            "merge_tables": True,
            "table_output_format": "csv",
        },
        "retrieval": {
            "embedding_optimized": True,
            "chunking": {
                "chunk_mode": "section",
                "chunk_overlap": 150,
            },
        },
        "settings": {
            "force_url_result": force_url_result,
            "return_ocr_data": False,
        },
    }


def financial_extract_parse_options(
    *,
    agentic_tables: bool = False,
) -> dict[str, Any]:
    options = financial_parse_options(agentic_tables=agentic_tables)
    return {
        "enhance": options["enhance"],
        "formatting": options["formatting"],
        "settings": {
            "return_ocr_data": False,
        },
    }


class ReductoDocumentParser:
    def __init__(self, client: Any | None = None, http_client: httpx.Client | None = None) -> None:
        self.client = client or self._build_default_client()
        self.http_client = http_client or httpx.Client(timeout=120)

    def parse_url(
        self,
        url: str,
        *,
        force_url_result: bool = False,
        agentic_tables: bool = False,
    ) -> NormalizedParseResult:
        try:
            return self._parse(
                url,
                force_url_result=force_url_result,
                agentic_tables=agentic_tables,
            )
        except Exception as exc:
            if not _is_source_download_failure(exc):
                raise

        with tempfile.TemporaryDirectory(prefix="reducto-oracledb-") as temp_dir:
            downloaded_path = self._download_source_url(url, Path(temp_dir))
            return self.parse_file(
                downloaded_path,
                force_url_result=force_url_result,
                agentic_tables=agentic_tables,
            )

    def parse_file(
        self,
        path: str | Path,
        *,
        force_url_result: bool = False,
        agentic_tables: bool = False,
    ) -> NormalizedParseResult:
        document_path = Path(path)
        upload = self.client.upload(
            file=document_path,
            extension=_upload_extension(document_path),
        )
        return self._parse(
            upload,
            force_url_result=force_url_result,
            agentic_tables=agentic_tables,
        )

    def extract_url(
        self,
        url: str,
        *,
        schema: dict[str, Any],
        citations: bool = True,
        numerical_confidence: bool = True,
        deep_extract: bool = False,
        system_prompt: str | None = None,
        agentic_tables: bool = False,
    ) -> NormalizedExtractResult:
        try:
            return self._extract(
                url,
                schema=schema,
                citations=citations,
                numerical_confidence=numerical_confidence,
                deep_extract=deep_extract,
                system_prompt=system_prompt,
                agentic_tables=agentic_tables,
            )
        except Exception as exc:
            if not _is_source_download_failure(exc):
                raise

        with tempfile.TemporaryDirectory(prefix="reducto-oracledb-") as temp_dir:
            downloaded_path = self._download_source_url(url, Path(temp_dir))
            return self.extract_file(
                downloaded_path,
                schema=schema,
                citations=citations,
                numerical_confidence=numerical_confidence,
                deep_extract=deep_extract,
                system_prompt=system_prompt,
                agentic_tables=agentic_tables,
            )

    def extract_file(
        self,
        path: str | Path,
        *,
        schema: dict[str, Any],
        citations: bool = True,
        numerical_confidence: bool = True,
        deep_extract: bool = False,
        system_prompt: str | None = None,
        agentic_tables: bool = False,
    ) -> NormalizedExtractResult:
        document_path = Path(path)
        upload = self.client.upload(
            file=document_path,
            extension=_upload_extension(document_path),
        )
        return self._extract(
            upload,
            schema=schema,
            citations=citations,
            numerical_confidence=numerical_confidence,
            deep_extract=deep_extract,
            system_prompt=system_prompt,
            agentic_tables=agentic_tables,
        )

    def _parse(
        self,
        input_value: object,
        *,
        force_url_result: bool,
        agentic_tables: bool,
    ) -> NormalizedParseResult:
        options = financial_parse_options(
            force_url_result=force_url_result,
            agentic_tables=agentic_tables,
        )
        response = self.client.parse.run(input=input_value, **options)
        materialized = self._materialize_url_result(response)
        return normalize_parse_response(materialized)

    def _extract(
        self,
        input_value: object,
        *,
        schema: dict[str, Any],
        citations: bool,
        numerical_confidence: bool,
        deep_extract: bool,
        system_prompt: str | None,
        agentic_tables: bool,
    ) -> NormalizedExtractResult:
        schema_data = as_dict(to_plain_data(schema))
        instructions: dict[str, JsonValue] = {"schema": schema_data}
        if system_prompt:
            instructions["system_prompt"] = system_prompt

        parsing = financial_extract_parse_options(agentic_tables=agentic_tables)
        settings: dict[str, JsonValue] = {
            "citations": {
                "enabled": citations,
                "numerical_confidence": numerical_confidence,
            },
            "deep_extract": deep_extract,
        }
        request_body: dict[str, JsonValue] = {
            "input": to_plain_data(input_value),
            "instructions": instructions,
            "parsing": to_plain_data(parsing),
            "settings": settings,
        }

        response = self.client.extract.run(
            input=input_value,
            instructions=instructions,
            parsing=parsing,
            settings=settings,
        )
        return normalize_extract_response(
            response,
            schema=schema_data,
            citations_enabled=citations,
            request_body=request_body,
        )

    def _materialize_url_result(self, response: object) -> dict[str, Any]:
        data = as_dict(to_plain_data(response))
        result = data.get("result")
        if not isinstance(result, dict) or result.get("type") != "url":
            return data

        result_url = result.get("url")
        if not isinstance(result_url, str) or not result_url:
            raise ValueError("Reducto URL result did not include a result URL.")

        fetched = self.http_client.get(result_url)
        fetched.raise_for_status()
        payload = as_dict(to_plain_data(fetched.json()))
        if "result" in payload:
            return payload
        return {**data, "result": payload}

    def _download_source_url(self, url: str, directory: Path) -> Path:
        response = self.http_client.get(
            url,
            follow_redirects=True,
            headers=_source_download_headers(),
        )
        response.raise_for_status()
        path = directory / _download_filename(url, response)
        path.write_bytes(response.content)
        return path

    @staticmethod
    def _build_default_client() -> Any:
        from reducto import Reducto

        environment = reducto_environment()
        if environment:
            return Reducto(environment=environment)
        return Reducto()


def normalize_extract_response(
    response: object,
    *,
    schema: dict[str, JsonValue],
    citations_enabled: bool,
    request_body: dict[str, JsonValue] | None = None,
) -> NormalizedExtractResult:
    data = as_dict(to_plain_data(response))
    if "result" not in data:
        raise ValueError("Expected a Reducto extract result with a result payload.")

    job_id = data.get("job_id")
    studio_link = data.get("studio_link")
    duration = data.get("duration")
    return NormalizedExtractResult(
        job_id=job_id if isinstance(job_id, str) else None,
        raw_response=data,
        schema_json=schema,
        extracted_json=data["result"],
        request_body=request_body or {},
        citations_enabled=citations_enabled,
        duration_seconds=duration if isinstance(duration, (int, float)) else None,
        studio_link=studio_link if isinstance(studio_link, str) else None,
    )


def _is_source_download_failure(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "invalid_config" in text
        and "failed to download file from url" in text
        and ("403" in text or "forbidden" in text)
    )


def _source_download_headers() -> dict[str, str]:
    user_agent = os.getenv("SEC_USER_AGENT") or os.getenv("HTTP_USER_AGENT")
    if not user_agent:
        raise RuntimeError("SEC_USER_AGENT or HTTP_USER_AGENT must be set before downloading source URLs.")
    return {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml,application/pdf,*/*",
        "Accept-Encoding": "gzip, deflate",
    }


def _download_filename(url: str, response: httpx.Response) -> str:
    parsed_name = Path(unquote(urlparse(url).path)).name
    if parsed_name and "." in parsed_name:
        if parsed_name.lower().endswith((".htm", ".xhtml")):
            return f"{Path(parsed_name).stem}.html"
        return parsed_name

    content_type = response.headers.get("content-type", "").lower()
    if "pdf" in content_type:
        return "document.pdf"
    if "html" in content_type:
        return "document.html"
    return "document"


def _upload_extension(path: Path) -> str | None:
    extension = path.suffix.lstrip(".").lower()
    if not extension:
        return None
    if extension in {"htm", "xhtml"}:
        return "html"
    return extension
