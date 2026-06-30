# Developer Cookbook: Reducto + OracleDB

This cookbook is a practical recipe book for building and testing against the
Reducto OracleDB starter kit.

![Executive thesis: Reducto AI + Oracle Database](../notebooks/assets/reducto_oracle_executive_thesis.svg)

## 1. Local Setup

```bash
./scripts/bootstrap
source .venv/bin/activate
```

Run these commands from the `reducto-python-sdk` repository root. The bootstrap
command installs all SDK features, including the optional OracleDB dependency.

Load environment variables:

```bash
cp examples/oracledb/.env.example examples/oracledb/.env
set -a
source examples/oracledb/.env
set +a
```

Required `.env` values:

```bash
REDUCTO_API_KEY=...
ORACLE_USER=REDUCTO_RAG_APP
ORACLE_PASSWORD=...
ORACLE_DSN=localhost:1521/FREEPDB1
ORACLE_VECTOR_DIMENSIONS=2048

EMBEDDING_PROVIDER=oracle
ORACLE_LLM_API_KEY=...
ORACLE_LLM_BASE_URL=https://dbdevllms.oraclecorp.com
ORACLE_LLM_EMBED_MODEL=nim/llama-3.2-nv-embedqa-1b-v2
ORACLE_LLM_EMBED_MAX_CHARS=16000

# Optional fallback/alternate provider:
CO_API_KEY=...
COHERE_EMBED_MODEL=embed-english-light-v3.0
SEC_USER_AGENT="reducto-oracledb your-email@example.com"
```

## 2. Check Oracle Connectivity

```bash
rye run python -c 'import os,oracledb; c=oracledb.connect(user=os.environ["ORACLE_USER"],password=os.environ["ORACLE_PASSWORD"],dsn=os.environ["ORACLE_DSN"]); print("connected"); c.close()'
```

Initialize or migrate schema:

```bash
rye run reducto-oracledb init-db
```

Inspect row counts:

```bash
rye run python - <<'PY'
import os, oracledb

conn = oracledb.connect(
    user=os.environ["ORACLE_USER"],
    password=os.environ["ORACLE_PASSWORD"],
    dsn=os.environ["ORACLE_DSN"],
)
cur = conn.cursor()
for table in [
    "DOCUMENTS",
    "DOCUMENT_EXTRACTIONS",
    "DOCUMENT_CHUNKS",
    "PARSED_TABLES",
    "FINANCIAL_FACTS",
]:
    cur.execute(f"select count(*) from {table}")
    print(table, cur.fetchone()[0])
conn.close()
PY
```

## 3. Ingest a SEC Filing URL

Apple:

```bash
rye run reducto-oracledb ingest \
  --url "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20230930.htm" \
  --company AAPL \
  --year 2023 \
  --filing-type 10-K \
  --title "Apple 2023 Form 10-K"
```

Microsoft:

```bash
rye run reducto-oracledb ingest \
  --url "https://www.sec.gov/Archives/edgar/data/789019/000095017023035122/msft-20230630.htm" \
  --company MSFT \
  --year 2023 \
  --filing-type 10-K \
  --title "Microsoft 2023 Form 10-K"
```

NVIDIA:

```bash
rye run reducto-oracledb ingest \
  --url "https://www.sec.gov/Archives/edgar/data/1045810/000104581024000029/nvda-20240128.htm" \
  --company NVDA \
  --year 2024 \
  --filing-type 10-K \
  --title "NVIDIA 2024 Form 10-K"
```

## 4. Upload a Local PDF or HTML File

```bash
rye run reducto-oracledb ingest \
  --file ./filings/example.pdf \
  --company ACME \
  --year 2024 \
  --filing-type 10-K \
  --title "ACME 2024 Form 10-K"
```

PDF upload path:

```text
local PDF -> Reducto upload -> Reducto parse -> normalize -> Oracle store -> Q&A
```

## 5. Extract Typed JSON With Reducto

Use Extract when you know the fields you want back as typed JSON. This path does
not create chunks or embeddings; it calls Reducto Extract, normalizes the
response, and stores the schema plus extracted JSON in Oracle.

Create a schema file:

```bash
mkdir -p schemas
cat > schemas/financial_extract_schema.json <<'JSON'
{
  "type": "object",
  "properties": {
    "company_name": { "type": "string" },
    "fiscal_year": { "type": "integer" },
    "filing_type": { "type": "string" },
    "total_net_sales_millions": { "type": "number" },
    "net_income_millions": { "type": "number" },
    "auditor_name": { "type": "string" },
    "business_summary": { "type": "string" }
  },
  "required": ["company_name", "fiscal_year", "filing_type"]
}
JSON
```

Extract from a URL:

