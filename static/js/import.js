(async function () {
  const $ = (id) => document.getElementById(id);
  const FIELDS = [
    ["txn_date", "Date", true], ["narration", "Narration / Description", true], ["ref_no", "Ref / Chq no."],
    ["debit", "Withdrawal (Dr)"], ["credit", "Deposit (Cr)"], ["balance", "Balance"],
    ["amount", "Amount (single column)"], ["drcr", "Dr/Cr indicator"], ["ledger", "Ledger (Tally)"],
  ];
  const FIELD_LABEL = Object.fromEntries(FIELDS.map(([k, l]) => [k, l]));

  const companySel = $("company");
  let companyLedgers = [];
  let file = null;
  let current = null; // {import, entries}
  let filter = "all";
  const selected = new Set();

  /* ------------------------------------------------------------ step 1: company + ledgers */

  const { error } = await loadCompanies(companySel);
  if (error) {
    $("tally-alert").innerHTML = `Tally Prime is not reachable: ${esc(error.replace(/\.$/, ""))}. Check <a href="/settings">Settings</a> — the company must be open in Tally to import.`;
    $("tally-alert").classList.remove("hidden");
  }

  async function onCompanyChange() {
    const company = companySel.value;
    companyLedgers = [];
    $("ledger-status").innerHTML = "";
    if (!company) return updateSteps();
    const r = await api("GET", `/api/ledgers?company=${encodeURIComponent(company)}`);
    companyLedgers = r.ledgers;
    if (!companyLedgers.length) {
      $("ledger-status").innerHTML = `
        <div class="alert alert-warning" style="margin:0;display:flex;align-items:center;gap:12px;justify-content:space-between">
          <span>Ledgers for <b>${esc(company)}</b> have not been fetched from Tally yet. They are needed to map entries.</span>
          <button class="btn btn-primary btn-sm" id="fetch-now">Fetch ledgers now</button>
        </div>`;
      $("fetch-now").addEventListener("click", (e) => withButton(e.target, async () => {
        const f = await api("POST", "/api/ledgers/fetch", { company });
        toast(f.message, "success");
        await onCompanyChange();
      }));
    } else {
      const fetched = companyLedgers.reduce((m, l) => (l.fetched_at > m ? l.fetched_at : m), "");
      $("ledger-status").innerHTML = `<div class="muted small">${companyLedgers.length} ledgers cached (fetched ${esc(fetched)}).
        <a href="#" id="refetch">Fetch again</a></div>`;
      $("refetch").addEventListener("click", (e) => {
        e.preventDefault();
        withButton(e.target, async () => {
          const f = await api("POST", "/api/ledgers/fetch", { company });
          toast(f.message, "success");
          await onCompanyChange();
        });
      });
    }
    updateSteps();
  }

  companySel.addEventListener("change", () => onCompanyChange().catch((e) => toast(e.message, "error")));

  function updateSteps() {
    const s1 = companySel.value && companyLedgers.length;
    $("step-1").className = `step ${s1 ? "done" : "active"}`;
    $("step-2").className = `step ${s1 ? "active" : ""}`;
    $("step-3").className = "step";
    $("upload-btn").disabled = !(s1 && file);
  }

  /* ------------------------------------------------------------ step 2: upload */

  function setFile(f) {
    file = f;
    $("file-label").textContent = f ? `${f.name} (${(f.size / 1024).toFixed(0)} KB)` : "Drop the bank statement here, or click to choose";
    updateSteps();
  }
  $("file").addEventListener("change", (e) => setFile(e.target.files[0] || null));
  const dz = $("dropzone");
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => setFile(e.dataTransfer.files[0] || null));

  /* ------------------------------------------------------------ column mapping dialog */

  let preview = null; // last /api/imports/preview response
  let previewTimer = null;
  let previewSeq = 0;

  const colName = (i) => {
    let n = i + 1, out = "";
    while (n > 0) { const m = (n - 1) % 26; out = String.fromCharCode(65 + m) + out; n = Math.floor((n - 1) / 26); }
    return out;
  };

  $("upload-btn").addEventListener("click", () => withButton($("upload-btn"), async () => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("company", companySel.value);
    try {
      preview = await api("POST", "/api/imports/preview", fd);
    } catch (e) {
      if (e.data.need_password) return askPassword(e.data);
      throw e;
    }
    askBank();
  }));

  /* ------------------------------------------------------------ which bank ledger (popup) */

  const BANK_ONLY = ["bank accounts", "bank od a/c", "bank occ a/c"];
  let bankFromMapping = false; // opened from the mapping dialog's "Change" link

  function bankLedgers(showAll) {
    if (showAll) return companyLedgers;
    return companyLedgers.filter((l) => {
      const group = (l.parent || "").toLowerCase();
      return group !== "cash-in-hand" && (l.cash_bank || BANK_ONLY.includes(group));
    });
  }

  function fillBankChoice() {
    const list = bankLedgers($("bank-show-all").checked);
    const want = $("bank-choice").value || preview.bank_ledger || (preview.bank_suggestion || {}).ledger || lastBank();
    $("bank-choice").innerHTML = `<option value="">Select bank ledger…</option>` + list.map((l) =>
      `<option value="${esc(l.name)}">${esc(l.name)}${l.parent ? " — " + esc(l.parent) : ""}</option>`).join("");
    const pick = list.some((l) => l.name === want) ? want : (list.length === 1 ? list[0].name : "");
    $("bank-choice").value = pick;
    $("bank-empty").classList.toggle("hidden", list.length > 0);
    showBankReason();
  }

  function showBankReason() {
    const s = preview.bank_suggestion;
    const v = $("bank-choice").value;
    let reason = "";
    if (v && s && s.ledger === v) reason = `✓ ${s.reason}.`;
    else if (v && v === lastBank()) reason = "Last bank used for this company.";
    $("bank-reason").textContent = reason;
  }

  function lastBank() {
    try { return localStorage.getItem(`febitally.bank.${companySel.value}`) || ""; } catch (_) { return ""; }
  }

  function askBank(fromMapping = false) {
    bankFromMapping = fromMapping;
    $("bank-file").textContent = preview.filename;
    const facts = [];
    if (preview.account_number) facts.push(`Account no. <b>…${esc(preview.account_number.slice(-4))}</b> found in the statement`);
    facts.push(`${preview.count} transaction${preview.count === 1 ? "" : "s"} detected`);
    $("bank-facts").innerHTML = facts.map((f) => `<span>${f}</span>`).join("");
    $("bank-show-all").checked = false;
    $("bank-choice").value = "";
    fillBankChoice();
    openModal("bank-modal");
    $("bank-choice").focus();
  }

  $("bank-show-all").addEventListener("change", fillBankChoice);
  $("bank-choice").addEventListener("change", showBankReason);
  $("bank-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const bank = $("bank-choice").value;
    if (!bank) return toast("Select the bank ledger for this statement.", "error");
    preview.bank_ledger = bank;
    try { localStorage.setItem(`febitally.bank.${companySel.value}`, bank); } catch (_) { /* storage blocked */ }
    closeModal("bank-modal");
    if (bankFromMapping) $("map-bank").textContent = bank;
    else openMappingDialog();
  });

  /* ------------------------------------------------------------ password-protected statements */

  // The password lives only in this page's memory (preview.password) and is sent with each request
  // that has to open the file. The server never stores or logs it.
  let pendingUnlock = null; // {token, filename} of the upload waiting for its password

  function askPassword(data) {
    pendingUnlock = { token: data.token, filename: data.filename };
    $("pw-file").textContent = data.filename || "";
    $("pw-input").value = "";
    $("pw-input").type = "password";
    $("pw-toggle").textContent = "Show";
    showPasswordError(data.wrong_password ? data.error : "");
    openModal("password-modal");
    $("pw-input").focus();
  }

  function showPasswordError(message) {
    $("pw-error").textContent = message || "";
    $("pw-error").classList.toggle("hidden", !message);
    $("pw-input").classList.toggle("invalid", !!message);
  }

  $("pw-toggle").addEventListener("click", () => {
    const show = $("pw-input").type === "password";
    $("pw-input").type = show ? "text" : "password";
    $("pw-toggle").textContent = show ? "Hide" : "Show";
    $("pw-input").focus();
  });

  $("password-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const password = $("pw-input").value;
    if (!password) return showPasswordError("Enter the password.");
    withButton($("pw-unlock"), async () => {
      try {
        const r = await api("POST", "/api/imports/preview", { token: pendingUnlock.token, password, company: companySel.value });
        preview = Object.assign(r, { password });
      } catch (err) {
        if (err.data.need_password) {
          showPasswordError(err.message);
          $("pw-input").select();
          return;
        }
        throw err;
      }
      pendingUnlock = null;
      $("pw-input").value = "";
      closeModal("password-modal");
      askBank();
    });
  });

  function currentMapping() {
    const mapping = {};
    document.querySelectorAll("[data-map]").forEach((sel) => { if (sel.value !== "") mapping[sel.dataset.map] = Number(sel.value); });
    return mapping;
  }
  function currentHeaderRow() {
    const v = $("map-header-row").value;
    return v === "" ? null : Number(v);
  }

  function openMappingDialog() {
    const p = preview;
    $("map-sub").textContent = `${p.filename}${p.password ? " · password-protected (unlocked)" : ""} · ${p.total_rows} rows in file · ${companySel.value}`;
    $("map-bank").textContent = p.bank_ledger;
    const tableMode = p.mode === "table";
    $("map-controls").classList.toggle("hidden", !tableMode);
    $("map-raw-wrap").classList.toggle("hidden", !tableMode);

    if (tableMode) {
      // header row choices: every previewed row that has some text
      $("map-header-row").innerHTML = `<option value="">No header row (data starts at row 1)</option>` +
        p.raw.map((r, i) => {
          const text = r.filter((c) => String(c).trim()).slice(0, 4).join(" · ");
          return text ? `<option value="${i}">Row ${i + 1}: ${esc(text.slice(0, 60))}</option>` : "";
        }).join("");
      $("map-header-row").value = p.header_row ?? "";
      renderFieldSelects(p.mapping);
    }
    renderPreview();
    openModal("mapping-modal");
  }

  function renderFieldSelects(mapping) {
    const header = currentHeaderRow() !== null ? preview.raw[currentHeaderRow()] || [] : [];
    const options = Array.from({ length: preview.columns }, (_, i) => {
      const title = String(header[i] ?? "").trim();
      return `<option value="${i}">${colName(i)}${title ? " · " + esc(title.slice(0, 28)) : ""}</option>`;
    }).join("");
    $("map-fields").innerHTML = FIELDS.map(([key, label, required]) => `
      <div class="field">
        <label for="map-${key}">${label}${required ? ' <span style="color:var(--danger)">*</span>' : ""}</label>
        <select id="map-${key}" data-map="${key}"><option value="">— not in statement —</option>${options}</select>
      </div>`).join("");
    Object.entries(mapping || {}).forEach(([k, v]) => {
      const sel = document.querySelector(`[data-map="${k}"]`);
      if (sel) sel.value = String(v);
    });
    markSelects();
  }

  function markSelects() {
    const used = {};
    document.querySelectorAll("[data-map]").forEach((sel) => {
      if (sel.value !== "") used[sel.value] = (used[sel.value] || 0) + 1;
    });
    document.querySelectorAll("[data-map]").forEach((sel) => {
      sel.classList.toggle("mapped", sel.value !== "" && used[sel.value] === 1);
      sel.classList.toggle("clash", sel.value !== "" && used[sel.value] > 1);
    });
    return Object.values(used).some((n) => n > 1);
  }

  function renderRaw() {
    const p = preview;
    const headerRow = currentHeaderRow();
    const byCol = {};
    Object.entries(currentMapping()).forEach(([k, v]) => { (byCol[v] = byCol[v] || []).push(FIELD_LABEL[k]); });
    const cols = Array.from({ length: p.columns }, (_, i) => i);
    $("map-raw").innerHTML =
      `<thead><tr><th class="rownum">#</th>${cols.map((i) =>
        `<th class="${byCol[i] ? "col-mapped" : ""}">${colName(i)}${byCol[i] ? `<span class="col-tag">${esc(byCol[i].join(", "))}</span>` : ""}</th>`).join("")}</tr></thead>` +
      `<tbody>${p.raw.map((r, ri) => `
        <tr data-row="${ri}" class="${ri === headerRow ? "is-header" : headerRow !== null && ri < headerRow ? "before-header" : ""}"
            title="Click to use row ${ri + 1} as the header row">
          <td class="rownum">${ri + 1}</td>
          ${cols.map((i) => `<td class="${byCol[i] ? "col-mapped" : ""}">${esc(r[i] ?? "")}</td>`).join("")}
        </tr>`).join("")}</tbody>`;
    $("map-raw-note").textContent = p.total_rows > p.raw.length
      ? `(first ${p.raw.length} of ${p.total_rows} rows — click a row to use it as the header)`
      : "(click a row to use it as the header)";
  }

  function renderPreview() {
    const p = preview;
    const clash = p.mode === "table" && markSelects();
    if (p.mode === "table") renderRaw();

    const rows = p.sample || [];
    // Ledger column from the statement: show whether each name is a ledger fetched from Tally
    const hasLedger = p.mode === "table" && currentMapping().ledger !== undefined;
    const known = new Map(companyLedgers.map((l) => [l.name.toLowerCase(), l.name]));
    const ledgerCell = (name) => {
      if (!name) return `<td class="muted">—</td>`;
      const match = known.get(name.toLowerCase());
      return match
        ? `<td title="Found in Tally"><span style="color:var(--success)">&#10003;</span> ${esc(match)}</td>`
        : `<td title="Not a ledger in Tally — a suggestion will be used instead"><span style="color:var(--danger)">&#10007;</span> ${esc(name)}</td>`;
    };
    const unknown = hasLedger ? rows.filter((r) => r.ledger && !known.has(r.ledger.toLowerCase())).length : 0;
    let alert = "";
    if (p.mode === "text") {
      alert = `<div class="alert alert-info">This PDF has no table, so transactions were read line by line.
        Check the parsed entries below; if they look wrong, export the statement as Excel instead.</div>`;
    } else if (clash) {
      alert = `<div class="alert alert-error">The same column is mapped to more than one field.</div>`;
    } else if (p.problem) {
      alert = `<div class="alert alert-warning">${esc(p.problem)}</div>`;
    } else if (p.detected && !p.userChanged) {
      alert = `<div class="alert alert-success">Columns were detected automatically. Check them below, then import.</div>`;
    }
    if (!alert.includes("alert-error") && unknown) {
      alert += `<div class="alert alert-warning">${unknown} ledger name${unknown === 1 ? "" : "s"} in the preview ${unknown === 1 ? "is" : "are"} not in Tally
        (marked &#10007;). Those entries get a suggested ledger instead; create missing ledgers on the Ledgers page or fix them after import.</div>`;
    }
    $("map-alert").innerHTML = alert;

    $("map-count").textContent = p.count ? `(${p.count} transaction${p.count === 1 ? "" : "s"}${p.count > rows.length ? `, first ${rows.length} shown` : ""})` : "";
    $("map-sample").innerHTML = rows.length
      ? `<thead><tr><th>Date</th><th>Narration</th><th>Ref / Chq</th><th class="num">Withdrawal</th><th class="num">Deposit</th><th class="num">Balance</th>${hasLedger ? "<th>Ledger</th>" : ""}</tr></thead>
         <tbody>${rows.map((r) => `<tr><td>${fmtDate(r.txn_date)}</td><td>${esc(r.narration)}</td><td>${esc(r.ref_no)}</td>
           <td class="num">${money(r.debit)}</td><td class="num">${money(r.credit)}</td>
           <td class="num">${r.balance === null || r.balance === undefined ? "" : money(r.balance)}</td>
           ${hasLedger ? ledgerCell(r.ledger) : ""}</tr>`).join("")}</tbody>`
      : `<tbody><tr><td class="empty">No transactions with the current mapping.</td></tr></tbody>`;

    const ok = p.count > 0 && !clash;
    $("mapping-apply").disabled = !ok;
    $("mapping-apply").textContent = ok ? `Import ${p.count} entr${p.count === 1 ? "y" : "ies"}` : "Import entries";
    $("map-status").textContent = "";
  }

  /* Re-parse on the server with the mapping shown in the dialog (debounced). */
  function refreshPreview() {
    clearTimeout(previewTimer);
    $("map-status").textContent = "Updating preview…";
    $("mapping-apply").disabled = true;
    const seq = ++previewSeq;
    previewTimer = setTimeout(async () => {
      try {
        const r = await api("POST", "/api/imports/preview", {
          token: preview.token, mapping: currentMapping(), header_row: currentHeaderRow(),
          password: preview.password || undefined,
        });
        if (seq !== previewSeq) return; // a newer change is in flight
        Object.assign(preview, { count: r.count, sample: r.sample, problem: r.problem, userChanged: true });
        renderPreview();
      } catch (e) {
        $("map-status").textContent = "";
        toast(e.message, "error");
      }
    }, 250);
  }

  $("map-fields").addEventListener("change", () => { markSelects(); renderRaw(); refreshPreview(); });
  $("map-header-row").addEventListener("change", () => {
    renderFieldSelects(currentMapping()); // re-label columns with the new header titles
    renderRaw();
    refreshPreview();
  });
  $("map-change-bank").addEventListener("click", (e) => {
    e.preventDefault();
    askBank(true);
  });

  $("map-raw").addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-row]");
    if (!tr) return;
    $("map-header-row").value = tr.dataset.row;
    $("map-header-row").dispatchEvent(new Event("change"));
  });

  $("mapping-apply").addEventListener("click", () => withButton($("mapping-apply"), async () => {
    const fd = new FormData();
    fd.append("company", companySel.value);
    fd.append("bank_ledger", preview.bank_ledger);
    fd.append("token", preview.token);
    if (preview.password) fd.append("password", preview.password);
    if (preview.mode === "table") {
      fd.append("mapping", JSON.stringify(currentMapping()));
      const hr = currentHeaderRow();
      if (hr !== null) fd.append("header_row", hr);
    }
    try {
      const r = await api("POST", "/api/imports", fd);
      toast(r.message, r.unmatched_ledgers ? "info" : "success", r.unmatched_ledgers ? 8000 : 4000);
      closeModal("mapping-modal");
      preview = null;
      setFile(null);
      $("file").value = "";
      await openImport(r.import_id);
    } catch (e) {
      if (e.data.need_fetch) {
        closeModal("mapping-modal");
        await onCompanyChange();
      }
      throw e;
    }
  }));

  /* ------------------------------------------------------------ previous imports */

  async function loadImports() {
    const r = await api("GET", "/api/imports");
    $("imports").innerHTML = r.imports.length ? r.imports.map((i) => `
      <tr>
        <td>#${i.id}</td>
        <td><a href="?id=${i.id}" data-open="${i.id}">${esc(i.filename)}</a></td>
        <td>${esc(i.company)}</td><td>${esc(i.bank_ledger)}</td>
        <td class="small muted">${esc(i.created_at)}</td>
        <td class="num">${i.total}</td><td class="num">${i.pending || 0}</td><td class="num">${i.validated || 0}</td>
        <td class="num">${i.pushed || 0}</td><td class="num">${i.failed || 0}</td>
        <td>${badge(i.status)}</td>
        <td class="num">
          <button class="btn btn-sm" data-open="${i.id}">Open</button>
          <button class="btn btn-sm btn-danger" data-delete-import="${i.id}">Delete</button>
        </td>
      </tr>`).join("")
      : `<tr><td colspan="12" class="empty">No imports yet.</td></tr>`;
  }

  $("imports").addEventListener("click", async (e) => {
    const openId = e.target.dataset.open;
    const delId = e.target.dataset.deleteImport;
    if (openId) {
      e.preventDefault();
      openImport(Number(openId)).catch((err) => toast(err.message, "error"));
    }
    if (delId) {
      if (!(await confirmDialog(`Delete import #${delId} and all its entries from FebiTally?`, {
        title: "Delete import", detail: "Vouchers already pushed to Tally stay in Tally.",
        confirmText: "Delete import", danger: true,
      }))) return;
      withButton(e.target, async () => {
        await api("DELETE", `/api/imports/${delId}`);
        toast("Import deleted.", "success");
        await loadImports();
      });
    }
  });

  /* ------------------------------------------------------------ step 3: review */

  async function openImport(id) {
    const imp = await api("GET", `/api/imports/${id}`);
    const lr = await api("GET", `/api/ledgers?company=${encodeURIComponent(imp.import.company)}`);
    if (imp.voucher_types_corrected) {
      toast(`${imp.voucher_types_corrected} row(s) switched voucher type to match their ledger (Contra for cash/bank ledgers).`, "info", 7000);
    }
    current = imp;
    current.ledgers = lr.ledgers.map((l) => l.name);
    current.ledgerInfo = new Map(lr.ledgers.map((l) => [l.name, l]));
    ["entry-search", "f-from", "f-to", "f-dir", "f-vtype", "f-ledger-mode", "f-ledger", "f-min", "f-max"]
      .forEach((fid) => { $(fid).value = ""; });
    $("f-ledger").classList.add("hidden");
    filter = "all";
    document.querySelectorAll("#filters button").forEach((b) => b.classList.toggle("active", b.dataset.filter === "all"));
    selected.clear();
    renderLedgerList();
    $("setup").classList.add("hidden");
    $("review").classList.remove("hidden");
    $("new-import-btn").classList.remove("hidden");
    $("step-1").className = "step done";
    $("step-2").className = "step done";
    $("step-3").className = "step active";
    history.replaceState(null, "", `?id=${id}`);
    renderReview();
  }

  /* Suggestion list: each ledger labelled with its group; cash/bank ledgers flagged for Contra. */
  function renderLedgerList() {
    $("ledger-list").innerHTML = current.ledgers.map((n) => {
      const l = current.ledgerInfo.get(n) || {};
      const label = [l.parent, l.cash_bank ? "cash/bank · Contra" : ""].filter(Boolean).join(" · ");
      return `<option value="${esc(n)}" label="${esc(label)}">`;
    }).join("");
  }

  async function reloadImport() {
    const imp = await api("GET", `/api/imports/${current.import.id}`);
    current.import = imp.import;
    current.entries = imp.entries;
    renderReview();
  }

  const locked = (e) => e.status === "pushed" || e.status === "skipped";

  /* ------------------------------------------------------------ filters */

  // Status tab + search + the Filters panel. Everything is filtered in the browser.
  function filterValues() {
    const num = (id) => ($(id).value === "" ? null : Number($(id).value));
    return {
      q: $("entry-search").value.trim().toLowerCase(),
      from: $("f-from").value, to: $("f-to").value,
      dir: $("f-dir").value, vtype: $("f-vtype").value,
      ledgerMode: $("f-ledger-mode").value, ledger: $("f-ledger").value.trim(),
      min: num("f-min"), max: num("f-max"),
    };
  }

  function matches(e, f, ignoreStatus = false) {
    if (!ignoreStatus && filter !== "all" && e.status !== filter) return false;
    if (f.q && ![e.narration, e.ledger, e.ref_no].some((v) => (v || "").toLowerCase().includes(f.q))) return false;
    if (f.from && e.txn_date < f.from) return false;
    if (f.to && e.txn_date > f.to) return false;
    const out = Number(e.debit || 0) > 0;
    if (f.dir === "out" && !out) return false;
    if (f.dir === "in" && out) return false;
    if (f.vtype && e.voucher_type !== f.vtype) return false;
    if (f.ledgerMode === "unset" && e.ledger) return false;
    if (f.ledgerMode === "missing" && !(e.ledger ? !canonicalLedger(e.ledger) : STATEMENT_LEDGER_RE.test(e.error || ""))) return false;
    if (f.ledgerMode === "is" && f.ledger && ledgerKey(e.ledger) !== ledgerKey(f.ledger)) return false;
    const amount = Number(e.debit || 0) || Number(e.credit || 0);
    if (f.min !== null && amount < f.min) return false;
    if (f.max !== null && amount > f.max) return false;
    return true;
  }

  function visibleEntries() {
    const f = filterValues();
    return current.entries.filter((e) => matches(e, f));
  }

  /* Chips for the active filters (each removable), and the count shown on the Filters button. */
  function activeFilters() {
    const f = filterValues();
    const list = [];
    const add = (label, clear) => list.push({ label, clear });
    if (f.q) add(`Search: “${$("entry-search").value.trim()}”`, () => { $("entry-search").value = ""; });
    if (f.from || f.to) {
      add(f.from && f.to ? `${fmtDate(f.from)} – ${fmtDate(f.to)}` : f.from ? `From ${fmtDate(f.from)}` : `Until ${fmtDate(f.to)}`,
        () => { $("f-from").value = ""; $("f-to").value = ""; });
    }
    if (f.dir) add(f.dir === "out" ? "Withdrawals" : "Deposits", () => { $("f-dir").value = ""; });
    if (f.vtype) add(`Voucher: ${f.vtype}`, () => { $("f-vtype").value = ""; });
    if (f.ledgerMode === "unset") add("Ledger not set", () => { $("f-ledger-mode").value = ""; });
    if (f.ledgerMode === "missing") add("Ledger not in Tally", () => { $("f-ledger-mode").value = ""; });
    if (f.ledgerMode === "is" && f.ledger) add(`Ledger: ${f.ledger}`, () => { $("f-ledger-mode").value = ""; $("f-ledger").value = ""; });
    if (f.min !== null || f.max !== null) {
      add(f.min !== null && f.max !== null ? `Amount ${money(f.min) || "0.00"} – ${money(f.max) || "0.00"}`
        : f.min !== null ? `Amount ≥ ${money(f.min) || "0.00"}` : `Amount ≤ ${money(f.max) || "0.00"}`,
      () => { $("f-min").value = ""; $("f-max").value = ""; });
    }
    return list;
  }

  function onFiltersChanged() {
    $("f-ledger").classList.toggle("hidden", $("f-ledger-mode").value !== "is");
    // bulk actions only ever apply to rows you can see
    const visible = new Set(visibleEntries().map((e) => e.id));
    [...selected].forEach((id) => { if (!visible.has(id)) selected.delete(id); });
    renderReview();
  }

  function clearFilters() {
    ["entry-search", "f-from", "f-to", "f-dir", "f-vtype", "f-ledger-mode", "f-ledger", "f-min", "f-max"]
      .forEach((id) => { $(id).value = ""; });
    filter = "all";
    document.querySelectorAll("#filters button").forEach((b) => b.classList.toggle("active", b.dataset.filter === "all"));
    onFiltersChanged();
  }

  $("filter-toggle").addEventListener("click", () => {
    const open = $("filter-panel").classList.toggle("hidden") === false;
    $("filter-toggle").setAttribute("aria-expanded", String(open));
    if (open) $("f-from").focus();
  });
  ["f-from", "f-to", "f-dir", "f-vtype", "f-ledger-mode", "f-ledger", "f-min", "f-max"].forEach((id) => {
    $(id).addEventListener("input", onFiltersChanged);
    $(id).addEventListener("change", onFiltersChanged);
  });
  $("f-ledger-mode").addEventListener("change", () => { if ($("f-ledger-mode").value === "is") $("f-ledger").focus(); });
  $("filter-clear").addEventListener("click", clearFilters);
  $("filter-chips").addEventListener("click", (e) => {
    const i = e.target.dataset.removeFilter;
    if (i === undefined) return;
    const f = activeFilters()[Number(i)];
    if (f) { f.clear(); onFiltersChanged(); }
  });

  /* ------------------------------------------------------------ create a missing ledger */

  const STATEMENT_LEDGER_RE = /^Ledger '(.+)' from the statement is not a ledger in Tally/;
  const ledgerKey = (name) => String(name || "").trim().toLowerCase();
  const canonicalLedger = (name) => current.ledgers.find((n) => ledgerKey(n) === ledgerKey(name));

  /* Cash/bank ledgers take Contra vouchers (same rule as the server: flag from Fetch, or own group). */
  const CASH_BANK_GROUPS = ["cash-in-hand", "bank accounts", "bank od a/c", "bank occ a/c"];
  function isCashBank(name) {
    const l = current.ledgerInfo.get(canonicalLedger(name) || "");
    return !!l && (!!l.cash_bank || CASH_BANK_GROUPS.includes(ledgerKey(l.parent)));
  }
  function voucherTypeFor(entry, ledger) {
    if (isCashBank(ledger)) return "Contra";
    return Number(entry.debit || 0) > 0 ? "Payment" : "Receipt";
  }

  /* The name a row wants but Tally lacks: typed into the box, or from a mapped statement column. */
  function missingLedger(e) {
    if (locked(e)) return "";
    if (e.ledger && !canonicalLedger(e.ledger)) return e.ledger.trim();
    const m = STATEMENT_LEDGER_RE.exec(e.error || "");
    return m && !canonicalLedger(m[1]) ? m[1] : "";
  }

  function createButton(e) {
    const name = missingLedger(e);
    return name
      ? `<button type="button" class="link-btn" data-create="${e.id}" title="Create this ledger in Tally and use it here">+ Create “${esc(name)}” in Tally</button>`
      : "";
  }

  let groupCache = { company: null, groups: [] };
  async function loadGroupsFor(company) {
    if (groupCache.company === company && groupCache.groups.length) return groupCache.groups;
    let groups = [];
    try {
      groups = (await api("GET", `/api/ledgers/groups?company=${encodeURIComponent(company)}`)).groups;
    } catch (_) { /* Tally unreachable: fall back to groups seen in fetched ledgers */ }
    const lr = await api("GET", `/api/ledgers?company=${encodeURIComponent(company)}`);
    groups = [...new Set(groups.concat(lr.ledgers.map((l) => l.parent).filter(Boolean)))].sort();
    groupCache = { company, groups };
    return groups;
  }

  let createFor = null; // {entryId, name}

  function rowsWanting(name) {
    const key = ledgerKey(name);
    return current.entries.filter((e) => !locked(e) && ledgerKey(missingLedger(e)) === key);
  }

  async function openCreateLedger(entryId, name) {
    name = (name || "").trim();
    if (!name) return;
    createFor = { entryId: Number(entryId), name };
    const entry = entryById(entryId);
    $("cl-name").value = name;
    $("cl-opening").value = 0;
    let lastGroup = "";
    try { lastGroup = localStorage.getItem("febitally.newLedgerGroup") || ""; } catch (_) { /* storage blocked */ }
    $("cl-parent").value = lastGroup;
    $("cl-context").textContent = entry
      ? `For ${fmtDate(entry.txn_date)} · ${entry.narration} · ${money(entry.debit || entry.credit)} (${entry.voucher_type})`
      : "";
    const others = rowsWanting(name).filter((e) => e.id !== createFor.entryId);
    $("cl-apply-all-wrap").classList.toggle("hidden", !others.length);
    $("cl-apply-all").checked = true;
    $("cl-apply-all-label").textContent = `Also use it for ${others.length} other row${others.length === 1 ? "" : "s"} wanting “${name}”`;
    openModal("create-ledger-modal");
    $(lastGroup ? "cl-name" : "cl-parent").focus();
    const groups = await loadGroupsFor(current.import.company);
    $("cl-group-list").innerHTML = groups.map((g) => `<option value="${esc(g)}">`).join("");
  }

  $("create-ledger-form").addEventListener("submit", (e) => {
    e.preventDefault();
    withButton($("cl-save"), async () => {
      const name = $("cl-name").value.trim();
      const parent = $("cl-parent").value.trim();
      const existing = canonicalLedger(name);
      if (!existing) {
        const r = await api("POST", "/api/ledgers", {
          company: current.import.company, name, parent, opening_balance: $("cl-opening").value,
        });
        current.ledgers.push(r.ledger.name);
        current.ledgers.sort((a, b) => a.localeCompare(b));
        current.ledgerInfo.set(r.ledger.name, r.ledger);
        renderLedgerList();
        if (companySel.value === current.import.company) companyLedgers.push(r.ledger);
        try { localStorage.setItem("febitally.newLedgerGroup", parent); } catch (_) { /* storage blocked */ }
        toast(r.message, "success");
      }
      const finalName = existing || name;
      const targets = $("cl-apply-all").checked && !$("cl-apply-all-wrap").classList.contains("hidden")
        ? rowsWanting(createFor.name) : [];
      const ids = new Set([createFor.entryId, ...targets.map((t) => t.id)]);
      await Promise.all([...ids].map((id) => patchEntry(id, { ledger: finalName })));
      closeModal("create-ledger-modal");
      const back = createFor.entryId;
      createFor = null;
      renderReview();
      const box = document.querySelector(`#entries [data-ledger="${back}"]`);
      if (box) box.focus();
    });
  });

  /* ------------------------------------------------------------ inline narration editing */

  // The narration is sent to Tally as the voucher narration. Pushed rows are read-only (the voucher
  // already exists in Tally); every other row can be edited in place.
  function narrationCell(e) {
    if (e.status === "pushed") return `<span class="narr-static">${esc(e.narration)}</span>`;
    const text = e.narration ? esc(e.narration) : `<span class="muted">Add narration…</span>`;
    return `<div class="narr-text" data-narr="${e.id}" tabindex="0" role="button"
              title="Click to edit the narration sent to Tally">${text}<span class="narr-pencil" aria-hidden="true">✎</span></div>`;
  }

  function startNarrationEdit(id, value) {
    const cell = document.querySelector(`#entries [data-narr="${id}"]`);
    const entry = entryById(id);
    if (!cell || !entry) return null;
    const wrap = document.createElement("div");
    wrap.className = "narr-editor";
    wrap.innerHTML = `
      <textarea class="inline-input" data-narr-edit="${id}" rows="2" maxlength="1000"
                aria-label="Narration"></textarea>
      <div class="narr-actions">
        <button type="button" class="btn btn-sm btn-primary" data-narr-save="${id}">Save</button>
        <button type="button" class="btn btn-sm" data-narr-cancel="${id}">Cancel</button>
        <span class="muted small">Enter to save · Esc to cancel · Shift+Enter new line</span>
      </div>`;
    cell.replaceWith(wrap);
    const box = wrap.querySelector("textarea");
    box.value = value !== undefined ? value : entry.narration || "";
    autoSize(box);
    box.focus();
    if (value === undefined) box.select();
    return box;
  }

  function autoSize(box) {
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight + 2, 160) + "px";
  }

  let savingNarration = false;
  async function saveNarration(box) {
    if (savingNarration) return;
    const id = Number(box.dataset.narrEdit);
    const entry = entryById(id);
    const value = box.value.replace(/[ \t]+/g, " ").trim();
    if (!entry || value === (entry.narration || "")) return cancelNarration(box); // unchanged: just close
    savingNarration = true;
    box.disabled = true;
    try {
      await patchEntry(id, { narration: value });
      box.blur();
      renderReview();
      const row = document.querySelector(`#entries tr[data-id="${id}"]`);
      if (row) {
        row.classList.add("saved-flash");
        setTimeout(() => row.classList.remove("saved-flash"), 1000);
      }
    } catch (err) {
      box.disabled = false;
      box.focus();
      toast(err.message, "error");
    } finally {
      savingNarration = false;
    }
  }

  function cancelNarration(box) {
    box.dataset.cancelled = "1";
    box.blur();
    renderReview();
  }

  $("entries").addEventListener("click", (e) => {
    const t = e.target.closest("[data-narr], [data-narr-save], [data-narr-cancel]");
    if (!t) return;
    if (t.dataset.narr) startNarrationEdit(t.dataset.narr);
    const box = t.closest(".narr-editor") && t.closest(".narr-editor").querySelector("textarea");
    if (t.dataset.narrSave && box) saveNarration(box);
    if (t.dataset.narrCancel && box) cancelNarration(box);
  });
  // keep the textarea focused while pressing Save / Cancel (so blur doesn't save first)
  $("entries").addEventListener("mousedown", (e) => {
    if (e.target.closest("[data-narr-save], [data-narr-cancel]")) e.preventDefault();
  });
  $("entries").addEventListener("keydown", (e) => {
    const t = e.target;
    if (t.dataset && t.dataset.narr && (e.key === "Enter" || e.key === "F2")) {
      e.preventDefault();
      startNarrationEdit(t.dataset.narr);
    } else if (t.dataset && t.dataset.narrEdit) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); saveNarration(t); }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); cancelNarration(t); }
    }
  });
  $("entries").addEventListener("input", (e) => { if (e.target.dataset.narrEdit) autoSize(e.target); });
  // clicking elsewhere saves, like a spreadsheet cell
  $("entries").addEventListener("focusout", (e) => {
    const box = e.target;
    if (!box.dataset || !box.dataset.narrEdit || box.dataset.cancelled || box.disabled) return;
    setTimeout(() => { if (document.contains(box) && document.activeElement !== box) saveNarration(box); }, 0);
  });

  function renderReview() {
    const imp = current.import;
    $("review-title").innerHTML = `#${imp.id} · ${esc(imp.filename)} ${badge(imp.status)}`;
    $("review-sub").textContent = `${imp.company} · Bank: ${imp.bank_ledger} · uploaded ${imp.created_at}`;

    const rows = visibleEntries();
    // Re-rendering replaces the inputs; remember the ledger box being typed in so focus survives.
    const active = document.activeElement;
    const keep = active && active.dataset && active.dataset.ledger
      ? { id: active.dataset.ledger, value: active.value, start: active.selectionStart, end: active.selectionEnd }
      : null;
    // ...and a narration being edited, so a redraw (e.g. another row saved) doesn't lose the typing
    const keepNarr = active && active.dataset && active.dataset.narrEdit
      ? { id: active.dataset.narrEdit, value: active.value, start: active.selectionStart, end: active.selectionEnd }
      : null;
    $("entries").innerHTML = rows.length ? rows.map((e) => `
      <tr class="row-${e.status}" data-id="${e.id}">
        <td><input type="checkbox" data-check="${e.id}" ${selected.has(e.id) ? "checked" : ""} ${locked(e) ? "disabled" : ""}></td>
        <td style="white-space:nowrap">${fmtDate(e.txn_date)}</td>
        <td class="narration">${narrationCell(e)}${e.error ? `<div class="row-error">${esc(e.error)}</div>` : ""}</td>
        <td class="small">${esc(e.ref_no)}</td>
        <td class="num">${money(e.debit)}</td>
        <td class="num">${money(e.credit)}</td>
        <td><input class="inline-input" list="ledger-list" data-ledger="${e.id}" value="${esc(e.ledger)}"
              placeholder="Select ledger…" ${locked(e) ? "disabled" : ""}>${createButton(e)}</td>
        <td><select class="inline-input" data-vtype="${e.id}" ${locked(e) ? "disabled" : ""}>
              ${["Payment", "Receipt", "Contra"].map((t) => `<option ${t === e.voucher_type ? "selected" : ""}>${t}</option>`).join("")}
            </select></td>
        <td>${badge(e.status)}</td>
        <td class="num">
          ${e.tally_response ? `<button class="btn btn-sm" data-response="${e.id}" title="Tally's raw response to this voucher">Response</button>` : ""}
          ${e.status === "pushed" ? "" : `
            ${e.status === "skipped"
              ? `<button class="btn btn-sm" data-restore="${e.id}">Restore</button>`
              : `<button class="btn btn-sm" data-edit="${e.id}">Edit</button>
                 <button class="btn btn-sm" data-skip="${e.id}">Skip</button>`}
            <button class="btn btn-sm btn-danger" data-del="${e.id}" title="Delete">&times;</button>`}
        </td>
      </tr>`).join("")
      : `<tr><td colspan="10" class="empty">No entries${filter !== "all" ? " with status " + filter : ""}.</td></tr>`;
    if (keepNarr) {
      const box = startNarrationEdit(keepNarr.id, keepNarr.value);
      if (box) box.setSelectionRange(keepNarr.start, keepNarr.end);
    }
    if (keep) {
      const el = document.querySelector(`#entries [data-ledger="${keep.id}"]`);
      if (el && !el.disabled) {
        el.value = keep.value;
        el.focus();
        try { el.setSelectionRange(keep.start, keep.end); } catch (_) { /* not a text selection */ }
      }
    }

    const all = current.entries;
    const count = (s) => all.filter((e) => e.status === s).length;

    // status tabs show how many rows each would list with the other filters applied
    const fv = filterValues();
    const pool = all.filter((e) => matches(e, fv, true));
    document.querySelectorAll("#filters button").forEach((b) => {
      const n = b.dataset.filter === "all" ? pool.length : pool.filter((e) => e.status === b.dataset.filter).length;
      b.innerHTML = `${b.dataset.filter[0].toUpperCase()}${b.dataset.filter.slice(1)}<span class="n">${n}</span>`;
    });

    const chips = activeFilters();
    const filtered = chips.length > 0 || filter !== "all";
    $("filter-count").textContent = chips.length;
    $("filter-count").classList.toggle("hidden", !chips.length);
    $("filter-chips").innerHTML = chips.map((c, i) =>
      `<span class="chip">${esc(c.label)}<button type="button" data-remove-filter="${i}" aria-label="Remove filter ${esc(c.label)}">&times;</button></span>`).join("");
    $("filter-bar").classList.toggle("hidden", !filtered);
    if (filtered) {
      const vsum = (k) => rows.filter((e) => e.status !== "skipped").reduce((t, e) => t + Number(e[k] || 0), 0);
      $("filter-summary").innerHTML = `Showing <b>${rows.length}</b> of ${all.length} · withdrawals <b>${money(vsum("debit")) || "0.00"}</b>` +
        ` · deposits <b>${money(vsum("credit")) || "0.00"}</b>`;
    }
    const sum = (k) => all.filter((e) => e.status !== "skipped").reduce((t, e) => t + Number(e[k] || 0), 0);
    $("totals").innerHTML = `
      <div><span>Entries</span>${all.length}</div>
      <div><span>Pending</span>${count("pending")}</div>
      <div><span>Validated</span>${count("validated")}</div>
      <div><span>Pushed</span>${count("pushed")}</div>
      <div><span>Failed</span>${count("failed")}</div>
      <div><span>Skipped</span>${count("skipped")}</div>
      <div><span>Withdrawals</span>${money(sum("debit")) || "0.00"}</div>
      <div><span>Deposits</span>${money(sum("credit")) || "0.00"}</div>`;

    const selectable = rows.filter((e) => !locked(e));
    $("check-all").checked = selectable.length > 0 && selectable.every((e) => selected.has(e.id));
    const pendingShown = rows.filter((e) => e.status === "pending").length;
    $("validate-btn").textContent = selected.size ? `Validate selected (${selected.size})`
      : filtered ? `Validate ${pendingShown} pending shown` : "Validate all pending";
    const pushable = count("validated") + count("failed");
    $("push-btn").textContent = `Push to Tally (${pushable})`;
    $("push-btn").disabled = !pushable;
  }

  const entryById = (id) => current.entries.find((e) => e.id === Number(id));

  async function patchEntry(id, data) {
    const r = await api("PATCH", `/api/imports/${current.import.id}/entries/${id}`, data);
    Object.assign(entryById(id), r.entry);
  }

  $("filters").addEventListener("click", (e) => {
    const tab = e.target.closest("button[data-filter]"); // the click may land on the count inside
    if (!tab) return;
    filter = tab.dataset.filter;
    document.querySelectorAll("#filters button").forEach((b) => b.classList.toggle("active", b === tab));
    onFiltersChanged();
  });
  $("entry-search").addEventListener("input", onFiltersChanged);

  $("check-all").addEventListener("change", (e) => {
    visibleEntries().filter((x) => !locked(x)).forEach((x) => (e.target.checked ? selected.add(x.id) : selected.delete(x.id)));
    renderReview();
  });

  $("entries").addEventListener("change", async (e) => {
    const t = e.target;
    try {
      if (t.dataset.check) {
        const id = Number(t.dataset.check);
        t.checked ? selected.add(id) : selected.delete(id);
        renderReview();
      } else if (t.dataset.ledger) {
        // exact Tally spelling if it only differs in case; unknown names get a "Create" button
        const val = canonicalLedger(t.value) || t.value.trim();
        await patchEntry(t.dataset.ledger, { ledger: val });
        renderReview();
      } else if (t.dataset.vtype) {
        await patchEntry(t.dataset.vtype, { voucher_type: t.value });
        renderReview();
      }
    } catch (err) {
      toast(err.message, "error");
    }
  });

  // Switch the Voucher dropdown as soon as the box names a known ledger (typed or picked from the
  // list): Contra for cash/bank ledgers, Payment/Receipt otherwise. Saved when the box is left.
  $("entries").addEventListener("input", (e) => {
    const box = e.target;
    if (!box.dataset.ledger || !canonicalLedger(box.value)) return;
    const sel = document.querySelector(`#entries [data-vtype="${box.dataset.ledger}"]`);
    const type = voucherTypeFor(entryById(box.dataset.ledger), box.value);
    if (sel && sel.value !== type) {
      sel.value = type;
      sel.classList.add("flash");
      setTimeout(() => sel.classList.remove("flash"), 900);
    }
  });

  // Tab / Shift+Tab in a ledger box jumps to the next / previous row's ledger box (skipping the
  // voucher dropdown and row buttons). At the first/last row Tab behaves normally.
  $("entries").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.dataset.ledger) {
      // wait a tick: Enter may be choosing a suggestion from the list, which fills the box
      const box = e.target;
      setTimeout(() => {
        if (box.value.trim() && !canonicalLedger(box.value)) openCreateLedger(box.dataset.ledger, box.value);
      }, 0);
      return;
    }
    if (e.key !== "Tab" || !e.target.dataset.ledger) return;
    const boxes = [...document.querySelectorAll("#entries input[data-ledger]:not([disabled])")];
    const next = boxes[boxes.indexOf(e.target) + (e.shiftKey ? -1 : 1)];
    if (!next) return;
    e.preventDefault();
    next.focus();
    next.select();
  });

  let editingId = null;
  $("entries").addEventListener("click", async (e) => {
    const d = e.target.dataset;
    try {
      if (d.create) {
        openCreateLedger(d.create, missingLedger(entryById(d.create)));
      } else if (d.response) {
        const en = entryById(d.response);
        showResponse(`Tally response · ${fmtDate(en.txn_date)} ${en.voucher_type}`, en.tally_response,
          `${responseFormat(en.tally_response)} · ${en.status}${en.error ? " · " + en.error : ""}`);
      } else if (d.edit) {
        const en = entryById(d.edit);
        editingId = en.id;
        $("e-date").value = en.txn_date;
        $("e-narration").value = en.narration;
        $("e-ref").value = en.ref_no;
        $("e-debit").value = en.debit || "";
        $("e-credit").value = en.credit || "";
        openModal("entry-modal");
      } else if (d.skip) {
        await patchEntry(d.skip, { status: "skipped" });
        selected.delete(Number(d.skip));
        renderReview();
      } else if (d.restore) {
        await patchEntry(d.restore, { status: "pending" });
        renderReview();
      } else if (d.del) {
        const en = entryById(d.del);
        if (!(await confirmDialog(`Delete the ${fmtDate(en.txn_date)} entry “${en.narration}” (${money(en.debit || en.credit)}) from this import?`, {
          title: "Delete entry", detail: "The statement line is removed from FebiTally only.",
          confirmText: "Delete entry", danger: true,
        }))) return;
        await api("DELETE", `/api/imports/${current.import.id}/entries/${d.del}`);
        selected.delete(Number(d.del));
        await reloadImport();
      }
    } catch (err) {
      toast(err.message, "error");
    }
  });

  $("entry-form").addEventListener("submit", (e) => {
    e.preventDefault();
    withButton($("entry-save"), async () => {
      const debit = Number($("e-debit").value || 0);
      const credit = Number($("e-credit").value || 0);
      await patchEntry(editingId, {
        txn_date: $("e-date").value,
        narration: $("e-narration").value.trim(),
        ref_no: $("e-ref").value.trim(),
        debit, credit,
        voucher_type: debit > 0 ? "Payment" : "Receipt",
      });
      closeModal("entry-modal");
      renderReview();
    });
  });

  $("fill-btn").addEventListener("click", () => {
    if (!selected.size) return toast("Select rows first.", "error");
    $("fill-ledger").value = "";
    openModal("fill-modal");
    $("fill-ledger").focus();
  });
  $("fill-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const ledger = $("fill-ledger").value.trim();
    if (!current.ledgers.includes(ledger)) return toast(`"${ledger}" is not a ledger in Tally.`, "error");
    closeModal("fill-modal");
    try {
      await Promise.all([...selected].map((id) => patchEntry(id, { ledger })));
      toast(`Ledger set on ${selected.size} rows.`, "success");
    } catch (err) {
      toast(err.message, "error");
    }
    renderReview();
  });

  $("validate-btn").addEventListener("click", () => withButton($("validate-btn"), async () => {
    const ids = selected.size
      ? [...selected]
      : visibleEntries().filter((e) => e.status === "pending").map((e) => e.id); // respects filters
    if (!ids.length) throw new Error("Nothing to validate.");
    const ledgers = {}, voucher_types = {};
    ids.forEach((id) => {
      const inp = document.querySelector(`[data-ledger="${id}"]`);
      const sel = document.querySelector(`[data-vtype="${id}"]`);
      if (inp) ledgers[id] = inp.value.trim();
      if (sel) voucher_types[id] = sel.value;
    });
    const r = await api("POST", `/api/imports/${current.import.id}/validate`, { entry_ids: ids, ledgers, voucher_types });
    toast(r.message, r.invalid ? "error" : "success");
    selected.clear();
    await reloadImport();
  }));

  $("push-btn").addEventListener("click", async () => {
    const n = current.entries.filter((e) => e.status === "validated" || e.status === "failed").length;
    const counts = ["Payment", "Receipt", "Contra"]
      .map((t) => [t, current.entries.filter((e) => (e.status === "validated" || e.status === "failed") && e.voucher_type === t).length])
      .filter(([, c]) => c).map(([t, c]) => `${c} ${t}`).join(", ");
    if (!(await confirmDialog(`Create ${n} voucher${n === 1 ? "" : "s"} in Tally Prime for ${current.import.company}?`, {
      title: "Push to Tally", detail: `${counts} · bank ledger ${current.import.bank_ledger}`,
      confirmText: `Push ${n} voucher${n === 1 ? "" : "s"}`,
    }))) return;
    withButton($("push-btn"), async () => {
      try {
        const r = await api("POST", `/api/imports/${current.import.id}/push`, {});
        toast(r.message, r.failed ? "error" : "success");
      } finally {
        await reloadImport();
      }
    });
  });

  $("new-import-btn").addEventListener("click", () => {
    current = null;
    history.replaceState(null, "", "/import");
    $("review").classList.add("hidden");
    $("setup").classList.remove("hidden");
    $("new-import-btn").classList.add("hidden");
    updateSteps();
    loadImports().catch((e) => toast(e.message, "error"));
  });

  /* ------------------------------------------------------------ init */

  const openId = new URLSearchParams(location.search).get("id");
  if (openId) {
    openImport(Number(openId)).catch((e) => toast(e.message, "error"));
  }
  loadImports().catch((e) => toast(e.message, "error"));
  if (companySel.value) onCompanyChange().catch((e) => toast(e.message, "error"));
})();
