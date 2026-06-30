const pipeline = document.querySelector("#pipeline");
const flowCaption = document.querySelector("#flowCaption");
const toast = document.querySelector("#toast");
const answerPanel = document.querySelector("#answerPanel");
const documentsList = document.querySelector("#documentsList");
const ingestMetrics = document.querySelector("#ingestMetrics");
const extractOutput = document.querySelector("#extractOutput");

let flowTimer = null;
let activeFlow = [];
let activeStage = 0;

const flows = {
  idle: [
    ["Input", "URL or upload"],
    ["Route", "/api/extract/url"],
    ["Reducto", "Extract API"],
    ["Oracle", "Store typed JSON"],
    ["Response", "Return result"],
    ["Done", "Ready"],
  ],
  extract: [
    ["Browser", "POST /api/extract/url"],
    ["Schema", "Validate JSON fields"],
    ["Reducto", "client.extract.run"],
    ["Oracle", "document_extractions"],
    ["Response", "Typed JSON"],
    ["Done", "Rendered"],
  ],
  ingest: [
    ["Input", "Document received"],
    ["Reducto", "Parse API"],
    ["Normalize", "Chunks and tables"],
    ["Oracle", "JSON, facts, vectors"],
    ["Index", "Vectors ready"],
    ["Done", "Ready to ask"],
  ],
  ask: [
    ["Question", "Filter scope"],
    ["Vector", "Oracle search"],
    ["Evidence", "Rank snippets"],
    ["Answer", "Generate response"],
    ["Source", "Attach citation"],
    ["Done", "Rendered"],
  ],
};

function setFlow(name, stageIndex = -1) {
  activeFlow = flows[name] || flows.idle;
  activeStage = stageIndex;
  pipeline.replaceChildren();
  activeFlow.forEach(([title, detail], index) => {
    const node = document.createElement("div");
    node.className = "stage";
    if (stageIndex >= 0 && index < stageIndex) node.classList.add("complete");
    if (stageIndex === index) node.classList.add("active");
    const strong = document.createElement("strong");
    strong.textContent = title;
    const span = document.createElement("span");
    span.textContent = detail;
    node.append(strong, span);
    pipeline.append(node);
  });
}

function startFlow(name, caption) {
  stopFlow();
  activeStage = 0;
  flowCaption.textContent = caption;
  setFlow(name, activeStage);
  flowTimer = window.setInterval(() => {
    activeStage = Math.min(activeStage + 1, activeFlow.length - 1);
    setFlow(name, activeStage);
  }, 1500);
}

function finishFlow(name, caption) {
  stopFlow();
  flowCaption.textContent = caption;
  setFlow(name, flows[name].length);
}

function stopFlow() {
  if (flowTimer !== null) {
    window.clearInterval(flowTimer);
    flowTimer = null;
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `Request failed with ${response.status}`);
  }
  return payload;
}

async function refreshStatus() {
  const status = await api("/api/status");
  renderStatus(status);
}

function renderStatus(status) {
  const env = status.environment || {};
  const db = status.database || {};
  const embeddingReady =
    env.embedding_provider && !String(env.embedding_provider).endsWith(":not-configured");
  updatePill("#oracleState", db.connected, db.connected ? `Oracle ${env.oracle_user}` : "Oracle offline");
  updatePill("#reductoState", env.reducto_api_key, env.reducto_api_key ? "Reducto ready" : "Reducto key missing");
  updatePill(
    "#embeddingState",
    embeddingReady,
    embeddingReady ? `Embeddings ${env.embedding_provider}` : "Embedding provider missing",
  );
  updatePill("#secState", env.sec_user_agent, env.sec_user_agent ? "SEC user-agent set" : "SEC user-agent missing");

  const counts = db.tables || {};
  setText("#documentsCount", counts.DOCUMENTS || 0);
  setText("#extractionsCount", counts.DOCUMENT_EXTRACTIONS || 0);
  setText("#chunksCount", counts.DOCUMENT_CHUNKS || 0);
  setText("#tablesCount", counts.PARSED_TABLES || 0);
  setText("#factsCount", counts.FINANCIAL_FACTS || 0);
  renderDocuments(db.documents || []);
}

