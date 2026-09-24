/* Shared helpers: API calls, toasts, modals, formatting. */

class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.status = status;
    this.data = data || {};
  }
}

async function api(method, url, data) {
  const opts = { method, headers: {} };
  if (data instanceof FormData) {
    opts.body = data;
  } else if (data !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(data);
  }
  let resp;
  try {
    resp = await fetch(url, opts);
  } catch (e) {
    throw new ApiError("Cannot reach the FebiTally server.", 0);
  }
  let json = {};
  try { json = await resp.json(); } catch (e) { /* non-JSON body */ }
  if (json && json.tally_response !== undefined) {
    document.dispatchEvent(new CustomEvent("tallyresponse", {
      detail: { method, url, ok: resp.ok, format: json.format, raw: json.tally_response, message: json.message || json.error },
    }));
  }
  if (!resp.ok) throw new ApiError(json.error || `Request failed (${resp.status})`, resp.status, json);
  return json;
}

function toast(message, type = "info", ms = 4000) {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = message;
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => el.remove(), type === "error" ? ms * 2 : ms);
}

/* Run an async action with a spinner on the button; show errors as a toast. */
async function withButton(btn, fn) {
  btn.disabled = true;
  btn.classList.add("loading");
  try {
    return await fn();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    btn.disabled = false;
    btn.classList.remove("loading");
  }
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const moneyFmt = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
function money(v) {
  const n = Number(v || 0);
  return n ? moneyFmt.format(n) : "";
}
/* Tally balances: negative = Dr, positive = Cr. */
function drcr(v) {
  const n = Number(v || 0);
  if (!n) return "0.00";
  return `${moneyFmt.format(Math.abs(n))} ${n < 0 ? "Dr" : "Cr"}`;
}
function fmtDate(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.slice(0, 10).split("-");
  return d && m && y ? `${d}-${m}-${y}` : iso;
}
function badge(status) {
  return `<span class="badge badge-${esc(status)}">${esc(status)}</span>`;
}

/* ---------- modal */
function openModal(id) { document.getElementById(id).classList.remove("hidden"); }
function closeModal(id) { document.getElementById(id).classList.add("hidden"); }
document.addEventListener("click", (e) => {
  const closer = e.target.closest("[data-close]");
  if (closer) closeModal(closer.dataset.close);
  if (e.target.classList.contains("modal-backdrop") && e.target.id !== "confirm-modal") e.target.classList.add("hidden");
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") document.querySelectorAll(".modal-backdrop:not(.hidden):not(#confirm-modal)").forEach((m) => m.classList.add("hidden"));
});

/* In-page confirmation dialog (replaces window.confirm). Resolves true on confirm, false otherwise.
   options: {title, detail, confirmText, cancelText, danger}
   Only one can be open: opening another cancels the earlier one, so one click never answers two. */
let closeConfirm = null;
function confirmDialog(message, options = {}) {
  if (closeConfirm) closeConfirm(false);
  const modal = document.getElementById("confirm-modal");
  const ok = document.getElementById("confirm-ok");
  const cancel = document.getElementById("confirm-cancel");
  const danger = !!options.danger;
  document.getElementById("confirm-title").textContent = options.title || "Are you sure?";
  document.getElementById("confirm-message").textContent = message;
  const detail = document.getElementById("confirm-detail");
  detail.textContent = options.detail || "";
  detail.classList.toggle("hidden", !options.detail);
  document.getElementById("confirm-icon").className = `confirm-icon ${danger ? "danger" : ""}`;
  ok.textContent = options.confirmText || "OK";
  ok.className = `btn ${danger ? "btn-danger-solid" : "btn-primary"}`;
  cancel.textContent = options.cancelText || "Cancel";

  const returnFocus = document.activeElement;
  modal.classList.remove("hidden");
  ok.focus();

  return new Promise((resolve) => {
    const finish = (result) => {
      closeConfirm = null;
      modal.classList.add("hidden");
      ok.removeEventListener("click", onOk);
      cancel.removeEventListener("click", onCancel);
      modal.removeEventListener("click", onBackdrop);
      document.removeEventListener("keydown", onKey, true);
      if (returnFocus && document.contains(returnFocus)) returnFocus.focus();
      resolve(result);
    };
    const onOk = () => finish(true);
    const onCancel = () => finish(false);
    const onBackdrop = (e) => { if (e.target === modal) finish(false); };
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); }
      if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); finish(document.activeElement !== cancel); }
      if (e.key === "Tab") { // keep focus inside the dialog
        e.preventDefault();
        (document.activeElement === ok ? cancel : ok).focus();
      }
    };
    closeConfirm = finish;
    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onCancel);
    modal.addEventListener("click", onBackdrop);
    document.addEventListener("keydown", onKey, true);
  });
}

