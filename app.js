const PAGE_SIZE = 10;
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const integer = new Intl.NumberFormat("en-US");
const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;
const COMMON_THREAD_COUNTS = [1, 2, 4, 8, 14, 16, 32, 56];

const state = {
  catalog: null,
  job: null,
  running: false,
  page: 1,
  total: 0,
  sort: "value",
  direction: "desc",
  search: "",
  lastStage: "",
  logs: [],
  pollTimer: null,
};

function escapeHtml(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

function formatValue(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  if (number === 0) return "0";
  if (Math.abs(number) < 0.001 || Math.abs(number) >= 1e6) return number.toExponential(3);
  return number.toLocaleString("en-US", { maximumFractionDigits: 7 });
}

function formatRuntime(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 1) return `${seconds.toFixed(3)} s`;
  return `${seconds.toFixed(seconds < 100 ? 2 : 1)} s`;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("visible");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("visible"), 3200);
}

function addLog(message) {
  state.logs.push({ time: new Date().toLocaleTimeString("en-GB", { hour12: false }), message });
  $("#logList").innerHTML = state.logs.map((entry) => `<li><time>${entry.time}</time>${escapeHtml(entry.message)}</li>`).join("");
}

async function api(path, options = {}) {
  const isMultipart = options.body instanceof FormData;
  const response = await fetch(path, {
    cache: "no-store",
    ...options,
    headers: { ...(!isMultipart ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function selectedDataset() {
  return state.catalog?.datasets.find((item) => item.id === $("#datasetSelect").value);
}

function selectedAlgorithm() {
  return state.catalog?.algorithms.find((item) => item.id === $("#algorithmSelect").value);
}

function renderDatasetOptions(selectedId) {
  $("#datasetSelect").innerHTML = state.catalog.datasets.map((item) => {
    const suffix = item.source === "uploaded" ? " · Uploaded" : "";
    return `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label + suffix)}</option>`;
  }).join("");
  if (selectedId) $("#datasetSelect").value = selectedId;
}

function selectedBenchmarkBackends() {
  return $$('#benchmarkBackends input[type="checkbox"]:checked').map((input) => input.value);
}

function updateBenchmarkBackendStatus() {
  const selected = selectedBenchmarkBackends().length;
  const total = state.catalog?.backends.length || 0;
  const status = $("#benchmarkBackendStatus");
  status.textContent = selected ? `${selected} of ${total} libraries selected` : "Select at least one library";
  status.classList.toggle("error", selected === 0);
}

function renderBenchmarkBackendOptions(backends) {
  $("#benchmarkBackends").innerHTML = backends.map((backend) => `
    <label class="benchmark-backend-option">
      <input type="checkbox" value="${escapeHtml(backend.id)}" checked>
      <span>${escapeHtml(backend.label)}</span>
    </label>`).join("");
  updateBenchmarkBackendStatus();
}

function populateCatalog(catalog) {
  state.catalog = catalog;
  renderDatasetOptions(catalog.defaults?.dataset);
  renderBenchmarkBackendOptions(catalog.backends);
  const groups = new Map();
  catalog.algorithms.forEach((item) => {
    if (!groups.has(item.family)) groups.set(item.family, []);
    groups.get(item.family).push(item);
  });
  $("#algorithmSelect").innerHTML = [...groups].map(([family, algorithms]) => `<optgroup label="${escapeHtml(family)}">${algorithms.map((item) => `<option value="${item.id}">${escapeHtml(item.label)}</option>`).join("")}</optgroup>`).join("");
  const defaults = catalog.defaults || {};
  if (defaults.algorithm) $("#algorithmSelect").value = defaults.algorithm;
  configureThreadInput(catalog.max_threads, defaults.threads);
  $("#datasetSelect").disabled = false;
  $("#algorithmSelect").disabled = false;
  $("#runButton").disabled = false;
  updateDataset();
  updateAlgorithm();
}

function configureThreadInput(maxThreads, defaultThreads) {
  const maximum = Math.max(1, Number(maxThreads) || 1);
  const threadInput = $("#threadSelect");
  threadInput.max = String(maximum);
  threadInput.value = String(Math.min(maximum, Math.max(1, Number(defaultThreads) || maximum)));
  const suggestions = [...new Set([...COMMON_THREAD_COUNTS.filter((value) => value <= maximum), maximum])].sort((left, right) => left - right);
  $("#threadSuggestions").innerHTML = suggestions.map((value) => `<option value="${value}"></option>`).join("");
  $("#threadHint").textContent = `This machine supports up to ${integer.format(maximum)} logical threads. Values above this limit use the maximum automatically.`;
  const maxButton = $("#threadMaxButton");
  maxButton.textContent = `Use max (${integer.format(maximum)})`;
  maxButton.disabled = false;
}

function updateDataset() {
  const dataset = selectedDataset();
  if (!dataset) return;
  $("#datasetNodes").textContent = integer.format(dataset.nodes);
  $("#datasetEdges").textContent = integer.format(dataset.edges);
  $("#datasetDirected").textContent = dataset.directed ? "Directed" : "Undirected";
  $("#datasetWeighted").textContent = dataset.weighted ? "Weighted data" : "Unweighted";
  const uploaded = dataset.source === "uploaded";
  $("#datasetSource").hidden = !uploaded;
  $("#datasetExpiry").hidden = !uploaded;
  $("#datasetExpiry").textContent = uploaded
    ? `Available until ${new Date(dataset.expires_at).toLocaleString()}`
    : "";
  renderParameterFields();
}

function toggleUploadPanel(open) {
  $("#uploadPanel").hidden = !open;
  $("#uploadToggle").setAttribute("aria-expanded", String(open));
  if (open) $("#uploadFile").focus();
}

function setUploadStatus(message, kind = "") {
  const status = $("#uploadStatus");
  status.textContent = message;
  status.className = `upload-status ${kind}`.trim();
}

async function uploadDataset(event) {
  event.preventDefault();
  const file = $("#uploadFile").files[0];
  if (!file) return setUploadStatus("Choose a CSV or TXT file.", "error");
  if (!/\.(csv|txt)$/i.test(file.name)) return setUploadStatus("Only CSV and TXT files are supported.", "error");
  if (file.size > MAX_UPLOAD_BYTES) return setUploadStatus("File exceeds the 100 MB limit.", "error");
  const name = $("#uploadName").value.trim();
  if (!name) return setUploadStatus("Enter a dataset name.", "error");

  const form = new FormData();
  form.append("file", file);
  form.append("name", name);
  form.append("directed", $("#uploadDirection").value);
  form.append("header", String($("#uploadHeader").checked));
  form.append("weighted", String($("#uploadWeighted").checked));
  $("#uploadSubmit").disabled = true;
  setUploadStatus("Uploading and validating…", "pending");
  try {
    const dataset = await api("/api/datasets/upload", { method: "POST", body: form });
    state.catalog.datasets.push(dataset);
    renderDatasetOptions(dataset.id);
    updateDataset();
    $("#uploadPanel").reset();
    setUploadStatus("");
    toggleUploadPanel(false);
    showToast(`${dataset.label} is ready to run.`);
  } catch (error) {
    setUploadStatus(error.message, "error");
  } finally {
    $("#uploadSubmit").disabled = false;
  }
}

function updateAlgorithm() {
  const algorithm = selectedAlgorithm();
  if (!algorithm) return;
  $("#resultValueHeader").textContent = algorithm.result_label;
  $("#statisticsTitle").textContent = `${algorithm.label} score distribution`;
  $("#resultCountLabel").textContent = algorithm.result_kind === "scalar" ? "Result" : "Result Rows";
  $("#rowCountDetail").textContent = algorithm.result_kind === "scalar" ? "One graph-level value" : "One value per node";
  renderParameterFields();
  resetResultPanels();
}

function renderParameterFields() {
  const algorithm = selectedAlgorithm();
  const dataset = selectedDataset();
  if (!algorithm || !dataset) return;
  const fields = algorithm.parameters || [];
  $("#parameterFields").innerHTML = fields.length ? fields.map((field) => {
    if (field.type === "select") {
      const options = field.options.map((option) => {
        const disabled = option.value === "weighted" && !dataset.weighted;
        return `<option value="${option.value}" ${option.value === field.default ? "selected" : ""} ${disabled ? "disabled" : ""}>${escapeHtml(option.label)}</option>`;
      }).join("");
      return `<label class="parameter-field"><span>${escapeHtml(field.label)}</span><select class="field-control" data-param="${field.id}">${options}</select></label>`;
    }
    return `<label class="parameter-field"><span>${escapeHtml(field.label)}</span><input class="field-control" data-param="${field.id}" type="number" value="${field.default}" ${field.min !== undefined ? `min="${field.min}"` : ""} ${field.max !== undefined ? `max="${field.max}"` : ""} step="${field.step ?? "any"}"></label>`;
  }).join("") : '<span class="parameter-empty">This function has no advanced parameters.</span>';
}

function collectParameters() {
  const params = {};
  $$('[data-param]').forEach((control) => {
    params[control.dataset.param] = control.tagName === "SELECT" ? control.value : Number(control.value);
  });
  return params;
}

function resetResultPanels() {
  $("#downloadCsv").disabled = true;
  $("#downloadJson").disabled = true;
  $("#nodeSearch").disabled = true;
  $("#scalarResult").hidden = true;
  $("#nodeResults").hidden = false;
  $("#resultsBody").innerHTML = '<tr class="empty-row"><td colspan="3">Run the analysis to view node-level scores.</td></tr>';
  $("#statsList").innerHTML = "<div><dt>Min</dt><dd>—</dd></div><div><dt>Median</dt><dd>—</dd></div><div><dt>Mean</dt><dd>—</dd></div><div><dt>Max</dt><dd>—</dd></div><div><dt>Non-zero</dt><dd>—</dd></div>";
  $("#percentileCards").innerHTML = "";
  drawPlaceholder($("#histogramCanvas"), "Run analysis to load distribution");
  drawPlaceholder($("#largeHistogram"), "Run analysis to load distribution");
}

async function startRun() {
  if (state.running) return;
  const threadInput = $("#threadSelect");
  const requestedThreads = threadInput.valueAsNumber;
  const maxThreads = Math.max(1, Number(state.catalog?.max_threads) || Number(threadInput.max) || 1);
  if (!Number.isInteger(requestedThreads) || requestedThreads < 1) {
    showToast("Enter a whole-number thread count of at least 1.");
    threadInput.focus();
    return;
  }
  const threads = Math.min(requestedThreads, maxThreads);
  if (requestedThreads > maxThreads) {
    threadInput.value = String(maxThreads);
    showToast(`This machine supports up to ${integer.format(maxThreads)} threads; using that maximum.`);
  }
  const benchmark = $("#benchmarkToggle").checked;
  const benchmarkBackends = selectedBenchmarkBackends();
  if (benchmark && !benchmarkBackends.length) {
    updateBenchmarkBackendStatus();
    showToast("Select at least one benchmark library.");
    return;
  }
  state.running = true;
  state.lastStage = "";
  state.page = 1;
  state.search = "";
  state.logs = [];
  $("#nodeSearch").value = "";
  $("#runButton").disabled = true;
  $("#statusValue").style.color = "";
  $("#runProgress").hidden = false;
  resetResultPanels();
  const body = {
    mode: benchmark ? "benchmark" : "analysis",
    dataset: $("#datasetSelect").value,
    algorithm: $("#algorithmSelect").value,
    threads,
    benchmark_runs: Number($("#benchmarkRuns").value),
    backends: benchmarkBackends,
    collect_metrics: $("#metricsToggle").checked,
    params: collectParameters(),
  };
  addLog(`Submitted · mode=${body.mode} · dataset=${body.dataset} · function=${body.algorithm}${benchmark ? ` · backends=${body.backends.join(",")}` : ""}`);
  try {
    state.job = await api("/api/runs", { method: "POST", body: JSON.stringify(body) });
    await pollJob();
  } catch (error) {
    finishWithError(error.message);
  }
}

async function pollJob() {
  try {
    const job = await api(`/api/runs/${state.job.id}`);
    state.job = job;
    renderJob(job);
    if (job.status === "completed") {
      await finishRun(job);
      return;
    }
    if (job.status === "failed") {
      finishWithError(job.error || "Run failed.");
      return;
    }
    state.pollTimer = setTimeout(pollJob, 700);
  } catch (error) {
    finishWithError(error.message);
  }
}

function renderJob(job) {
  const statusNames = { queued: "Queued", running: "Running", completed: "Completed", failed: "Failed" };
  $("#statusValue").textContent = statusNames[job.status] || job.status;
  $("#statusDetail").textContent = job.message;
  $("#runtimeValue").textContent = job.runtime_seconds != null ? formatRuntime(job.runtime_seconds) : formatRuntime(job.elapsed_seconds);
  $("#cpuValue").textContent = job.config.collect_metrics ? `${Number(job.peak_cpu).toFixed(1)}%` : "Off";
  $("#memoryValue").textContent = job.config.collect_metrics ? formatMemory(job.peak_memory_mb) : "Off";
  $("#headerCpu").textContent = job.config.collect_metrics && job.metrics.length ? `${job.metrics.at(-1).cpu_percent.toFixed(0)}%` : "—";
  $("#systemLabel").textContent = job.status === "running" ? "System Busy" : "System Ready";
  $("#progressTitle").textContent = job.current_backend_label ? `${job.current_backend_label} · ${job.stage}` : job.message;
  $("#progressDetail").textContent = job.config.mode === "benchmark" ? `Sequential backend ${job.backend_index || 0} of ${job.backend_total}` : "Live EGMCC subprocess";
  const progress = job.status === "completed" ? 100 : job.config.mode === "benchmark" ? Math.max(5, ((job.backend_index - 1) / job.backend_total) * 100 + (job.stage === "running" ? 12 : 22)) : job.status === "running" ? 55 : 8;
  $("#progressBar").style.width = `${Math.min(100, progress)}%`;
  if (job.stage !== state.lastStage) {
    state.lastStage = job.stage;
    addLog(job.message);
  }
  renderResource(job.metrics || []);
  renderBenchmark(job.benchmark_results || []);
}

async function finishRun(job) {
  state.running = false;
  $("#runButton").disabled = false;
  $("#runProgress").hidden = true;
  $("#statusValue").textContent = "Completed";
  $("#statusValue").style.color = "";
  $("#statusDetail").textContent = job.message;
  $("#headerCpu").textContent = "—";
  $("#systemLabel").textContent = "System Ready";
  addLog(job.message);

  if (job.config.mode === "benchmark") {
    const reference = job.benchmark_results.find((item) => item.backend === "easygraph" && item.status === "completed")
      || job.benchmark_results.find((item) => item.status === "completed");
    $("#runtimeValue").textContent = reference ? formatRuntime(reference.runtime_seconds) : "—";
    $("#runtimeDetail").textContent = reference ? `${reference.backend_label} mean runtime` : "No backend completed";
    $("#rowCountValue").textContent = String(job.benchmark_results.length);
    $("#resultProvenance").textContent = "Benchmark mode stores timing summaries; run Analysis mode for downloadable node results.";
    switchView("benchmark");
    showToast("Serial benchmark completed.");
    return;
  }

  $("#runtimeValue").textContent = formatRuntime(job.runtime_seconds);
  $("#runtimeDetail").textContent = "EGMCC measured runtime";
  $("#downloadCsv").disabled = false;
  $("#downloadJson").disabled = false;
  $("#resultProvenance").textContent = `${selectedDataset().label} · ${selectedAlgorithm().label} · live EGMCC computation`;
  if (job.result_kind === "scalar") {
    $("#nodeResults").hidden = true;
    $("#scalarResult").hidden = false;
    $("#scalarLabel").textContent = job.result_label;
    $("#scalarValue").textContent = formatValue(job.scalar_result);
    $("#rowCountValue").textContent = formatValue(job.scalar_result);
    $("#statsList").innerHTML = `<div><dt>${escapeHtml(job.result_label)}</dt><dd>${formatValue(job.scalar_result)}</dd></div>`;
  } else {
    $("#nodeSearch").disabled = false;
    $("#rowCountValue").textContent = integer.format(job.result_count);
    await loadResults();
    renderStatistics(job.statistics);
  }
  showToast(`${selectedAlgorithm().label} results are ready.`);
}

function finishWithError(message) {
  state.running = false;
  clearTimeout(state.pollTimer);
  $("#runButton").disabled = false;
  $("#runProgress").hidden = true;
  $("#statusValue").textContent = "Failed";
  $("#statusValue").style.color = "#c92a2a";
  $("#statusDetail").textContent = message;
  $("#systemLabel").textContent = "System Ready";
  addLog(`Failed · ${message}`);
  showToast(message);
}

async function loadResults() {
  if (!state.job?.id) return;
  const query = new URLSearchParams({ page: state.page, page_size: PAGE_SIZE, search: state.search, sort: state.sort, direction: state.direction });
  const payload = await api(`/api/runs/${state.job.id}/results?${query}`);
  state.total = payload.total;
  const max = Math.max(...payload.rows.map((row) => Number(row.value)), 0);
  $("#resultsBody").innerHTML = payload.rows.length ? payload.rows.map((row) => {
    const width = max > 0 ? Math.max(1, Number(row.value) / max * 96) : 0;
    return `<tr><td>${integer.format(row.rank)}</td><td>${escapeHtml(row.node_id)}</td><td class="score-cell"><span class="score-bar" style="width:${width}%"></span><span class="score-value">${formatValue(row.value)}</span></td></tr>`;
  }).join("") : '<tr class="empty-row"><td colspan="3">No matching node IDs.</td></tr>';
  const start = payload.total ? (payload.page - 1) * payload.page_size + 1 : 0;
  const end = Math.min(payload.page * payload.page_size, payload.total);
  $("#paginationText").textContent = `${integer.format(start)}–${integer.format(end)} of ${integer.format(payload.total)}`;
  $("#tableSummary").textContent = `${integer.format(payload.total)} visible rows`;
  $("#pageNumber").textContent = payload.page;
  $("#prevPage").disabled = payload.page <= 1;
  $("#nextPage").disabled = end >= payload.total;
}

function renderStatistics(stats) {
  if (!stats?.count) return;
  $("#statsList").innerHTML = [["Min", stats.min], ["Median", stats.median], ["Mean", stats.mean], ["Max", stats.max]].map(([label, value]) => `<div><dt>${label}</dt><dd>${formatValue(value)}</dd></div>`).join("") + `<div><dt>Non-zero</dt><dd>${integer.format(stats.nonzero)} (${Number(stats.nonzero_percentage).toFixed(2)}%)</dd></div>`;
  $("#percentileCards").innerHTML = [["50th percentile", stats.median], ["75th percentile", stats.p75], ["90th percentile", stats.p90], ["95th percentile", stats.p95], ["99th percentile", stats.p99]].map(([label, value]) => `<div class="percentile-card"><span>${label}</span><strong>${formatValue(value)}</strong></div>`).join("") + `<div class="percentile-card"><span>Zero-value nodes</span><strong>${integer.format(stats.count - stats.nonzero)}</strong></div>`;
  drawHistogram($("#histogramCanvas"), stats.histogram, false);
  drawHistogram($("#largeHistogram"), stats.histogram, true);
}

function formatMemory(mb) {
  const value = Number(mb);
  if (!Number.isFinite(value)) return "—";
  return value >= 1024 ? `${(value / 1024).toFixed(2)} GB` : `${value.toFixed(0)} MB`;
}

function prepareCanvas(canvas) {
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  return ctx;
}

function drawPlaceholder(canvas, message) {
  const ctx = prepareCanvas(canvas);
  ctx.fillStyle = "#8792a4";
  ctx.font = "14px sans-serif";
  ctx.textAlign = "center";
  ctx.fillText(message, canvas.width / 2, canvas.height / 2);
}

function drawHistogram(canvas, bins, detailed) {
  if (!bins?.length) return drawPlaceholder(canvas, "No positive values to plot");
  const ctx = prepareCanvas(canvas);
  const margin = detailed ? { top: 28, right: 24, bottom: 52, left: 58 } : { top: 14, right: 12, bottom: 34, left: 38 };
  const width = canvas.width - margin.left - margin.right;
  const height = canvas.height - margin.top - margin.bottom;
  const max = Math.max(...bins.map((bin) => bin.percentage), 1);
  ctx.strokeStyle = "#dfe5ee";
  ctx.beginPath(); ctx.moveTo(margin.left, margin.top); ctx.lineTo(margin.left, margin.top + height); ctx.lineTo(margin.left + width, margin.top + height); ctx.stroke();
  const step = width / bins.length;
  bins.forEach((bin, index) => {
    const barHeight = bin.percentage / max * (height - 5);
    ctx.fillStyle = "#1765e6";
    ctx.fillRect(margin.left + index * step + 2, margin.top + height - barHeight, Math.max(2, step - 4), barHeight);
  });
  ctx.fillStyle = "#64748b"; ctx.font = `${detailed ? 13 : 10}px sans-serif`; ctx.textAlign = "center";
  ctx.fillText("Value (log scale)", margin.left + width / 2, canvas.height - 6);
}

function drawLines(canvas, points, detailed = false) {
  if (!points.length) return drawPlaceholder(canvas, "Resource data will appear during a run");
  const ctx = prepareCanvas(canvas);
  const margin = detailed ? { top: 28, right: 65, bottom: 52, left: 58 } : { top: 14, right: 48, bottom: 30, left: 40 };
  const width = canvas.width - margin.left - margin.right;
  const height = canvas.height - margin.top - margin.bottom;
  const maxTime = Math.max(...points.map((p) => p.elapsed), 1);
  const maxMemory = Math.max(...points.map((p) => p.memory_mb), 1);
  ctx.strokeStyle = "#dfe5ee"; ctx.strokeRect(margin.left, margin.top, width, height);
  const line = (field, max, color) => {
    ctx.strokeStyle = color; ctx.lineWidth = detailed ? 3 : 2; ctx.beginPath();
    points.forEach((point, index) => { const x = margin.left + point.elapsed / maxTime * width; const y = margin.top + height - point[field] / max * height; index ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.stroke();
  };
  line("cpu_percent", 100, "#159947"); line("memory_mb", maxMemory, "#1765e6");
  ctx.fillStyle = "#64748b"; ctx.font = "11px sans-serif"; ctx.textAlign = "center"; ctx.fillText(`Time · ${maxTime.toFixed(1)} s`, margin.left + width / 2, canvas.height - 6);
}

function drawSpark(canvas, values, color, max) {
  const ctx = prepareCanvas(canvas);
  if (!values.length) return;
  const ceiling = max || Math.max(...values, 1);
  ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath();
  values.forEach((value, index) => { const x = index / Math.max(1, values.length - 1) * canvas.width; const y = canvas.height - 3 - value / ceiling * (canvas.height - 6); index ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); ctx.stroke();
}

function renderResource(metrics) {
  drawLines($("#resourceCanvas"), metrics);
  drawLines($("#largeResourceCanvas"), metrics, true);
  drawSpark($("#cpuSpark"), metrics.map((item) => item.cpu_percent), "#159947", 100);
  drawSpark($("#memorySpark"), metrics.map((item) => item.memory_mb), "#1765e6");
}

function renderBenchmark(results) {
  if (!results.length) return drawPlaceholder($("#benchmarkCanvas"), "Run benchmark mode to compare backends");
  $("#benchmarkTable").innerHTML = results.map((result) => `<div class="benchmark-row"><strong>${escapeHtml(result.backend_label)}</strong><span>${escapeHtml(result.status)}${result.error ? ` · ${escapeHtml(result.error.slice(0, 90))}` : ""}</span><em>${result.status === "completed" ? formatRuntime(result.runtime_seconds) : "—"}</em></div>`).join("");
  const completed = results.filter((item) => item.status === "completed" && Number(item.runtime_seconds) > 0);
  if (!completed.length) return drawPlaceholder($("#benchmarkCanvas"), "No supported backend has completed yet");
  const ctx = prepareCanvas($("#benchmarkCanvas"));
  const canvas = $("#benchmarkCanvas");
  const margin = { top: 30, right: 35, bottom: 55, left: 90 };
  const width = canvas.width - margin.left - margin.right;
  const height = canvas.height - margin.top - margin.bottom;
  const logs = completed.map((item) => Math.log10(item.runtime_seconds));
  const min = Math.floor(Math.min(...logs, -3));
  const max = Math.ceil(Math.max(...logs, 1));
  const colors = ["#1765e6", "#159947", "#f47a25", "#8792a4"];
  completed.forEach((item, index) => {
    const y = margin.top + index * (height / completed.length) + 18;
    const barWidth = Math.max(5, (Math.log10(item.runtime_seconds) - min) / Math.max(1, max - min) * width);
    ctx.fillStyle = colors[state.catalog.backends.findIndex((backend) => backend.id === item.backend)] || "#1765e6";
    ctx.fillRect(margin.left, y, barWidth, 42);
    ctx.fillStyle = "#12213a"; ctx.font = "14px sans-serif"; ctx.textAlign = "right"; ctx.fillText(item.backend_label, margin.left - 10, y + 25);
    ctx.textAlign = "left"; ctx.fillText(formatRuntime(item.runtime_seconds), margin.left + barWidth + 8, y + 25);
  });
  ctx.fillStyle = "#64748b"; ctx.textAlign = "center"; ctx.fillText("Runtime (seconds, log scale)", margin.left + width / 2, canvas.height - 12);
}

function switchView(view) {
  $$(".view-panel").forEach((panel) => { panel.hidden = panel.dataset.view !== view; panel.classList.toggle("active", panel.dataset.view === view); });
  $$(".tab-button").forEach((button) => button.classList.toggle("active", button.dataset.viewTarget === view));
  if (view === "resource" && state.job) renderResource(state.job.metrics || []);
  if (view === "benchmark" && state.job) renderBenchmark(state.job.benchmark_results || []);
}

function download(extension) {
  if (!state.job?.id) return;
  window.location.href = `/api/runs/${state.job.id}/download.${extension}`;
}

function bindEvents() {
  $("#datasetSelect").addEventListener("change", updateDataset);
  $("#uploadToggle").addEventListener("click", () => toggleUploadPanel($("#uploadPanel").hidden));
  $("#uploadCancel").addEventListener("click", () => { setUploadStatus(""); toggleUploadPanel(false); });
  $("#uploadFile").addEventListener("change", (event) => {
    const file = event.target.files[0];
    if (!file) return;
    $("#uploadName").value = file.name.replace(/\.(csv|txt)$/i, "");
    setUploadStatus("");
  });
  $("#uploadPanel").addEventListener("submit", uploadDataset);
  $("#algorithmSelect").addEventListener("change", updateAlgorithm);
  $("#threadMaxButton").addEventListener("click", () => {
    const maxThreads = Math.max(1, Number(state.catalog?.max_threads) || 1);
    $("#threadSelect").value = String(maxThreads);
  });
  $("#runButton").addEventListener("click", startRun);
  $("#benchmarkBackends").addEventListener("change", updateBenchmarkBackendStatus);
  $("#selectAllBackends").addEventListener("click", () => {
    $$('#benchmarkBackends input[type="checkbox"]').forEach((input) => { input.checked = true; });
    updateBenchmarkBackendStatus();
  });
  $("#clearBackends").addEventListener("click", () => {
    $$('#benchmarkBackends input[type="checkbox"]').forEach((input) => { input.checked = false; });
    updateBenchmarkBackendStatus();
  });
  $("#benchmarkToggle").addEventListener("change", (event) => {
    $("#benchmarkOptions").hidden = !event.target.checked;
    $("#runButton span:last-child").textContent = event.target.checked ? "Run Benchmark" : "Run Analysis";
    $("#resultCountLabel").textContent = event.target.checked ? "Backends" : (selectedAlgorithm()?.result_kind === "scalar" ? "Result" : "Result Rows");
  });
  $$(".tab-button").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.viewTarget)));
  $$(".nav-link[data-nav]").forEach((button) => button.addEventListener("click", () => {
    const benchmark = button.dataset.nav === "benchmark";
    $("#benchmarkToggle").checked = benchmark;
    $("#benchmarkOptions").hidden = !benchmark;
    $("#runButton span:last-child").textContent = benchmark ? "Run Benchmark" : "Run Analysis";
    $("#resultCountLabel").textContent = benchmark ? "Backends" : (selectedAlgorithm()?.result_kind === "scalar" ? "Result" : "Result Rows");
    $$(".nav-link[data-nav]").forEach((item) => item.classList.toggle("active", item === button));
    switchView(benchmark ? "benchmark" : "results");
  }));
  let searchTimer;
  $("#nodeSearch").addEventListener("input", (event) => { clearTimeout(searchTimer); searchTimer = setTimeout(async () => { state.search = event.target.value.trim(); state.page = 1; await loadResults(); }, 250); });
  $$('[data-sort]').forEach((button) => button.addEventListener("click", async () => { const key = button.dataset.sort; state.direction = state.sort === key && state.direction === "desc" ? "asc" : "desc"; state.sort = key; state.page = 1; await loadResults(); }));
  $("#prevPage").addEventListener("click", async () => { if (state.page > 1) { state.page -= 1; await loadResults(); } });
  $("#nextPage").addEventListener("click", async () => { if (state.page * PAGE_SIZE < state.total) { state.page += 1; await loadResults(); } });
  $("#downloadCsv").addEventListener("click", () => download("csv"));
  $("#downloadJson").addEventListener("click", () => download("json"));
  $("#clearLog").addEventListener("click", () => { state.logs = []; $("#logList").innerHTML = "<li>No run has started.</li>"; });
}

async function initialize() {
  bindEvents();
  resetResultPanels();
  renderResource([]);
  renderBenchmark([]);
  try {
    const catalog = await api("/api/catalog");
    populateCatalog(catalog);
    $("#systemLabel").textContent = "System Ready";
  } catch (error) {
    $("#systemLabel").textContent = "Server Offline";
    $("#statusValue").textContent = "Offline";
    $("#statusDetail").textContent = "Start the Python demo server";
    showToast(error.message);
  }
}

initialize();