function updatePill(selector, ok, text) {
  const element = document.querySelector(selector);
  element.textContent = text;
  element.classList.toggle("ok", Boolean(ok));
  element.classList.toggle("warn", !ok);
}

function setText(selector, value) {
  document.querySelector(selector).textContent = new Intl.NumberFormat().format(value);
}

function renderDocuments(documents) {
  document.querySelector("#documentsState").textContent = `${documents.length} shown`;
  documentsList.replaceChildren();
  if (!documents.length) {
    const empty = document.createElement("p");
    empty.className = "subtle";
    empty.textContent = "No documents stored yet.";
    documentsList.append(empty);
    return;
  }

  documents.forEach((doc) => {
    const row = document.createElement("article");
    row.className = "document-row";
    row.append(
      labeled(doc.title || doc.source_uri || `Document ${doc.document_id}`, `${doc.company || "Unknown"} ${doc.fiscal_year || ""}`),
      labeled("Extractions", doc.extractions),
      labeled("Chunks", doc.chunks),
      labeled("Tables", doc.tables),
      labeled("Facts", doc.financial_facts),
      labeled("Source", doc.source_uri || "Not available"),
    );
    row.addEventListener("click", () => {
      fillAskScope(doc.company, doc.fiscal_year);
      showToast(`Scoped questions to ${doc.company || "document"} ${doc.fiscal_year || ""}.`);
    });
    documentsList.append(row);
  });
}

function labeled(label, value) {
  const wrapper = document.createElement("div");
  const strong = document.createElement("strong");
  const span = document.createElement("span");
  strong.textContent = String(value || "-");
  span.textContent = label;
  wrapper.append(strong, span);
  return wrapper;
}

function fillAskScope(company, year) {
  const form = document.querySelector("#askForm");
  if (company) form.elements.company.value = company;
  if (year) form.elements.year.value = year;
}

function payloadFromForm(form) {
  const data = new FormData(form);
  const payload = {};
  for (const [key, value] of data.entries()) {
    if (value instanceof File) continue;
    payload[key] = String(value).trim();
  }
  if (payload.year) payload.fiscal_year = Number(payload.year);
  return payload;
}

async function submitUrlIngest(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type='submit']");
  button.disabled = true;
  document.querySelector("#ingestState").textContent = "Running";
  startFlow("ingest", "Ingesting document");
  try {
    const clientStarted = performance.now();
    const result = await api("/api/ingest/url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payloadFromForm(form)),
    });
    result.client_elapsed_ms = elapsedMs(clientStarted);
    finishFlow("ingest", `Ingested document ${result.document_id}`);
    document.querySelector("#ingestState").textContent = "Complete";
    renderIngestMetrics(result, "URL request");
    showToast(`Ingested document ${result.document_id}: ${result.tables} tables, ${result.financial_facts} facts.`);
    await refreshStatus();
  } catch (error) {
    finishFlow("idle", "Ingest failed");
    showToast(error.message, true);
    document.querySelector("#ingestState").textContent = "Error";
  } finally {
    button.disabled = false;
  }
}

