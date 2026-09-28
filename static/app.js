/* CPI Load Tester – Oberfläche (Vanilla JS, kein Build). */
"use strict";

const $ = (id) => document.getElementById(id);
const STORAGE_KEY = "cpiload.form.v1";
const TERMINAL = new Set(["completed", "stopped", "failed", "interrupted"]);

const STATUS = {
  created: ["neutral", "Angelegt"],
  running: ["teal", "Läuft"],
  paused: ["warning", "Pausiert"],
  stopping: ["warning", "Stoppt …"],
  completed: ["success", "Abgeschlossen"],
  stopped: ["neutral", "Gestoppt"],
  failed: ["danger", "Fehlgeschlagen"],
  interrupted: ["warning", "Unterbrochen"],
};

const state = {
  folder: "",
  preview: null,
  active: null,
  lastRunId: null,
  lastStatus: null,
  defaults: {},
  scanning: null,
};

// -- Formatierung (de-DE) -------------------------------------------------------
const nf0 = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat("de-DE", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const fmtInt = (n) => (n == null ? "–" : nf0.format(n));
const fmtRate = (n) => (n == null ? "–" : n >= 100 ? nf0.format(n) : nf1.format(n));
const fmtMs = (ms) => {
  if (ms == null) return "–";
  if (ms >= 10000) return `${nf1.format(ms / 1000)} s`;
  return `${nf0.format(ms)} ms`;
};
const fmtDuration = (s) => {
  if (s == null || !isFinite(s)) return "–";
  if (s < 10) return `${nf1.format(s)} s`;
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h} h ${String(m).padStart(2, "0")} min`;
  if (m) return `${m} min ${String(sec).padStart(2, "0")} s`;
  return `${sec} s`;
};
const fmtDateTime = (iso) => {
  if (!iso) return "–";
  const d = new Date(iso);
  return `${d.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" })} ${d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" })}`;
};
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleTimeString("de-DE") : "–");
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function badge(status) {
  const [tone, label] = STATUS[status] || ["neutral", status];
  return `<span class="badge badge-${tone}"><span class="dot"></span>${esc(label)}</span>`;
}

// -- API ------------------------------------------------------------------------
async function api(path, options = {}) {
  const opts = { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } };
  if (opts.body && typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
  const res = await fetch(path, opts);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    let detail = data && data.detail !== undefined ? data.detail : text || res.statusText;
    if (Array.isArray(detail)) detail = detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join(" · ");
    throw new Error(detail);
  }
  return data;
}

let toastTimer = null;
function toast(message, kind = "ok") {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast ${kind}`;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), kind === "err" ? 7000 : 3500);
}

// -- Formularzustand (ohne Secret) ----------------------------------------------
function loadForm() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}"); } catch { saved = {}; }
  document.querySelectorAll("[data-persist]").forEach((el) => {
    if (!(el.id in saved)) return;
    if (el.type === "checkbox") el.checked = !!saved[el.id];
    else el.value = saved[el.id];
  });
  state.folder = saved.folder || "";
  renderHeaderRows(saved.headers || []);
}

function saveForm() {
  const data = { folder: state.folder, headers: readHeaders() };
  document.querySelectorAll("[data-persist]").forEach((el) => {
    data[el.id] = el.type === "checkbox" ? el.checked : el.value;
  });
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(data)); } catch { /* Speicher gesperrt – egal */ }
}

function connection() {
  return {
    endpoint_url: $("endpoint_url").value.trim(),
    token_url: $("token_url").value.trim(),
    client_id: $("client_id").value.trim(),
    client_secret: $("client_secret").value,
  };
}

const num = (id, fallback = 0) => {
  const v = parseFloat($(id).value);
  return Number.isFinite(v) ? v : fallback;
};

function loadSettings() {
  return {
    concurrency: Math.round(num("concurrency", 10)),
    rate_limit: num("rate_limit", 0),
    timeout_s: num("timeout_s", 60),
    method: $("method").value,
    content_type: $("content_type").value.trim() || "auto",
    placeholders: $("placeholders").checked,
    headers: readHeaders(),
    max_consecutive_failures: Math.round(num("max_consecutive_failures", 100)),
  };
}

