CREATE TABLE documents (
    document_id NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company VARCHAR2(255),
    fiscal_year NUMBER(4),
    filing_type VARCHAR2(30),
    source_uri VARCHAR2(2048),
    source_kind VARCHAR2(30),
    title VARCHAR2(500),
    reducto_job_id VARCHAR2(128),
    pdf_url VARCHAR2(2048),
    studio_link VARCHAR2(2048),
    raw_reducto_output JSON,
    created_at TIMESTAMP DEFAULT SYSTIMESTAMP,
    CONSTRAINT documents_reducto_job_uk UNIQUE (reducto_job_id)
);

CREATE TABLE document_extractions (
    extraction_id NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id NUMBER NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    reducto_job_id VARCHAR2(128),
    schema_json JSON,
    extracted_json JSON,
    raw_reducto_output JSON,
    citations_enabled NUMBER(1) DEFAULT 1 NOT NULL,
    studio_link VARCHAR2(2048),
    created_at TIMESTAMP DEFAULT SYSTIMESTAMP
);

CREATE TABLE document_chunks (
    chunk_id NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id NUMBER NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    chunk_index NUMBER NOT NULL,
    content CLOB,
    embedding_text CLOB,
    page_start NUMBER,
    page_end NUMBER,
    block_count NUMBER,
    block_metadata JSON,
    embedding VECTOR(384, FLOAT32),
    created_at TIMESTAMP DEFAULT SYSTIMESTAMP,
    CONSTRAINT document_chunks_doc_idx_uk UNIQUE (document_id, chunk_index)
);

CREATE TABLE parsed_tables (
    table_id NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id NUMBER NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    chunk_id NUMBER REFERENCES document_chunks(chunk_id) ON DELETE SET NULL,
    table_index NUMBER NOT NULL,
    chunk_index NUMBER NOT NULL,
    block_index NUMBER NOT NULL,
    page_number NUMBER,
    raw_content CLOB,
    rows_json JSON,
    metadata JSON,
    created_at TIMESTAMP DEFAULT SYSTIMESTAMP,
    CONSTRAINT parsed_tables_doc_idx_uk UNIQUE (document_id, table_index)
);

CREATE TABLE financial_facts (
    fact_id NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id NUMBER NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    table_id NUMBER REFERENCES parsed_tables(table_id) ON DELETE SET NULL,
    source_chunk_id NUMBER REFERENCES document_chunks(chunk_id) ON DELETE SET NULL,
    metric VARCHAR2(500) NOT NULL,
    period_label VARCHAR2(255),
    value NUMBER,
    raw_value VARCHAR2(4000),
    unit VARCHAR2(50),
    currency VARCHAR2(20),
    scale VARCHAR2(30),
    row_index NUMBER,
    column_index NUMBER,
    page_number NUMBER,
    raw_row JSON,
    created_at TIMESTAMP DEFAULT SYSTIMESTAMP
);

CREATE INDEX documents_company_year_idx ON documents(company, fiscal_year, filing_type);
CREATE INDEX chunks_doc_page_idx ON document_chunks(document_id, page_start, page_end);
CREATE INDEX facts_doc_metric_idx ON financial_facts(document_id, metric);
CREATE INDEX extractions_doc_idx ON document_extractions(document_id);
CREATE INDEX extractions_job_idx ON document_extractions(reducto_job_id);

-- Optional full-text index for `reducto-oracledb search --mode hybrid`.
-- CREATE INDEX document_chunks_text_idx
-- ON document_chunks(content)
-- INDEXTYPE IS CTXSYS.CONTEXT;
