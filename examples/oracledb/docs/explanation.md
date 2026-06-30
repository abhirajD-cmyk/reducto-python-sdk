# Implementation Explanation

This document explains the integration code that was added or changed during the
Oracle + Reducto + demo work. It is written as a developer walkthrough: each file
is explained by its meaningful blocks and functions.

## High-Level Changes

The project started with a CLI-oriented Reducto-to-Oracle pipeline. The added work
made it usable end-to-end:

- Created a working Oracle app schema and initialized the existing tables.
- Fixed SEC URL ingestion by downloading blocked SEC URLs locally with `SEC_USER_AGENT`.
- Fixed Reducto upload format handling for SEC `.htm` filings.
- Fixed large Oracle JSON/CLOB binds.
- Widened `financial_facts.raw_value` for long SEC table values.
- Added polished extractive Q&A with evidence snippets.
- Added `reducto-oracledb ask`.
- Added a browser demo with upload, URL ingest, architecture visualization, status, and Q&A.
- Added tests for the new behavior.

## `src/reducto/lib/oracledb/reducto_client.py`

### Imports

```python
import os
import tempfile
from urllib.parse import unquote, urlparse
```

- `os` reads `SEC_USER_AGENT` and `HTTP_USER_AGENT`.
- `tempfile` creates short-lived local files when Reducto cannot fetch a URL itself.
- `unquote` and `urlparse` derive a usable filename from the URL path.

### `ReductoDocumentParser.parse_url`

Purpose:

- First try the normal Reducto URL parse path.
- If Reducto returns a SEC-style URL download failure, fall back to local download + upload.

Behavior:

1. Calls `_parse(url, ...)`.
2. Catches exceptions.
3. Uses `_is_source_download_failure()` to decide whether the error is the known SEC 403 path.
4. If it is not that error, re-raises immediately.
5. If it is that error, creates a temporary directory.
6. Downloads the source URL using `_download_source_url()`.
7. Calls `parse_file()` on the downloaded file.
8. The temporary directory is deleted automatically when the block exits.

Why:

SEC often blocks server-side fetches from third-party services unless requests use
a contact-style User-Agent. Reducto's backend received `403`, so local fallback was
needed.

### `ReductoDocumentParser.parse_file`

Changed behavior:

```python
extension=_upload_extension(document_path)
```

Instead of passing the raw suffix, this normalizes extensions:

- `.pdf` stays `pdf`.
- `.html` stays `html`.
- `.htm` and `.xhtml` become `html`.

Why:

Reducto returned `415 DOCUMENT_CORRUPT` when uploaded SEC filings were sent as
`htm`. Normalizing to `html` fixes that path.

### `_download_source_url`

Purpose:

- Fetch the original URL from the local machine.
- Use SEC-friendly headers.
- Save the body to a temporary file.

Important lines:

```python
response = self.http_client.get(url, follow_redirects=True, headers=_source_download_headers())
```

This follows SEC redirects and identifies the client.

```python
path.write_bytes(response.content)
```

This writes the downloaded document so Reducto can receive it as an upload.

### `_is_source_download_failure`

Purpose:

- Detect only the specific Reducto error that should trigger local fallback.

It checks for:

- `INVALID_CONFIG`
- `Failed to download file from URL`
- `403` or `forbidden`

Why:

The fallback should not hide unrelated Reducto errors, authentication failures, or
invalid documents.

### `_source_download_headers`

Purpose:

- Build headers for local source downloads.

It checks:

1. `SEC_USER_AGENT`
2. `HTTP_USER_AGENT`
3. fallback: `reducto-oracledb/0.1 contact@example.com`

Why:

SEC accepts contact-style User-Agent strings and rejects generic/bot-like traffic
more often.

### `_download_filename`

Purpose:

- Infer a temporary filename from URL or response content-type.

Special handling:

- URL ending in `.htm` or `.xhtml` becomes `.html`.
- PDF content becomes `document.pdf`.
- HTML content becomes `document.html`.

### `_upload_extension`

Purpose:

- Tell Reducto the correct document extension.

This prevents `.htm` uploads from being treated as unsupported/corrupt files.

## `src/reducto/lib/oracledb/oracle.py`

### Schema Migration Addition

```python
self._resize_varchar_column_if_smaller(FACTS_TABLE, "RAW_VALUE", 4000)
```