function fileSelection() {
  return {
    folder: state.folder,
    pattern: $("pattern").value.trim() || "*",
    recursive: $("recursive").checked,
    max_files: Math.round(num("max_files", 0)),
  };
}

// -- Custom Header --------------------------------------------------------------
function renderHeaderRows(rows) {
  const box = $("header-rows");
  box.innerHTML = "";
  rows.forEach((r) => addHeaderRow(r.name, r.value));
}

function addHeaderRow(name = "", value = "") {
  const row = document.createElement("div");
  row.className = "header-row";
  row.innerHTML = `
    <input type="text" class="h-name" placeholder="X-Filename" value="${esc(name)}" spellcheck="false">
    <input type="text" class="h-value" placeholder="{{filename}}" value="${esc(value)}" spellcheck="false">
    <button type="button" class="remove" title="Header entfernen" aria-label="Header entfernen">✕</button>`;
  row.querySelector(".remove").addEventListener("click", () => { row.remove(); saveForm(); });
  row.querySelectorAll("input").forEach((i) => i.addEventListener("change", saveForm));
  $("header-rows").appendChild(row);
  return row;
}

function readHeaders() {
  return [...document.querySelectorAll(".header-row")]
    .map((r) => ({ name: r.querySelector(".h-name").value.trim(), value: r.querySelector(".h-value").value }))
    .filter((h) => h.name);
}

// -- Service Key ----------------------------------------------------------------
function applyServiceKey() {
  let key;
  try { key = JSON.parse($("servicekey").value); } catch { toast("Kein gültiges JSON.", "err"); return; }
  const o = key.oauth || (key.credentials && key.credentials.oauth) || key;
  if (!o.clientid || !o.clientsecret || !o.tokenurl) {
    toast("Service Key ohne clientid/clientsecret/tokenurl – ist es ein OAuth-Key der Process Integration Runtime?", "err");
    return;
  }
  let tokenUrl = o.tokenurl.replace(/\/+$/, "");
  if (!/\/oauth\/token$/.test(tokenUrl)) tokenUrl += "/oauth/token";
  $("client_id").value = o.clientid;
  $("client_secret").value = o.clientsecret;
  $("token_url").value = tokenUrl;
  const base = (o.url || "").replace(/\/+$/, "");
  const endpoint = $("endpoint_url");
  if (base && !endpoint.value.startsWith(base)) {
    endpoint.value = `${base}/http/`;
    endpoint.focus();
    endpoint.setSelectionRange(endpoint.value.length, endpoint.value.length);
  }
  $("servicekey").value = "";
  $("servicekey-box").hidden = true;
  saveForm();
  toast("Service Key übernommen. Endpunkt-Pfad des iFlows ergänzen.");
}

async function testAuth() {
  const out = $("auth-result");
  out.className = "inline-result";
  out.textContent = "Hole Token …";
  try {
    const res = await api("/api/auth/test", { method: "POST", body: { connection: connection() } });
    if (res.ok) {
      out.className = "inline-result ok";
      out.textContent = `Token erhalten → gültig für ${fmtDuration(res.expires_in)}.`;
    } else {
      out.className = "inline-result err";
      out.textContent = res.error;
    }
  } catch (e) {
    out.className = "inline-result err";
    out.textContent = e.message;
  }
}