```bash
rye run reducto-oracledb ingest \
  --mode extract \
  --url "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20230930.htm" \
  --schema-file schemas/financial_extract_schema.json \
  --company AAPL \
  --year 2023 \
  --filing-type 10-K \
  --title "Apple 2023 Form 10-K Extract" \
  --extract-system-prompt "Extract audited annual filing fields."
```

Extract from a local file:

```bash
rye run reducto-oracledb ingest \
  --mode extract \
  --file ./filings/example.pdf \
  --schema-file schemas/financial_extract_schema.json \
  --company ACME \
  --year 2024 \
  --filing-type 10-K \
  --title "ACME 2024 Form 10-K Extract"
```

The core implementation path is:

```text
ReductoDocumentParser.extract_url / extract_file
  -> client.extract.run(...)
  -> normalize_extract_response
  -> OracleDocumentRepository.store_extract_result
  -> DOCUMENTS + DOCUMENT_EXTRACTIONS
```

Useful flags:

- `--deep-extract` enables Reducto Deep Extract.
- `--no-citations` stores plain extracted values without citation wrappers.
- `--no-numerical-confidence` disables numeric citation confidence scores.
- `--agentic-tables` asks Reducto to preserve table structure more carefully.

Inspect the latest extracted JSON:

```bash
rye run python - <<'PY'
import os, oracledb

conn = oracledb.connect(
    user=os.environ["ORACLE_USER"],
    password=os.environ["ORACLE_PASSWORD"],
    dsn=os.environ["ORACLE_DSN"],
)
cur = conn.cursor()
cur.execute("""
select e.extraction_id,
       d.company,
       d.fiscal_year,
       e.reducto_job_id,
       json_serialize(e.extracted_json returning clob pretty)
from document_extractions e
join documents d on d.document_id = e.document_id
order by e.created_at desc
fetch first 1 row only
""")
row = cur.fetchone()
if row:
    print("extraction_id:", row[0])
    print("company:", row[1])
    print("fiscal_year:", row[2])
    print("reducto_job_id:", row[3])
    print(row[4].read() if hasattr(row[4], "read") else row[4])
conn.close()
PY
```

Extract requires `REDUCTO_API_KEY` and Oracle connection variables. It does not
require an embedding provider unless you also run the parse/RAG workflows.

For parse/RAG workflows, use Oracle embeddings:

```bash
EMBEDDING_PROVIDER=oracle
ORACLE_LLM_API_KEY=...
ORACLE_LLM_EMBED_MODEL=nim/llama-3.2-nv-embedqa-1b-v2
ORACLE_LLM_EMBED_MAX_CHARS=16000
```

The default Oracle embedding model returns 2048-dimensional vectors. If you are
switching an existing 384-dimensional Cohere demo database to Oracle embeddings,
migrate or recreate `DOCUMENT_CHUNKS.embedding` and re-embed the chunks.
The max-char cap prevents oversized parsed chunks from being rejected by the
embedding endpoint; the full chunk text is still stored in Oracle.

Or switch back to Cohere without code changes:

```bash
EMBEDDING_PROVIDER=cohere
CO_API_KEY=...
COHERE_EMBED_MODEL=embed-english-light-v3.0
```

## 6. Ask Polished Questions

Use `ask` for human-friendly output:

```bash
rye run reducto-oracledb ask "What were Apple's net sales in 2023?" \
  --company AAPL \
  --year 2023
```

Driver-style question:

```bash
rye run reducto-oracledb ask "What drove revenue growth?" \
  --company AAPL \
  --year 2023
```

Microsoft cloud question:

```bash
rye run reducto-oracledb ask "What drove cloud revenue growth?" \
  --company MSFT \
  --year 2023
```

## 7. Debug Retrieval

Use `search` when you want raw-ish retrieval output:

```bash
rye run reducto-oracledb search "net sales" --company AAPL --year 2023 --limit 3
```

Use `facts` when you want promoted financial rows:

```bash
rye run reducto-oracledb facts --metric "revenue" --company MSFT --year 2023 --limit 10
```

## 8. Run the Demo Website

```bash
rye run python examples/oracledb/demo/app.py --host 127.0.0.1 --port 8767
```

Open:

```text
http://127.0.0.1:8767
```

Use the browser to:

- ingest URL documents,
- upload PDFs or saved HTML files,
- see Oracle counts,
- inspect stored documents,
- ask polished questions,
- view evidence snippets and source URLs.

## 9. Demo API Recipes

Status:

```bash
curl -sS http://127.0.0.1:8767/api/status
```

Ask:

```bash
curl -sS -X POST http://127.0.0.1:8767/api/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"What were Apple'\''s net sales in 2023?","company":"AAPL","fiscal_year":2023}'
```

URL ingest:

```bash
curl -sS -X POST http://127.0.0.1:8767/api/ingest/url \
  -H 'Content-Type: application/json' \
  -d '{
    "url":"https://www.sec.gov/Archives/edgar/data/1045810/000104581024000029/nvda-20240128.htm",
    "company":"NVDA",
    "fiscal_year":2024,
    "filing_type":"10-K",
    "title":"NVIDIA 2024 Form 10-K"
  }'
```

File upload:

```bash
curl -sS -X POST http://127.0.0.1:8767/api/ingest/file \
  -F file=@./filings/example.pdf \
  -F company=ACME \
  -F year=2024 \
  -F filing_type=10-K \
  -F title="ACME 2024 Form 10-K"
```

## 10. Remove Duplicate Documents

Preview the Tesla cleanup helper without deleting anything:

```bash
rye run python examples/oracledb/scripts/cleanup_tesla_duplicates.py
```

After reviewing the listed IDs, apply the cleanup explicitly:

```bash
rye run python examples/oracledb/scripts/cleanup_tesla_duplicates.py --apply
```

List documents:

```bash
rye run python - <<'PY'
import os, oracledb

conn = oracledb.connect(
    user=os.environ["ORACLE_USER"],
    password=os.environ["ORACLE_PASSWORD"],
    dsn=os.environ["ORACLE_DSN"],
)
cur = conn.cursor()
cur.execute("""
select document_id, company, fiscal_year, title, source_uri, created_at
from documents
order by created_at desc
""")
for row in cur.fetchall():
    print(row)
conn.close()
PY
```

Delete one duplicate document. Chunks, tables, and facts cascade:

```bash
rye run python - <<'PY'
import os, oracledb

DOCUMENT_ID_TO_DELETE = 21

conn = oracledb.connect(
    user=os.environ["ORACLE_USER"],
    password=os.environ["ORACLE_PASSWORD"],
    dsn=os.environ["ORACLE_DSN"],
)
cur = conn.cursor()
cur.execute("delete from documents where document_id = :document_id", document_id=DOCUMENT_ID_TO_DELETE)
print("deleted", cur.rowcount)
conn.commit()
conn.close()
PY
```

## 11. Evaluate Q&A Quality

Run the evaluation notebook:

```bash
rye run jupyter nbconvert --to notebook --execute \
  examples/oracledb/notebooks/evaluation_metrics_notebook.ipynb
```

Or run a tiny inline evaluation:

```bash
rye run python - <<'PY'
import os
import oracledb

from reducto.lib.oracledb.embeddings import embedding_provider_from_env
from reducto.lib.oracledb.models import SearchFilters
from reducto.lib.oracledb.qa import answer_from_search_results
from reducto.lib.oracledb.retrieval import OracleHybridRetriever

conn = oracledb.connect(
    user=os.environ["ORACLE_USER"],
    password=os.environ["ORACLE_PASSWORD"],
    dsn=os.environ["ORACLE_DSN"],
)
retriever = OracleHybridRetriever(conn, embedding_provider_from_env(input_type="search_query"))
question = "What drove cloud revenue growth?"
results = retriever.semantic_search(question, filters=SearchFilters(company="MSFT", fiscal_year=2023))
answer = answer_from_search_results(question, results)
print(answer.answer)
conn.close()
PY
```

## 12. Common Failures

### Reducto cannot fetch SEC URL

Set:

```bash
SEC_USER_AGENT="reducto-oracledb your-real-email@example.com"
```

### Ingest takes a long time

This is normal for large 10-K HTML/PDF files. Reducto parsing plus Oracle storage
can take minutes for large documents.

### Ugly table fragments in answers

The Q&A layer penalizes spaced SEC table artifacts and favors prose. If a new
document still produces ugly output, add a regression test in
`tests/lib/oracledb/test_qa.py` with the bad snippet and update
`src/reducto/lib/oracledb/qa.py` scoring.

### Duplicate documents

Delete the older duplicate from `DOCUMENTS`. Related rows cascade.

## 13. Development Loop

```bash
./scripts/test tests/lib/oracledb tests/examples/oracledb
rye run ruff check src/reducto/lib/oracledb examples/oracledb \
  tests/lib/oracledb tests/examples/oracledb
rye run ruff format --check src/reducto/lib/oracledb examples/oracledb \
  tests/lib/oracledb tests/examples/oracledb
```

Run the live Oracle + Reducto + embedding-provider E2E test only when the real
services and credentials are available:

```bash
RUN_E2E_INTEGRATION=1 rye run pytest tests/e2e/test_oracledb.py -m integration
```

Restart the demo after Python code changes:

```bash
rye run python examples/oracledb/demo/app.py --host 127.0.0.1 --port 8767
```