Purpose:

- Existing databases created with `raw_value VARCHAR2(255)` are upgraded to
  `VARCHAR2(4000)`.

Why:

Some SEC table cells produce raw values longer than 255 characters.

### `_resize_varchar_column_if_smaller`

Purpose:

- Query `USER_TAB_COLUMNS`.
- Check column type and length.
- Run `ALTER TABLE ... MODIFY ...` only if the column is too small.

Why:

`init-db` should be safe to re-run and should repair compatible old schemas.

### `_set_clob_inputs`

Purpose:

- Explicitly bind large CLOB/JSON string inputs as `DB_TYPE_CLOB`.

Why:

Large SEC filings produced one very large Reducto chunk. Without CLOB input sizing,
the Oracle thin driver confused bind positions and raised:

```text
ORA-40441: JSON syntax error
JZN-00078: Invalid JSON keyword 'START'
```

The fix was verified with synthetic large content: it failed without CLOB sizing
and succeeded with CLOB sizing.

### Insert Methods

`_insert_document`:

- Now calls `_set_clob_inputs(..., "raw_reducto_output")`.
- Stores full Reducto output in Oracle JSON.

`_insert_chunks`:

- Now calls `_set_clob_inputs(..., "content", "embedding_text", "block_metadata")`.
- Stores large document text, embedding text, metadata JSON, and vector.

`_insert_tables`:

- Now calls `_set_clob_inputs(..., "raw_content", "rows_json", "metadata")`.
- Stores raw table text and normalized rows.

`_insert_facts`:

- Now calls `_set_clob_inputs(..., "raw_row")`.
- Inserts promoted facts with raw row JSON.

## `examples/oracledb/sql/schema.sql`

Changed:

```sql
raw_value VARCHAR2(4000)
```

Why:

The reference schema should match the runtime schema manager. Long SEC table
values broke the original `VARCHAR2(255)` limit.

## `src/reducto/lib/oracledb/retrieval.py`

### Hybrid Search Oracle Text Error

Observed error from hybrid search:

```text
ORA-29902: Error while processing the ODCIINDEXSTART routine for index
"REDUCTO_RAG_APP"."DOCUMENT_CHUNKS_TEXT_IDX".
ORA-30600: Oracle Text error
DRG-50901: text query parser syntax error on line 1, column 28
```

Diagnosis:

- Oracle was running and reachable; the failure happened inside Oracle Text.
- The text index was required for hybrid search and was created with
  `init-db --text-index`.
- After the index existed, natural punctuation in a question such as
  `What were revenues in 2023?` still caused the Oracle Text parser to fail.

### `_escape_text_query`

Changed:

```python
_TEXT_QUERY_TERM_RE = re.compile(r"[A-Za-z0-9]+")
```

Purpose:

- Extract only plain alphanumeric terms from the user's question.
- Drop punctuation such as `?`, `/`, apostrophes, and periods before passing the
  string into Oracle Text `CONTAINS(...)`.
- Limit the Oracle Text side to the first 20 terms.

Why:

Oracle Text does not treat `CONTAINS(c.content, :text_query, 1)` as a normal
free-form sentence search. It parses `:text_query` as Oracle Text query syntax.
User-friendly questions can therefore be invalid Oracle Text syntax unless the
text query is cleaned first.

### `hybrid_search`

Changed behavior:

- Builds the sanitized text query once.
- Falls back to semantic search if the question has no alphanumeric terms.
- Uses the sanitized string for the Oracle Text candidate branch.

Why:

Hybrid search should tolerate normal user questions from the CLI and browser demo
instead of surfacing Oracle Text parser errors.

## `src/reducto/lib/oracledb/normalizer.py`

Changed:

```python
"enriched": chunk.get("enriched")
```

was removed from chunk metadata.

Why:

The enriched chunk text is already stored in `embedding_text`. Duplicating it in
`block_metadata` made the JSON metadata huge and increased Oracle bind pressure.

## `src/reducto/lib/oracledb/qa.py`

This file is the polished extractive Q&A layer.

### Dataclasses

`EvidenceSnippet`:

- `text`: cleaned evidence text.
- `source_uri`: source document URL or uploaded filename.
- `page_number`: inferred from Reducto page markers when possible.
- `score`: evidence ranking score.

`AnswerResult`:

- `question`: original question.
- `answer`: concise extracted answer.
- `evidence`: ranked supporting snippets.

### `answer_from_search_results`

Purpose:

- Convert raw `SearchResult` objects into a polished Q&A response.

Steps:

1. Calls `_evidence_from_results`.
2. If there is no evidence, returns a graceful no-match answer.
3. Calls `_best_answer_sentence`.
4. Returns `AnswerResult`.

### `format_answer`

Purpose:

- Render CLI output in readable sections:
  - Question
  - Answer
  - Evidence
  - Source

Why:

The original `search` command returned JSON snippets. This gives a human-friendly
terminal experience.

### `snippet_for_query`

Purpose:

- Improve the old `search` command by showing text around the query match rather
  than the first 700 characters of a giant SEC/XBRL chunk.

### `_evidence_from_results`

Purpose:

- Find multiple good evidence windows inside each retrieved chunk.

Important behavior:

- Uses `_scored_positions()` to find relevant positions.
- Builds cleaned text windows around those positions.
- Infers page number with `_page_for_position()`.
- Sorts evidence by score.

Why:

SEC HTML sometimes becomes one huge chunk. A single chunk can contain many useful
passages, so evidence must be selected inside the chunk.

### `_best_answer_sentence`

Purpose:

- Pick a concise answer from evidence snippets.

Priority order:

1. Driver-style answer for revenue/sales driver questions.
2. Exact financial answer for questions like net sales.
3. Generic highest-scoring sentence.

### `_driver_answer`

Purpose:

- Handle questions like:

```text
What drove revenue growth?
What affected sales?
Why did revenue change?
```

It prefers prose with:

- `total net sales`
- `increased` or `decreased`
- `compared to`
- `accounted for`
- `primarily`
- `offset`
- `higher` / `lower`

It penalizes:

- table fragments
- expense-only explanations when the question asks about revenue/sales

Why:

The initial Q&A layer sometimes selected noisy SEC table text such as:

```text
P e r c e n t a g e o f t o t a l ...
```

The driver-answer logic avoids that and returns prose explanations.

### `_looks_like_table_artifact`

Purpose:

- Detect SEC/XBRL table fragments.

Signals:

- repeated `,,,`
- spaced-out letters like `P e r c e n t a g e`
- high punctuation density

Why:

Reducto table output from SEC inline XBRL can be useful for facts but ugly as
Q&A prose.

### `_exact_financial_answer`

Purpose:

- For net-sales questions, prefer sentences matching:

```text
total net sales were $...
```

Why:

For Apple's 2023 filing, this returns the clean answer:

```text
The Company's total net sales were $383.3 billion...
```

### `_query_phrases`

Purpose:

- Add domain phrases to retrieval scoring.

Special behavior:

- If the question contains `revenue`, it also searches for `net sales`, because
  10-K filings often use "net sales" rather than "revenue."

### Cleaning Helpers

`_clean_text`:

- Removes page markers.
- Normalizes curly quotes.
- Collapses whitespace.

`_clean_snippet`:

- Avoids snippets starting mid-sentence when possible.

`_sentences`:

- Splits snippets into candidate sentences.

`_trim_sentence`:

- Limits answer length and removes heading noise before "The Company".

`_page_for_position`:

- Finds the most recent `[[START OF PAGE N]]` marker before a text position.

## `src/reducto/lib/oracledb/cli.py`

### New `ask` Subcommand

Added parser:

```python
ask = subparsers.add_parser("ask", help="Ask a question and print a concise answer.")
```

Arguments:

- `query`
- `--mode semantic|hybrid`
- `--company`
- `--year`
- `--filing-type`
- `--page`
- `--limit`
- `--evidence-limit`

### `_ask`

Purpose:

- Connect to Oracle.
- Build `OracleHybridRetriever`.
- Run semantic or hybrid retrieval.
- Pass results into `answer_from_search_results`.
- Print `format_answer(answer)`.

Why:

This is the polished CLI path for user-facing Q&A.

### Updated `_search`

Changed:

```python
"content": snippet_for_query(result.content, args.query)
```

Why:

The original search output showed the start of the stored chunk, which often began
with raw SEC/XBRL boilerplate. The updated output centers around the query.

## `examples/oracledb/demo/app.py`

This is the no-build web demo backend.

### Path Constants

```python
EXAMPLE_ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
```