// -- Ordner-Browser -------------------------------------------------------------
async function openFolder(path, refresh = false) {
  let data;
  const loading = setTimeout(() => {
    $("folder-list").innerHTML = `<li class="loading">Lese Ordner ein …</li>`;
    $("folder-info").textContent = "Große Ordner über Docker Desktop brauchen beim ersten Mal etwas – danach aus dem Cache.";
  }, 300);
  state.scanning = path;
  try {
    data = await api(`/api/folders?path=${encodeURIComponent(path)}${refresh ? "&refresh=true" : ""}`);
  } catch (e) {
    clearTimeout(loading);
    state.scanning = null;
    if (path) return openFolder("");
    $("folder-list").innerHTML = `<li class="empty">${esc(e.message)}</li>`;
    return;
  }
  clearTimeout(loading);
  state.scanning = null;
  state.folder = data.path;
  saveForm();

  const crumbs = $("breadcrumb");
  const parts = data.path ? data.path.split("/") : [];
  const items = [`<button type="button" data-path="">/data</button>`];
  parts.forEach((p, i) => {
    const target = parts.slice(0, i + 1).join("/");
    items.push(`<span>/</span>`);
    items.push(i === parts.length - 1
      ? `<span class="current">${esc(p)}</span>`
      : `<button type="button" data-path="${esc(target)}">${esc(p)}</button>`);
  });
  crumbs.innerHTML = items.join("");
  crumbs.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => openFolder(b.dataset.path)));

  const list = $("folder-list");
  list.innerHTML = data.dirs.length
    ? data.dirs.map((d) => `<li><button type="button" data-path="${esc(d.path)}" aria-label="Ordner ${esc(d.name)} öffnen"><span>${esc(d.name)}</span><span class="arrow">→</span></button></li>`).join("")
    : `<li class="empty">Keine Unterordner.</li>`;
  list.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => openFolder(b.dataset.path)));

  $("folder-info").innerHTML = `<span class="num">${fmtInt(data.files)}</span> Dateien direkt in diesem Ordner`;
  schedulePreview();
}

let previewTimer = null;
let previewSeq = 0;
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, 350);
}

async function runPreview() {
  const seq = ++previewSeq;
  const out = $("preview-result");
  out.innerHTML = "Zähle passende Dateien …";
  try {
    const sel = fileSelection();
    const res = await api("/api/folders/preview", { method: "POST", body: sel });
    if (seq !== previewSeq) return;
    state.preview = res;
    const limited = res.effective < res.matches
      ? ` · davon <strong>${fmtInt(res.effective)}</strong> im Lauf (Max. Dateien)` : "";
    out.innerHTML = `<strong>${fmtInt(res.matches)}</strong> Treffer${limited}` +
      (res.sample.length ? `<div class="sample">${res.sample.slice(0, 3).map(esc).join(" · ")}${res.matches > 3 ? " · …" : ""}</div>` : "");
  } catch (e) {
    if (seq !== previewSeq) return;
    state.preview = null;
    out.textContent = e.message;
  }
  updatePlanSummary();
}

// -- Planung --------------------------------------------------------------------
function updatePlanSummary() {
  const el = $("plan-summary");
  const count = state.preview ? state.preview.effective : null;
  const load = loadSettings();
  document.querySelectorAll("[data-smoke]").forEach((c) =>
    c.classList.toggle("active", Number(c.dataset.smoke) === Math.round(num("max_files", 0))));
  if (count == null) { el.textContent = "Ordner und Endpunkt wählen, dann geht's los."; return; }
  if (count === 0) { el.textContent = "Keine passenden Dateien im gewählten Ordner."; return; }
  const parts = [`<strong>${fmtInt(count)}</strong> Dateien`, `<strong>${fmtInt(load.concurrency)}</strong> parallel`];
  if (load.rate_limit > 0) {
    parts.push(`max. <strong>${fmtRate(load.rate_limit)}</strong> msg/s`);
    parts.push(`Dauer ≈ <strong>${fmtDuration(count / load.rate_limit)}</strong>`);
  } else {
    parts.push("Rate offen – Tempo bestimmt der iFlow");
  }
  el.innerHTML = parts.join(" · ");
}

// -- Läufe steuern --------------------------------------------------------------
async function startRun() {
  saveForm();
  const btn = $("start");
  btn.disabled = true;
  try {
    const body = { connection: connection(), files: fileSelection(), load: loadSettings() };
    const res = await api("/api/runs", { method: "POST", body });
    toast(`Lauf #${res.id} gestartet.`);
    $("live").scrollIntoView({ behavior: "smooth", block: "start" });
    refreshHistory();
  } catch (e) {
    toast(e.message, "err");
  } finally {
    updateButtons();
  }
}

async function control(action) {
  if (!state.active) return;
  if (action === "stop" && !confirm(`Lauf #${state.active.run_id} stoppen? Offene Dateien lassen sich später fortsetzen.`)) return;
  try {
    await api(`/api/runs/${state.active.run_id}/${action}`, { method: "POST" });
  } catch (e) {
    toast(e.message, "err");
  }
}