/* Fill a <select> with companies open in Tally (falls back to companies with cached ledgers). */
async function loadCompanies(select, { remember = "febitally.company" } = {}) {
  let companies = [];
  let error = null;
  try {
    companies = (await api("GET", "/api/tally/companies")).companies;
  } catch (e) {
    error = e.message;
    try {
      companies = (await api("GET", "/api/ledgers/companies")).companies.map((c) => c.company);
    } catch (_) { /* ignore */ }
  }
  let saved = null;
  try { saved = localStorage.getItem(remember); } catch (_) { /* storage blocked */ }
  select.innerHTML = `<option value="">Select company…</option>` +
    companies.map((c) => `<option ${c === saved ? "selected" : ""}>${esc(c)}</option>`).join("");
  select.addEventListener("change", () => {
    try { localStorage.setItem(remember, select.value); } catch (_) { /* storage blocked */ }
  });
  return { companies, error };
}

/* Sidebar Tally status indicator. */
async function refreshTallyStatus() {
  const dot = document.getElementById("tally-dot");
  const label = document.getElementById("tally-label");
  try {
    const s = await api("GET", "/api/tally/status");
    dot.className = `dot ${s.ok ? "ok" : "err"}`;
    const where = `${s.host}:${s.port} · ${(s.format || "json").toUpperCase()}`;
    label.textContent = s.ok ? `Tally connected · ${where}` : `Tally offline · ${where}`;
    label.title = s.ok ? s.message : s.error;
    return s;
  } catch (e) {
    dot.className = "dot err";
    label.textContent = "Server unreachable";
    return { ok: false, error: e.message };
  }
}
const tallyStatus = refreshTallyStatus();

/* ---------- Tally response format (set on the Settings page) */

/* Render "Tally API: XML" chips (elements with [data-format-chip]) linking to Settings. */
tallyStatus.then((s) => {
  const fmt = (s.format || "json").toUpperCase();
  document.querySelectorAll("[data-format-chip]").forEach((el) => {
    el.innerHTML = `<a class="format-chip" href="/settings" title="Requests and responses use ${fmt}. Change on the Settings page.">
      <span class="k">Tally API</span>${fmt}</a>`;
  });
});

/* Indent a raw Tally response for display: JSON is re-serialised, XML gets one tag per line. */
function prettyResponse(raw) {
  const text = String(raw || "").trim();
  if (!text) return "(empty response)";
  if (text[0] === "{" || text[0] === "[") {
    try { return JSON.stringify(JSON.parse(text), null, 2); } catch (_) { return text; }
  }
  if (text[0] !== "<" || /\n\s*</.test(text)) return text; // already indented
  let depth = 0;
  return text.replace(/>\s*</g, ">\n<").split("\n").map((line) => {
    if (/^<\//.test(line)) depth = Math.max(depth - 1, 0);
    const out = "  ".repeat(depth) + line;
    if (/^<[^!?\/][^>]*[^\/]>$/.test(line) && !/<\/[^>]+>$/.test(line)) depth += 1;
    return out;
  }).join("\n");
}

function responseFormat(raw) {
  const t = String(raw || "").trim();
  return t.startsWith("<") ? "XML" : t.startsWith("{") || t.startsWith("[") ? "JSON" : "text";
}

function showResponse(title, raw, sub) {
  document.getElementById("response-title").textContent = title;
  document.getElementById("response-sub").textContent = sub || `Raw response from Tally · ${responseFormat(raw)}`;
  document.getElementById("response-body").textContent = prettyResponse(raw);
  openModal("response-modal");
}

document.addEventListener("click", (e) => {
  if (e.target.id !== "response-copy") return;
  const text = document.getElementById("response-body").textContent;
  navigator.clipboard.writeText(text).then(() => toast("Copied.", "success"), () => toast("Copy failed.", "error"));
});

/* "Last Tally response" button (#last-response-btn): enabled after any call that talked to Tally. */
let lastTallyResponse = null;
document.addEventListener("tallyresponse", (e) => {
  lastTallyResponse = Object.assign({ at: new Date() }, e.detail);
  const btn = document.getElementById("last-response-btn");
  if (btn) {
    btn.disabled = false;
    btn.classList.toggle("btn-danger", !e.detail.ok);
    btn.title = `${e.detail.ok ? "OK" : "Error"} · ${e.detail.message || ""}`;
  }
});
document.addEventListener("click", (e) => {
  if (!e.target.closest("#last-response-btn") || !lastTallyResponse) return;
  const r = lastTallyResponse;
  showResponse("Last Tally response", r.raw,
    `${(r.format || "").toUpperCase()} · ${r.ok ? "OK" : "Error"} · ${r.message || ""} · ${r.at.toLocaleTimeString()}`);
});