Purpose:

- Locate the `examples/oracledb` configuration root.
- Load the example's local `.env` independently of the current working directory.
- Serve static assets from `examples/oracledb/demo/static`.

The SDK is imported as the installed `reducto` package; the demo does not
modify `sys.path`.

### Runtime Constants

```python
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY_BYTES = 120 * 1024 * 1024
```

Purpose:

- Bind locally by default.
- Try port 8765 and subsequent ports if occupied.
- Limit upload request size.

### `DemoError`

Purpose:

- Carry a user-facing error message and HTTP status code.

### `UploadedFile` and `MultipartForm`

Purpose:

- Represent file uploads without a web framework.

### `load_env`

Purpose:

- Read `.env` and populate missing environment variables.
- Does not override already-set shell variables.

Why:

The demo should work when launched directly without requiring the user to run
`source examples/oracledb/.env` first.

### `status_payload`

Purpose:

- Return readiness info for the UI.

It reports:

- Oracle user/DSN
- whether Reducto API key is set
- whether SEC User-Agent is set
- database connection status
- table counts
- recent documents

Secrets are never returned.

### `table_counts`

Purpose:

- Count rows in the four core tables for dashboard cards.

### `list_documents`

Purpose:

- Return recent stored documents with related chunk/table/fact counts.

It uses subqueries so the UI can show useful per-document stats.

### `ingest_url`

Purpose:

- Ingest a document URL from the browser.

Steps:

1. Validate URL.
2. Build `DocumentMetadata`.
3. Call `ReductoDocumentParser.parse_url`.
4. Store result with `_store_parse_result`.
5. Return document ID, Reducto job ID, counts, and elapsed seconds.

### `ingest_file`

Purpose:

- Ingest a browser-uploaded file.

Steps:

1. Validate non-empty file.
2. Create a temporary directory.
3. Sanitize filename with `_safe_filename`.
4. Write uploaded bytes.
5. Call `ReductoDocumentParser.parse_file`.
6. Store result in Oracle.
7. Return document ID and counts.

Why:

This supports PDFs and saved HTML files without keeping uploaded files on disk.

### `ask_question`

Purpose:

- Back the browser Q&A form.

Steps:

1. Validate question.
2. Connect to Oracle.
3. Build retriever.
4. Apply filters.
5. Run semantic or hybrid search.
6. Convert results to polished answer/evidence with `answer_from_search_results`.
7. Return JSON to the browser.

### `parse_multipart_form`

Purpose:

- Parse browser `multipart/form-data` uploads.

Why:

Avoids adding Flask/FastAPI just for the demo.

### `DemoHandler`

This is the HTTP request handler.

`do_GET` routes:

- `/` -> `index.html`
- `/static/...` -> CSS/JS assets
- `/api/status` -> readiness payload
- `/api/documents` -> document list

`do_POST` routes:

- `/api/ask`
- `/api/ingest/url`
- `/api/ingest/file`

`_read_body`:

- Reads request bytes and enforces `MAX_BODY_BYTES`.

`_read_json`:

- Parses JSON request bodies.

`_send_json`:

- Serializes dictionaries as JSON responses.

`_send_static`:

- Serves static files only inside `examples/oracledb/demo/static`.

`_handle_exception`:

- Converts exceptions into JSON error responses.

### `run`, `main`, `_make_server`

Purpose:

- Load `.env`.
- Start a threaded local HTTP server.
- Auto-pick the next port if the requested one is occupied.

Example:

```bash
rye run python examples/oracledb/demo/app.py --host 127.0.0.1 --port 8767
```

### Utility Helpers

`_store_parse_result`:

- Creates/migrates schema.
- Stores Reducto parse result in Oracle.

`_metadata_from_payload`:

- Converts browser fields to `DocumentMetadata`.

`_filters_from_payload`:

- Converts browser ask filters to `SearchFilters`.

`_required_str`, `_optional_str`, `_optional_int`, `_positive_int`, `_bool`:

- Validate and normalize incoming request values.

`_multipart_boundary`, `_parse_part_headers`, `_header_params`:

- Support file uploads.

`_safe_filename`:

- Removes paths and unsafe characters from uploaded filenames.

`_public_error`:

- Returns one-line safe error messages for the UI.

## `examples/oracledb/demo/static/index.html`

Purpose:

- Define the browser UI.

Major sections:

- Top status bar for Oracle, Reducto, and SEC.
- Pipeline visualizer.
- Metrics cards.
- URL ingestion form.
- File upload ingestion form.
- Ask form.
- Answer panel.
- Stored documents list.

Important design choice:

The first screen is the actual tool, not a landing page. This keeps the demo
operational and lets a user immediately ingest or ask questions.

## `examples/oracledb/demo/static/styles.css`

Purpose:

- Provide a restrained dashboard UI.

Design choices:

- Neutral background for a work-focused tool.
- Teal for active/healthy state.
- Berry/amber accents for evidence and warnings.
- Stable grid dimensions for metrics, pipeline cards, and document rows.
- Responsive layout for smaller screens.

Important classes:

- `.topbar`: header and integration status.
- `.pipeline`, `.stage`: architecture visualizer.
- `.metrics-grid`, `.metric`: counts.
- `.workspace`: ingest + ask two-column layout.
- `.answer-panel`, `.evidence-item`: polished Q&A output.
- `.documents-list`, `.document-row`: stored document inventory.
- `.toast`: transient success/error messages.

## `examples/oracledb/demo/static/app.js`

Purpose:

- Drive all browser interactivity.

### Pipeline State

`flows` defines the visual steps:

- idle: input -> Reducto -> normalize -> Oracle -> retrieve -> answer
- ingest: input -> Reducto -> normalize -> Oracle -> index -> done
- ask: question -> vector -> evidence -> answer -> source -> done

`setFlow`, `startFlow`, `finishFlow`, and `stopFlow` animate those stages.

### API Helper

`api(path, options)`:

- Calls `fetch`.
- Parses JSON.
- Throws a readable error for non-2xx responses.

### Status Rendering

`refreshStatus` calls `/api/status`.

`renderStatus` updates:

- Oracle/Reducto/SEC pills.
- metrics cards.
- stored document list.

### Ingestion

`submitUrlIngest`:

- Reads URL form.
- Posts JSON to `/api/ingest/url`.
- Updates pipeline and document list.

`submitFileIngest`:

- Reads file upload form.
- Posts `FormData` to `/api/ingest/file`.
- Updates pipeline and document list.

### Q&A

`submitAsk`:

- Reads question/filter form.
- Posts JSON to `/api/ask`.
- Renders answer and evidence.

`renderAnswer`:

- Builds answer card.
- Builds evidence cards.
- Shows source URL.

### UX Helpers

`showToast`:

- Shows success/error notifications.

Quick question buttons:

- Fill the question textarea with common examples.

## Tests Added

### `tests/lib/oracledb/test_reducto_client.py`

Added a fallback test:

- Simulates Reducto failing to fetch a URL with 403.
- Verifies local download occurs.
- Verifies `.htm` upload extension becomes `html`.

### `tests/lib/oracledb/test_qa.py`

Added tests for:

- net-sales answer selection.
- readable formatted answer sections.
- query-centered snippets.
- avoiding spaced SEC table artifacts for driver questions.

### `tests/examples/oracledb/test_demo_app.py`

Added tests for:

- `.env` loading without overriding existing shell variables.
- multipart upload parsing.
- safe filename cleanup.

The scoped `tests/examples/oracledb/conftest.py` adds the OracleDB example root
to `sys.path`, allowing the tests to import `demo.app` without changing the
SDK's generated root-level test configuration.

## Current Runtime State

The current Oracle schema contains:

```text
DOCUMENTS 2
DOCUMENT_CHUNKS 2
PARSED_TABLES 103
FINANCIAL_FACTS 3183
```

Stored documents:

- AAPL 2023 Form 10-K
- MSFT 2023 Form 10-K

The current demo server is expected at:

```text
http://127.0.0.1:8767
```

## Verification Commands

```bash
rye run pytest tests/lib/oracledb tests/examples/oracledb
rye run ruff check src/reducto/lib/oracledb examples/oracledb \
  tests/lib/oracledb tests/examples/oracledb
rye run ruff format --check src/reducto/lib/oracledb examples/oracledb \
  tests/lib/oracledb tests/examples/oracledb
```

Live Q&A:

```bash
set -a
source examples/oracledb/.env
set +a

rye run reducto-oracledb ask "What drove revenue growth?" --company AAPL --year 2023
```