async function applyLive() {
  if (!state.active) return;
  try {
    const res = await api(`/api/runs/${state.active.run_id}/live`, {
      method: "PATCH",
      body: { concurrency: Math.round(num("live-concurrency", 1)), rate_limit: num("live-rate", 0) },
    });
    toast(`Übernommen: ${res.concurrency} parallel · ${res.rate_limit > 0 ? `${fmtRate(res.rate_limit)} msg/s` : "Rate offen"}.`);
  } catch (e) {
    toast(e.message, "err");
  }
}

async function historyAction(action, id) {
  try {
    if (action === "resume" || action === "retry") {
      const path = action === "resume" ? "resume" : "retry-failed";
      const res = await api(`/api/runs/${id}/${path}`, { method: "POST", body: { connection: connection() } });
      toast(action === "resume" ? `Lauf #${res.id} wird fortgesetzt.` : `Lauf #${res.id} sendet die Fehler erneut.`);
      $("live").scrollIntoView({ behavior: "smooth", block: "start" });
    } else if (action === "delete") {
      if (!confirm(`Lauf #${id} inklusive aller Einzelergebnisse löschen?`)) return;
      await api(`/api/runs/${id}`, { method: "DELETE" });
    }
    refreshHistory();
  } catch (e) {
    toast(e.message, "err");
  }
}

// -- Live-Ansicht ---------------------------------------------------------------
function isBusy() {
  return !!state.active && !TERMINAL.has(state.active.status);
}

function updateButtons() {
  const busy = isBusy();
  $("start").disabled = busy;
  document.querySelectorAll("[data-requires-idle]").forEach((b) => (b.disabled = busy));
  const s = state.active ? state.active.status : null;
  $("btn-pause").hidden = s !== "running";
  $("btn-unpause").hidden = s !== "paused";
  $("btn-stop").hidden = !(s === "running" || s === "paused");
  $("live-apply").disabled = !(s === "running" || s === "paused");

  const [tone, label] = s && !TERMINAL.has(s) ? STATUS[s] : ["neutral", "Bereit"];
  const g = $("global-status");
  g.className = `badge badge-${tone}`;
  g.querySelector(".label").textContent = s && !TERMINAL.has(s) ? `${label} · #${state.active.run_id}` : label;
}

