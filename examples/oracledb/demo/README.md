# Reducto Extract API Demo

Run from the repository root:

```bash
set -a
source examples/oracledb/.env
set +a
rye run python examples/oracledb/demo/app.py
```

Open the URL printed by the server. The first workflow in the UI is the active
Extract API integration:

```text
Browser
  -> POST /api/extract/url
  -> ReductoDocumentParser.extract_url
  -> client.extract.run(...)
  -> OracleDocumentRepository.store_extract_result
  -> DOCUMENTS + DOCUMENT_EXTRACTIONS
  -> typed JSON response rendered in the page
```

## Endpoint

```http
POST /api/extract/url
Content-Type: application/json
```

Ready-to-run request body:

- `examples/oracledb/demo/extract_api_request.json`

Run it from the repository root after starting the demo server:

```bash
curl -sS \
  -X POST http://127.0.0.1:8765/api/extract/url \
  -H "Content-Type: application/json" \
  --data @examples/oracledb/demo/extract_api_request.json
```

Example body:

```json
{
  "url": "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20230930.htm",
  "company": "AAPL",
  "year": "2023",
  "filing_type": "10-K",
  "system_prompt": "Extract audited annual filing fields.",
  "schema": {
    "type": "object",
    "properties": {
      "company_name": { "type": "string" },
      "fiscal_year": { "type": "integer" },
      "filing_type": { "type": "string" },
      "total_net_sales_millions": { "type": "number" }
    },
    "required": ["company_name", "fiscal_year", "filing_type"]
  }
}
```

The response includes proof that the Extract API path ran:

| Field | Meaning |
|---|---|
| `route` | `/api/extract/url` |
| `backend_api` | `Reducto Extract API` |
| `reducto_endpoint` | `/extract` |
| `sdk_call` | `client.extract.run` |
| `request_body` | Effective Extract SDK request captured by the wrapper, including `input`, `instructions`, `parsing`, and `settings` |
| `extracted_json` | Typed JSON returned by Reducto |
| `document_id` | Oracle `DOCUMENTS` row |
| `extraction_id` | Oracle `DOCUMENT_EXTRACTIONS` row |

The response contract is captured in
`examples/oracledb/demo/extract_api_response.example.json`.

The demo still includes the parse/RAG ingest and ask panels for chunk retrieval
and evidence-backed Q&A, but those are companion workflows. The structured-field
integration demonstrated here is `/api/extract/url` backed by Reducto Extract
API.
