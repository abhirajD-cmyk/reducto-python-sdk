# Reducto Extract Metadata Lineage

This document describes the active structured-field integration path:
Reducto `/extract` through the Extract API, followed by Oracle JSON storage.

The parse/RAG path still exists for chunks, vectors, promoted table facts, and
evidence-backed Q&A. It is separate from the `/extract` path described here.

## Active Flow

```text
Browser demo or CLI
  -> /api/extract/url or reducto-oracledb ingest --mode extract
  -> ReductoDocumentParser.extract_url / extract_file
  -> client.extract.run(...)
  -> normalize_extract_response
  -> OracleDocumentRepository.store_extract_result
  -> DOCUMENTS + DOCUMENT_EXTRACTIONS
  -> typed JSON returned to caller
```

## Extract API Request Shape

The wrapper calls the Reducto Extract API through the official SDK:

```python
response = client.extract.run(
    input=input_value,
    instructions={
        "schema": schema,
        "system_prompt": system_prompt,
    },
    parsing=financial_extract_parse_options(...),
    settings={
        "citations": {
            "enabled": citations_enabled,
            "numerical_confidence": numerical_confidence,
        },
        "deep_extract": deep_extract,
    },
)
```

Implementation location:

- `src/reducto/lib/oracledb/reducto_client.py`
- `ReductoDocumentParser.extract_url`
- `ReductoDocumentParser.extract_file`
- `ReductoDocumentParser._extract`

The `instructions.schema` object is the contract for the returned JSON. The
demo exposes this schema in a textarea and sends it to `/api/extract/url`.

## Extract API Response Shape

The wrapper expects a Reducto response with a top-level `result` field. The
normalized object keeps:

| Field | Meaning |
|---|---|
| `job_id` | Reducto job identifier, when returned. |
| `studio_link` | Link to the Reducto Studio job, when returned. |
| `raw_response` | Complete Reducto Extract API response. |
| `schema_json` | Exact schema supplied in `instructions.schema`. |
| `extracted_json` | Typed JSON returned by Reducto under `result`. |
| `citations_enabled` | Whether citation wrappers were requested. |

When citations are enabled, extracted field values can include citation objects
with source content, page, bounding box, confidence, and related metadata. The
repository stores those citation wrappers as part of `extracted_json`; it does
not flatten them or rewrite the field values.

## Oracle Storage

The Extract API integration writes two tables.

| Table | Columns used by `/extract` | Purpose |
|---|---|---|
| `DOCUMENTS` | `document_id`, metadata columns, `reducto_job_id`, `studio_link`, `raw_reducto_output` | One document record plus full raw Reducto response for traceability. |
| `DOCUMENT_EXTRACTIONS` | `extraction_id`, `document_id`, `reducto_job_id`, `schema_json`, `extracted_json`, `raw_reducto_output`, `citations_enabled`, `studio_link` | One extraction record containing the schema, typed JSON result, raw response, and citation setting. |

Runtime storage method:

```python
OracleDocumentRepository(connection).store_extract_result(metadata, extract_result)
```

The method returns `(document_id, extraction_id)`.

Reference DDL:

- `examples/oracledb/sql/schema.sql`
- `src/reducto/lib/oracledb/oracle.py`

## Demo Proof Contract

The browser demo endpoint is:

```text
POST /api/extract/url
```

The response intentionally includes proof fields:

| Response field | Proof |
|---|---|
| `route` | Confirms the browser called `/api/extract/url`. |
| `backend_api` | States `Reducto Extract API`. |
| `reducto_endpoint` | States the Reducto backend route: `/extract`. |
| `sdk_call` | States `client.extract.run`. |
| `request_body` | Shows the effective Extract SDK request captured by the wrapper, including `input`, `instructions`, `parsing`, and `settings`. |
| `extracted_json` | Shows the typed JSON returned from Reducto. |
| `document_id` | Shows the Oracle `DOCUMENTS` row created. |
| `extraction_id` | Shows the Oracle `DOCUMENT_EXTRACTIONS` row created. |

This is the quickest way to prove the Extract API is integrated end to end:
browser request, backend route, Reducto SDK call, Oracle JSON write, and response
rendering are all visible in one run.

The same contract is captured as reusable demo artifacts:

- `examples/oracledb/demo/extract_api_request.json`
- `examples/oracledb/demo/extract_api_response.example.json`

## Optional Parse/RAG Path

The parse path remains available for retrieval use cases:

```text
client.parse.run(...)
  -> normalize_parse_response
  -> DOCUMENT_CHUNKS, PARSED_TABLES, FINANCIAL_FACTS
  -> VECTOR search / hybrid search / extractive Q&A
```

Use parse when the product needs chunk retrieval, table promotion, or
evidence-backed Q&A over document spans. Use Extract API when the product needs
known fields as typed JSON.