function renderLive(snap) {
  const prev = state.active;
  state.active = snap;
  $("live-empty").hidden = !!snap;
  $("live-body").hidden = !snap;
  if (!snap) { updateButtons(); return; }

  const [, label] = STATUS[snap.status] || ["neutral", snap.status];
  $("live-kicker").textContent = `Live · Lauf #${snap.run_id} · ${label}`;
  $("live-title").textContent = `/data/${snap.folder || ""} → ${snap.endpoint_url.replace(/^https?:\/\//, "")}`;

  const msg = $("live-message");
  msg.hidden = !snap.message;
  msg.textContent = snap.message || "";
  msg.classList.toggle("danger", snap.status === "failed");

  const pctOk = snap.total ? (snap.ok / snap.total) * 100 : 0;
  const pctFail = snap.total ? (snap.failed / snap.total) * 100 : 0;
  $("progress-ok").style.width = `${pctOk}%`;
  $("progress-failed").style.width = `${pctFail}%`;
  $("progress-text").textContent = `${fmtInt(snap.sent)} / ${fmtInt(snap.total)}`;
  $("progress-pct").textContent = `${nf1.format(snap.total ? (snap.sent / snap.total) * 100 : 0)} %`;

  const lat = snap.latency || {};
  $("k-sent").textContent = fmtInt(snap.sent);
  $("k-sent-cap").textContent = `von ${fmtInt(snap.total)} · offen ${fmtInt(snap.total - snap.sent)}`;
  $("k-ok").textContent = fmtInt(snap.ok);
  $("k-ok-cap").textContent = snap.sent ? `${nf1.format((snap.ok / snap.sent) * 100)} % Erfolgsquote` : " ";
  $("k-failed").textContent = fmtInt(snap.failed);
  $("k-failed-cap").textContent = snap.sent ? `${nf1.format((snap.failed / snap.sent) * 100)} % Fehlerquote` : " ";
  $("k-inflight").textContent = fmtInt(snap.in_flight);
  $("k-inflight-cap").textContent = `Limit ${fmtInt(snap.concurrency)} parallel`;
  $("k-rate").textContent = fmtRate(snap.rate_now);
  $("k-rate-cap").textContent = `Ø ${fmtRate(snap.rate_avg)} msg/s · Limit ${snap.rate_limit > 0 ? fmtRate(snap.rate_limit) : "offen"}`;
  $("k-p95").textContent = fmtMs(lat.p95);
  $("k-lat-cap").textContent = `p50 ${fmtMs(lat.p50)} · p99 ${fmtMs(lat.p99)} · max ${fmtMs(lat.max)}`;
  $("k-elapsed").textContent = fmtDuration(snap.elapsed_s);
  const etaLabel = { completed: "fertig", paused: "pausiert", stopped: "gestoppt", failed: "–", interrupted: "–" }[snap.status];
  $("k-eta").textContent = etaLabel || fmtDuration(snap.eta_s);
  $("k-eta-cap").textContent = snap.eta_s && snap.status === "running"
    ? `≈ fertig um ${new Date(Date.now() + snap.eta_s * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" })}` : " ";

  const liveC = $("live-concurrency"), liveR = $("live-rate");
  const runChanged = !prev || prev.run_id !== snap.run_id;
  if (runChanged || document.activeElement !== liveC) { if (runChanged || !liveC.dataset.dirty) liveC.value = snap.concurrency; }
  if (runChanged || document.activeElement !== liveR) { if (runChanged || !liveR.dataset.dirty) liveR.value = snap.rate_limit; }

  const codes = Object.entries(snap.status_codes || {});
  $("status-codes").innerHTML = codes.length
    ? codes.map(([code, n]) => {
        const tone = /^2/.test(code) ? "success" : /^4/.test(code) ? "warning" : "danger";
        return `<span class="badge badge-${tone}">${esc(code)} · ${fmtInt(n)}</span>`;
      }).join("")
    : `<span class="hint">Noch keine Antworten.</span>`;

  const errors = snap.recent_errors || [];
  $("errors-body").innerHTML = errors.length
    ? errors.map((e) => `<tr>
        <td class="mono">${esc(fmtTime(e.at))}</td>
        <td class="path" title="${esc(e.path)}">${esc(e.path)}</td>
        <td>${badgeCode(e.code)}</td>
        <td class="mono ${e.mpl_id ? "copyable" : ""}" ${e.mpl_id ? `data-copy="${esc(e.mpl_id)}" title="Klicken zum Kopieren"` : ""}>${esc(e.mpl_id || "–")}</td>
        <td class="msg" title="${esc(e.error)}">${esc(e.error)}</td></tr>`).join("")
    : `<tr><td colspan="5" class="empty">Keine Fehler.</td></tr>`;

  renderChart(snap.timeline || []);
  updateButtons();

  // Beim Wechsel in einen Endzustand die Historie auffrischen.
  if (state.lastRunId !== snap.run_id || state.lastStatus !== snap.status) {
    if (TERMINAL.has(snap.status) || state.lastRunId !== snap.run_id) refreshHistory();
    state.lastRunId = snap.run_id;
    state.lastStatus = snap.status;
  }
}

function badgeCode(code) {
  const tone = /^2/.test(code) ? "success" : /^4/.test(code) ? "warning" : "danger";
  return `<span class="badge badge-${tone}">${esc(code)}</span>`;
}