async function submitExtractUrl(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type='submit']");
  const payload = payloadFromForm(form);
  try {
    payload.schema = JSON.parse(payload.schema);
  } catch (_error) {
    showToast("Extract schema must be valid JSON.", true);
    return;
  }

  button.disabled = true;
  document.querySelector("#extractState").textContent = "Running";
  extractOutput.className = "extract-output empty";
  extractOutput.textContent = "Calling /api/extract/url and Reducto Extract API...";
  startFlow("extract", "Running Extract API");
  try {
    const clientStarted = performance.now();
    const result = await api("/api/extract/url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    result.client_elapsed_ms = elapsedMs(clientStarted);
    renderExtractResult(result);
    finishFlow("extract", `Extracted document ${result.document_id}`);
    document.querySelector("#extractState").textContent = "Complete";
    showToast(`Extracted JSON via ${result.sdk_call}; extraction ${result.extraction_id}.`);
    await refreshStatus();
  } catch (error) {
    finishFlow("idle", "Extract failed");
    extractOutput.className = "extract-output empty";
    extractOutput.textContent = error.message;
    showToast(error.message, true);
    document.querySelector("#extractState").textContent = "Error";
  } finally {
    button.disabled = false;
  }
}

async function submitFileIngest(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type='submit']");
  const data = new FormData(form);
  if (!data.get("file") || data.get("file").size === 0) {
    showToast("Choose a file first.", true);
    return;
  }
  button.disabled = true;
  document.querySelector("#ingestState").textContent = "Uploading";
  startFlow("ingest", "Uploading and ingesting");
  try {
    const clientStarted = performance.now();
    const result = await api("/api/ingest/file", {
      method: "POST",
      body: data,
    });
    result.client_elapsed_ms = elapsedMs(clientStarted);
    finishFlow("ingest", `Ingested document ${result.document_id}`);
    document.querySelector("#ingestState").textContent = "Complete";
    renderIngestMetrics(result, "Upload request");
    showToast(`Uploaded ${result.filename}: ${result.tables} tables, ${result.financial_facts} facts.`);
    await refreshStatus();
  } catch (error) {
    finishFlow("idle", "Upload failed");
    showToast(error.message, true);
    document.querySelector("#ingestState").textContent = "Error";
  } finally {
    button.disabled = false;
  }
}

async function submitAsk(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type='submit']");
  button.disabled = true;
  answerPanel.className = "answer-panel empty";
  answerPanel.textContent = "Searching Oracle and building answer...";
  document.querySelector("#askState").textContent = "Running";
  startFlow("ask", "Answering question");
  try {
    const payload = payloadFromForm(form);
    payload.limit = 5;
    payload.evidence_limit = 3;
    const clientStarted = performance.now();
    const result = await api("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    result.client_elapsed_ms = elapsedMs(clientStarted);
    renderAnswer(result);
    finishFlow("ask", "Answer ready");
    document.querySelector("#askState").textContent = "Ready";
  } catch (error) {
    finishFlow("idle", "Answer failed");
    answerPanel.className = "answer-panel empty";
    answerPanel.textContent = error.message;
    showToast(error.message, true);
    document.querySelector("#askState").textContent = "Error";
  } finally {
    button.disabled = false;
  }
}

function renderAnswer(result) {
  answerPanel.className = "answer-panel";
  answerPanel.replaceChildren();

  answerPanel.append(renderResultMetrics(result));

  const answer = document.createElement("div");
  answer.className = "answer-block";
  const label = document.createElement("strong");
  label.textContent = "Answer";
  const text = document.createElement("p");
  text.textContent = result.answer;
  answer.append(label, text);
  answerPanel.append(answer);

  const evidenceList = document.createElement("div");
  evidenceList.className = "evidence-list";
  (result.evidence || []).forEach((item, index) => {
    const evidence = document.createElement("article");
    evidence.className = "evidence-item";
    const meta = document.createElement("span");
    meta.textContent = `Evidence ${index + 1} - ${item.page_number ? `page ${item.page_number}` : "page unknown"}`;
    const body = document.createElement("p");
    body.textContent = item.text;
    evidence.append(meta, body);
    evidenceList.append(evidence);
  });
  answerPanel.append(evidenceList);

  const firstSource = result.evidence && result.evidence[0] ? result.evidence[0].source_uri : "";
  if (firstSource) {
    const source = document.createElement("p");
    source.className = "source-line";
    source.textContent = `Source: ${firstSource}`;
    answerPanel.append(source);
  }
}

