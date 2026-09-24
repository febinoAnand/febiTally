(async function () {
  const $ = (id) => document.getElementById(id);
  const BANK_GROUPS = ["bank accounts", "bank od a/c", "bank occ a/c"];
  const FIELDS = [
    ["txn_date", "Date", true], ["narration", "Narration / Description", true], ["ref_no", "Ref / Chq no."],
    ["debit", "Withdrawal (Dr)"], ["credit", "Deposit (Cr)"], ["balance", "Balance"],
    ["amount", "Amount (single column)"], ["drcr", "Dr/Cr indicator"], ["ledger", "Ledger (Tally)"],
  ];
  const FIELD_LABEL = Object.fromEntries(FIELDS.map(([k, l]) => [k, l]));

  const companySel = $("company");
  const bankSel = $("bank-ledger");
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
    bankSel.disabled = true;
    bankSel.innerHTML = `<option value="">Select company first</option>`;
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
      fillBankLedgers();
    }
    updateSteps();
  }

  function fillBankLedgers() {
    const showAll = $("show-all-ledgers").checked;
    let list = companyLedgers.filter((l) => BANK_GROUPS.includes((l.parent || "").toLowerCase()));
    if (showAll || !list.length) list = companyLedgers;
    let saved = null;
    try { saved = localStorage.getItem(`febitally.bank.${companySel.value}`); } catch (_) { /* storage blocked */ }
    bankSel.innerHTML = `<option value="">Select bank ledger…</option>` +
      list.map((l) => `<option value="${esc(l.name)}" ${l.name === saved ? "selected" : ""}>${esc(l.name)}${l.parent ? " — " + esc(l.parent) : ""}</option>`).join("");
    bankSel.disabled = false;
  }

  companySel.addEventListener("change", () => onCompanyChange().catch((e) => toast(e.message, "error")));
  $("show-all-ledgers").addEventListener("change", fillBankLedgers);
  bankSel.addEventListener("change", () => {
    try { localStorage.setItem(`febitally.bank.${companySel.value}`, bankSel.value); } catch (_) { /* storage blocked */ }
    updateSteps();
  });

  function updateSteps() {
    const s1 = companySel.value && companyLedgers.length && bankSel.value;
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
    preview = await api("POST", "/api/imports/preview", fd);
    openMappingDialog();
  }));

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
    $("map-sub").textContent = `${p.filename} · ${p.total_rows} rows in file · ${companySel.value} · ${bankSel.value}`;
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
  $("map-raw").addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-row]");
    if (!tr) return;
    $("map-header-row").value = tr.dataset.row;
    $("map-header-row").dispatchEvent(new Event("change"));
  });

  $("mapping-apply").addEventListener("click", () => withButton($("mapping-apply"), async () => {
    const fd = new FormData();
    fd.append("company", companySel.value);
    fd.append("bank_ledger", bankSel.value);
    fd.append("token", preview.token);
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
      if (!(await confirmDialog(`Delete import #${delId} and its entries from FebiTally?\nVouchers already pushed stay in Tally.`))) return;
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
    current = imp;
    current.ledgers = lr.ledgers.map((l) => l.name);
    selected.clear();
    $("ledger-list").innerHTML = current.ledgers.map((n) => `<option value="${esc(n)}">`).join("");
    $("setup").classList.add("hidden");
    $("review").classList.remove("hidden");
    $("new-import-btn").classList.remove("hidden");
    $("step-1").className = "step done";
    $("step-2").className = "step done";
    $("step-3").className = "step active";
    history.replaceState(null, "", `?id=${id}`);
    renderReview();
  }

  async function reloadImport() {
    const imp = await api("GET", `/api/imports/${current.import.id}`);
    current.import = imp.import;
    current.entries = imp.entries;
    renderReview();
  }

  const locked = (e) => e.status === "pushed" || e.status === "skipped";

  function visibleEntries() {
    const q = $("entry-search").value.trim().toLowerCase();
    return current.entries.filter((e) => (filter === "all" || e.status === filter)
      && (!q || (e.narration || "").toLowerCase().includes(q) || (e.ledger || "").toLowerCase().includes(q)
        || (e.ref_no || "").toLowerCase().includes(q)));
  }

  /* ------------------------------------------------------------ create a missing ledger */

  const STATEMENT_LEDGER_RE = /^Ledger '(.+)' from the statement is not a ledger in Tally/;
  const ledgerKey = (name) => String(name || "").trim().toLowerCase();
  const canonicalLedger = (name) => current.ledgers.find((n) => ledgerKey(n) === ledgerKey(name));

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
        $("ledger-list").innerHTML = current.ledgers.map((n) => `<option value="${esc(n)}">`).join("");
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
    $("entries").innerHTML = rows.length ? rows.map((e) => `
      <tr class="row-${e.status}" data-id="${e.id}">
        <td><input type="checkbox" data-check="${e.id}" ${selected.has(e.id) ? "checked" : ""} ${locked(e) ? "disabled" : ""}></td>
        <td style="white-space:nowrap">${fmtDate(e.txn_date)}</td>
        <td class="narration">${esc(e.narration)}${e.error ? `<div class="row-error">${esc(e.error)}</div>` : ""}</td>
        <td class="small">${esc(e.ref_no)}</td>
        <td class="num">${money(e.debit)}</td>
        <td class="num">${money(e.credit)}</td>
        <td><input class="inline-input" list="ledger-list" data-ledger="${e.id}" value="${esc(e.ledger)}"
              placeholder="Select ledger…" ${locked(e) ? "disabled" : ""}>${createButton(e)}</td>
        <td><select class="inline-input" data-vtype="${e.id}" ${locked(e) ? "disabled" : ""}>
              ${["Payment", "Receipt"].map((t) => `<option ${t === e.voucher_type ? "selected" : ""}>${t}</option>`).join("")}
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
    $("validate-btn").textContent = selected.size ? `Validate selected (${selected.size})` : "Validate all pending";
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
    if (!e.target.dataset.filter) return;
    filter = e.target.dataset.filter;
    document.querySelectorAll("#filters button").forEach((b) => b.classList.toggle("active", b === e.target));
    renderReview();
  });
  $("entry-search").addEventListener("input", renderReview);

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
        if (!(await confirmDialog("Delete this entry from the import?"))) return;
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
      : current.entries.filter((e) => e.status === "pending").map((e) => e.id);
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
    if (!(await confirmDialog(`Create ${n} voucher(s) in Tally Prime for ${current.import.company}?`))) return;
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