function renderChart(points) {
  const box = $("chart");
  if (points.length < 2) {
    const text = state.active && TERMINAL.has(state.active.status)
      ? "Lauf zu kurz für eine Verlaufskurve." : "Die Kurve erscheint nach wenigen Sekunden.";
    box.innerHTML = `<div class="empty">${text}</div>`;
    return;
  }
  const W = box.clientWidth || 600, H = box.clientHeight || 200;
  const pad = { l: 44, r: 54, t: 8, b: 22 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const tMax = points[points.length - 1].t, tMin = points[0].t;
  const rMax = niceMax(Math.max(...points.map((p) => p.rate), 1));
  const lMax = niceMax(Math.max(...points.map((p) => p.p95 || 0), 1));
  const x = (t) => pad.l + ((t - tMin) / Math.max(tMax - tMin, 1)) * iw;
  const yR = (v) => pad.t + ih - (v / rMax) * ih;
  const yL = (v) => pad.t + ih - (v / lMax) * ih;
  const line = (sel, y) => points.filter((p) => sel(p) != null).map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(sel(p)).toFixed(1)}`).join("");
  const area = `${line((p) => p.rate, yR)}L${x(tMax).toFixed(1)},${pad.t + ih}L${x(tMin).toFixed(1)},${pad.t + ih}Z`;

  const grid = [0, 0.5, 1].map((f) => {
    const y = pad.t + ih - f * ih;
    return `<line class="grid" x1="${pad.l}" x2="${W - pad.r}" y1="${y}" y2="${y}"/>
      <text class="axis" x="${pad.l - 8}" y="${y + 4}" text-anchor="end">${rMax >= 10 ? nf0.format(rMax * f) : fmtRate(rMax * f)}</text>
      <text class="axis" x="${W - pad.r + 8}" y="${y + 4}">${nf0.format(lMax * f)}</text>`;
  }).join("");

  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Durchsatz und p95-Latenz über die Zeit">
    <defs><linearGradient id="rateFill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#00AFB4" stop-opacity=".18"/><stop offset="1" stop-color="#00AFB4" stop-opacity="0"/></linearGradient></defs>
    ${grid}
    <path d="${area}" fill="url(#rateFill)"/>
    <path d="${line((p) => p.rate, yR)}" fill="none" stroke="#00AFB4" stroke-width="2" stroke-linejoin="round"/>
    <path d="${line((p) => p.p95, yL)}" fill="none" stroke="#7D6EFF" stroke-width="1.75" stroke-linejoin="round" stroke-dasharray="4 3"/>
    <text class="axis" x="${pad.l}" y="${H - 4}">${fmtDuration(tMin)}</text>
    <text class="axis" x="${W - pad.r}" y="${H - 4}" text-anchor="end">${fmtDuration(tMax)}</text>
  </svg>`;
}

function niceMax(v) {
  const exp = Math.pow(10, Math.floor(Math.log10(v)));
  const f = v / exp;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * exp;
}

// -- Historie -------------------------------------------------------------------
let historyTimer = null;
function refreshHistory() {
  clearTimeout(historyTimer);
  historyTimer = setTimeout(loadHistory, 150);
}