function renderExtractResult(result) {
  extractOutput.className = "extract-output";
  extractOutput.replaceChildren();

  const summary = document.createElement("div");
  summary.className = "result-metrics";
  summary.append(
    metricCard("Route", result.route),
    metricCard("Reducto endpoint", result.reducto_endpoint),
    metricCard("Backend", result.sdk_call),
    metricCard("Request", formatMs(result.client_elapsed_ms)),
    metricCard("Extract", formatMs(result.latency?.extract_ms)),
    metricCard("Oracle write", formatMs(result.latency?.store_ms)),
    metricCard("Document", result.document_id),
    metricCard("Extraction", result.extraction_id),
    metricCard("Fields", result.schema_fields),
  );
  extractOutput.append(summary);

  const grid = document.createElement("div");
  grid.className = "json-grid";
  grid.append(
    jsonBlock(`Backend request to Reducto ${result.reducto_endpoint || "/extract"}`, result.request_body),
    jsonBlock("Extract API response payload", result.extracted_json),
  );
  extractOutput.append(grid);

  if (result.studio_link) {
    const source = document.createElement("p");
    source.className = "source-line";
    source.textContent = `Reducto Studio: ${result.studio_link}`;
    extractOutput.append(source);
  }
}

function jsonBlock(title, payload) {
  const block = document.createElement("article");
  block.className = "json-block";
  const heading = document.createElement("strong");
  heading.textContent = title;
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(payload, null, 2);
  block.append(heading, pre);
  return block;
}

function renderIngestMetrics(result, requestLabel) {
  ingestMetrics.className = "run-metrics";
  ingestMetrics.replaceChildren();
  ingestMetrics.append(
    metricCard(requestLabel, formatMs(result.client_elapsed_ms)),
    metricCard("Total", formatMs(result.latency?.total_ms)),
    metricCard("Parse", formatMs(result.latency?.parse_ms)),
    metricCard("Oracle write", formatMs(result.latency?.store_ms)),
    metricCard("Reducto", formatMs(result.latency?.reducto_ms)),
  );
}

function renderResultMetrics(result) {
  const wrapper = document.createElement("div");
  wrapper.className = "result-metrics";
  const latency = result.latency || {};

  wrapper.append(
    metricCard("Request", formatMs(result.client_elapsed_ms)),
    metricCard("Retrieve", formatMs(latency.retrieval_ms)),
    metricCard("Answer", formatMs(latency.answer_ms)),
    metricCard("Results", latency.result_count ?? 0),
  );
  return wrapper;
}

function metricCard(label, value) {
  const card = document.createElement("div");
  card.className = "metric-card";
  const strong = document.createElement("strong");
  strong.textContent = value === null || value === undefined || value === "" ? "-" : String(value);
  const span = document.createElement("span");
  span.textContent = label;
  card.append(strong, span);
  return card;
}

function elapsedMs(started) {
  return Math.round((performance.now() - started) * 100) / 100;
}

function formatMs(value) {
  if (value === null || value === undefined) return "-";
  if (value >= 1000) return `${(value / 1000).toFixed(value >= 10000 ? 1 : 2)}s`;
  return `${Math.round(value)}ms`;
}

function showToast(message, isError = false) {
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("visible");
  window.setTimeout(() => toast.classList.remove("visible"), 5200);
}

document.querySelector("#refreshButton").addEventListener("click", () => {
  refreshStatus().catch((error) => showToast(error.message, true));
});
document.querySelector("#extractForm").addEventListener("submit", submitExtractUrl);
document.querySelector("#urlForm").addEventListener("submit", submitUrlIngest);
document.querySelector("#fileForm").addEventListener("submit", submitFileIngest);
document.querySelector("#askForm").addEventListener("submit", submitAsk);
document.querySelectorAll("[data-question]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelector("#questionInput").value = button.dataset.question;
  });
});

setFlow("idle");
refreshStatus().catch((error) => showToast(error.message, true));
