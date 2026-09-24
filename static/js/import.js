(async function () {
  const $ = (id) => document.getElementById(id);
  const BANK_GROUPS = ["bank accounts", "bank od a/c", "bank occ a/c"];
  const FIELDS = [
    ["txn_date", "Date *"], ["narration", "Narration *"], ["ref_no", "Ref / Chq no."],
    ["debit", "Withdrawal"], ["credit", "Deposit"], ["balance", "Balance"],
    ["amount", "Amount (single column)"], ["drcr", "Dr/Cr indicator"],
  ];

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

  async function upload(mapping) {
    const fd = new FormData();
    fd.append("company", companySel.value);
    fd.append("bank_ledger", bankSel.value);
    fd.append("file", file);
    if (mapping) fd.append("mapping", JSON.stringify(mapping));
    try {
      const r = await api("POST", "/api/imports", fd);
      toast(r.message, "success");
      closeModal("mapping-modal");
      setFile(null);
      $("file").value = "";
      await openImport(r.import_id);
    } catch (e) {
      if (e.data.need_mapping && e.data.preview && e.data.preview.length) return showMapping(e.message, e.data);
      if (e.data.need_fetch) await onCompanyChange();
      throw e;
    }
  }

  $("upload-btn").addEventListener("click", () => withButton($("upload-btn"), () => upload(null)));

  function showMapping(message, data) {
    $("mapping-msg").textContent = message;
    const cols = Math.max(...data.preview.map((r) => r.length));
    const colName = (i) => `Col ${i + 1}${data.headers[i] ? ": " + String(data.headers[i]).slice(0, 24) : ""}`;
    $("mapping-fields").innerHTML = FIELDS.map(([key, label]) => `
      <div class="field" style="min-width:160px">
        <label class="small">${label}</label>
        <select data-map="${key}"><option value="">—</option>
          ${Array.from({ length: cols }, (_, i) => `<option value="${i}">${esc(colName(i))}</option>`).join("")}
        </select>
      </div>`).join("");
    $("mapping-preview").innerHTML =
      `<thead><tr>${Array.from({ length: cols }, (_, i) => `<th>Col ${i + 1}</th>`).join("")}</tr></thead><tbody>` +
      data.preview.map((r) => `<tr>${Array.from({ length: cols }, (_, i) => `<td>${esc(r[i] ?? "")}</td>`).join("")}</tr>`).join("") +
      "</tbody>";
    openModal("mapping-modal");
  }

  $("mapping-apply").addEventListener("click", () => withButton($("mapping-apply"), async () => {
    const mapping = {};
    document.querySelectorAll("[data-map]").forEach((s) => { if (s.value !== "") mapping[s.dataset.map] = Number(s.value); });
    if (mapping.txn_date === undefined || mapping.narration === undefined) throw new Error("Map at least Date and Narration.");
    if (mapping.debit === undefined && mapping.credit === undefined && mapping.amount === undefined) {
      throw new Error("Map Withdrawal/Deposit or Amount.");
    }
    await upload(mapping);
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

  function renderReview() {
    const imp = current.import;
    $("review-title").innerHTML = `#${imp.id} · ${esc(imp.filename)} ${badge(imp.status)}`;
    $("review-sub").textContent = `${imp.company} · Bank: ${imp.bank_ledger} · uploaded ${imp.created_at}`;

    const rows = visibleEntries();
    $("entries").innerHTML = rows.length ? rows.map((e) => `
      <tr class="row-${e.status}" data-id="${e.id}">
        <td><input type="checkbox" data-check="${e.id}" ${selected.has(e.id) ? "checked" : ""} ${locked(e) ? "disabled" : ""}></td>
        <td style="white-space:nowrap">${fmtDate(e.txn_date)}</td>
        <td class="narration">${esc(e.narration)}${e.error ? `<div class="row-error">${esc(e.error)}</div>` : ""}</td>
        <td class="small">${esc(e.ref_no)}</td>
        <td class="num">${money(e.debit)}</td>
        <td class="num">${money(e.credit)}</td>
        <td><input class="inline-input" list="ledger-list" data-ledger="${e.id}" value="${esc(e.ledger)}"
              placeholder="Select ledger…" ${locked(e) ? "disabled" : ""}></td>
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
        const val = t.value.trim();
        if (val && !current.ledgers.includes(val)) toast(`"${val}" is not a ledger in Tally. Create it on the Ledgers page or pick one from the list.`, "error");
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

  let editingId = null;
  $("entries").addEventListener("click", async (e) => {
    const d = e.target.dataset;
    try {
      if (d.response) {
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