async function loadHistory() {
  let runs;
  try { runs = await api("/api/runs?limit=50"); } catch (e) { return; }
  const body = $("history-body");
  if (!runs.length) { body.innerHTML = `<tr><td colspan="7" class="empty">Noch keine Läufe.</td></tr>`; return; }
  const busy = isBusy();
  body.innerHTML = runs.map((r) => {
    const s = r.summary || {};
    const lat = s.latency || {};
    const actions = [
      `<a class="btn btn-secondary btn-xs" href="/api/runs/${r.id}/export.csv" download>CSV</a>`,
      r.failed ? `<a class="btn btn-secondary btn-xs" href="/api/runs/${r.id}/export.csv?status=failed" download>Fehler-CSV</a>` : "",
      r.resumable ? `<button class="btn btn-primary btn-xs" data-action="resume" data-id="${r.id}" data-requires-idle ${busy ? "disabled" : ""}>Fortsetzen</button>` : "",
      r.failed && TERMINAL.has(r.status) ? `<button class="btn btn-secondary btn-xs" data-action="retry" data-id="${r.id}" data-requires-idle ${busy ? "disabled" : ""} title="Neuer Lauf nur mit den fehlgeschlagenen Dateien">Fehler senden</button>` : "",
      TERMINAL.has(r.status) ? `<button class="btn btn-ghost btn-xs" data-action="delete" data-id="${r.id}" title="Lauf löschen">✕</button>` : "",
    ].join("");
    return `<tr>
      <td class="mono nowrap">#${r.id}<span class="sub">${esc(fmtDateTime(r.started_at || r.created_at))}${r.parent_run_id ? ` · aus #${r.parent_run_id}` : ""}</span></td>
      <td class="path" title="/data/${esc(r.folder)}">/data/${esc(r.folder)}<span class="sub">${esc(r.endpoint_url.replace(/^https?:\/\/[^/]+/, ""))}</span></td>
      <td>${badge(r.status)}${r.message ? `<span class="sub" title="${esc(r.message)}">${esc(r.message.slice(0, 48))}${r.message.length > 48 ? " …" : ""}</span>` : ""}</td>
      <td class="num r nowrap">${fmtInt(r.ok)} ok${r.failed ? ` · <span class="fail">${fmtInt(r.failed)} Fehler</span>` : ""}<span class="sub">von ${fmtInt(r.total)}</span></td>
      <td class="num r nowrap">${s.rate_avg != null ? fmtRate(s.rate_avg) : "–"}</td>
      <td class="num r nowrap">${fmtMs(lat.p95)}</td>
      <td><div class="actions">${actions}</div></td></tr>`;
  }).join("");
  body.querySelectorAll("button[data-action]").forEach((b) =>
    b.addEventListener("click", () => historyAction(b.dataset.action, b.dataset.id)));
}

// -- Live-Stream ----------------------------------------------------------------
function renderScans(scans) {
  if (state.scanning === null || state.scanning === undefined) return;
  const scan = scans.find((s) => s.path === state.scanning) || scans[0];
  if (!scan) return;
  const item = document.querySelector("#folder-list .loading");
  if (item) item.innerHTML = `Lese Ordner ein … <span class="num">${fmtInt(scan.entries)}</span> Einträge`;
}

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      renderLive(data.active);
      renderScans(data.scans || []);
    } catch (e) { console.error(e); }
  };
  es.onerror = () => {
    const g = $("global-status");
    g.className = "badge badge-danger";
    g.querySelector(".label").textContent = "Keine Verbindung";
  };
}

// -- Init -----------------------------------------------------------------------
async function init() {
  loadForm();
  try {
    state.defaults = await api("/api/defaults");
    const d = state.defaults;
    if (!$("endpoint_url").value && d.endpoint_url) $("endpoint_url").value = d.endpoint_url;
    if (!$("token_url").value && d.token_url) $("token_url").value = d.token_url;
    if (!$("client_id").value && d.client_id) $("client_id").value = d.client_id;
    if (d.has_secret) $("client_secret").placeholder = "aus .env – leer lassen";
  } catch { /* Defaults sind optional */ }

  $("folder-refresh").addEventListener("click", () => openFolder(state.folder, true));
  $("servicekey-open").addEventListener("click", () => { $("servicekey-box").hidden = false; $("servicekey").focus(); });
  $("servicekey-cancel").addEventListener("click", () => { $("servicekey-box").hidden = true; $("servicekey").value = ""; });
  $("servicekey-apply").addEventListener("click", applyServiceKey);
  $("auth-test").addEventListener("click", testAuth);
  $("header-add").addEventListener("click", () => addHeaderRow().querySelector(".h-name").focus());
  $("start").addEventListener("click", startRun);
  $("btn-pause").addEventListener("click", () => control("pause"));
  $("btn-unpause").addEventListener("click", () => control("unpause"));
  $("btn-stop").addEventListener("click", () => control("stop"));
  $("live-apply").addEventListener("click", () => {
    delete $("live-concurrency").dataset.dirty;
    delete $("live-rate").dataset.dirty;
    applyLive();
  });
  ["live-concurrency", "live-rate"].forEach((id) => $(id).addEventListener("input", () => ($(id).dataset.dirty = "1")));

  document.querySelectorAll("[data-persist]").forEach((el) => el.addEventListener("change", saveForm));
  ["pattern", "recursive", "max_files"].forEach((id) => $(id).addEventListener(id === "pattern" ? "input" : "change", schedulePreview));
  ["concurrency", "rate_limit", "max_files"].forEach((id) => $(id).addEventListener("input", updatePlanSummary));
  document.querySelectorAll("[data-smoke]").forEach((chip) =>
    chip.addEventListener("click", () => {
      $("max_files").value = chip.dataset.smoke;
      saveForm();
      schedulePreview();
      updatePlanSummary();
    }));
  document.addEventListener("click", async (ev) => {
    const el = ev.target.closest("[data-copy]");
    if (!el) return;
    try { await navigator.clipboard.writeText(el.dataset.copy); toast("MPL-ID kopiert."); } catch { /* Clipboard gesperrt */ }
  });
  window.addEventListener("resize", () => state.active && renderChart(state.active.timeline || []));

  await openFolder(state.folder);
  loadHistory();
  connectEvents();
}

init();
