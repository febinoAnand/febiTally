(async function () {
  const $ = (id) => document.getElementById(id);
  const companySel = $("company");
  let ledgers = [];
  let sort = { key: "name", dir: 1 };
  let editing = null; // ledger being edited, or null for create

  const { error } = await loadCompanies(companySel);
  if (error) {
    $("company-alert").textContent = `Tally is not reachable (${error}). Showing companies with cached ledgers; Fetch and edits need Tally.`;
    $("company-alert").classList.remove("hidden");
  }

  async function loadLedgers() {
    const company = companySel.value;
    if (!company) {
      ledgers = [];
      render();
      return;
    }
    const r = await api("GET", `/api/ledgers?company=${encodeURIComponent(company)}`);
    ledgers = r.ledgers;
    const fetched = ledgers.reduce((m, l) => (l.fetched_at > m ? l.fetched_at : m), "");
    $("fetch-info").textContent = ledgers.length ? `Last fetched ${fetched}` : "Not fetched yet — click Fetch from Tally.";
    const groups = [...new Set(ledgers.map((l) => l.parent).filter(Boolean))].sort();
    $("group-filter").innerHTML = `<option value="">All groups</option>` + groups.map((g) => `<option>${esc(g)}</option>`).join("");
    render();
  }

  function render() {
    const q = $("search").value.trim().toLowerCase();
    const group = $("group-filter").value;
    const rows = ledgers
      .filter((l) => (!q || l.name.toLowerCase().includes(q) || (l.parent || "").toLowerCase().includes(q))
        && (!group || l.parent === group))
      .sort((a, b) => {
        const x = a[sort.key], y = b[sort.key];
        return (typeof x === "number" ? x - y : String(x || "").localeCompare(String(y || ""))) * sort.dir;
      });
    $("count").textContent = ledgers.length ? `(${rows.length}${rows.length !== ledgers.length ? " of " + ledgers.length : ""})` : "";
    $("ledger-rows").innerHTML = rows.length ? rows.map((l) => `
      <tr>
        <td>${esc(l.name)}</td>
        <td>${esc(l.parent)}</td>
        <td class="num">${drcr(l.opening_balance)}</td>
        <td class="num">${drcr(l.closing_balance)}</td>
        <td class="small muted">${esc(l.fetched_at || "")}</td>
        <td class="num">
          <button class="btn btn-sm" data-edit="${l.id}">Edit</button>
          <button class="btn btn-sm btn-danger" data-delete="${l.id}">Delete</button>
        </td>
      </tr>`).join("")
      : `<tr><td colspan="6" class="empty">${companySel.value ? (ledgers.length ? "No ledgers match." : "No ledgers cached. Click <b>Fetch from Tally</b>.") : "Select a company."}</td></tr>`;
  }

  async function loadGroups() {
    const fromCache = [...new Set(ledgers.map((l) => l.parent).filter(Boolean))];
    let groups = fromCache;
    try {
      groups = (await api("GET", `/api/ledgers/groups?company=${encodeURIComponent(companySel.value)}`)).groups;
    } catch (_) { /* fall back to groups seen in the cache */ }
    $("group-list").innerHTML = [...new Set(groups.concat(fromCache))].sort().map((g) => `<option value="${esc(g)}">`).join("");
  }

  function openForm(ledger) {
    if (!companySel.value) return toast("Select a company first.", "error");
    editing = ledger;
    $("modal-title").textContent = ledger ? `Edit ${ledger.name}` : "New ledger";
    $("f-name").value = ledger ? ledger.name : "";
    $("f-parent").value = ledger ? ledger.parent : "";
    $("f-opening").value = ledger ? ledger.opening_balance : 0;
    openModal("ledger-modal");
    $("f-name").focus();
    loadGroups();
  }

  companySel.addEventListener("change", () => loadLedgers().catch((e) => toast(e.message, "error")));
  $("search").addEventListener("input", render);
  $("group-filter").addEventListener("change", render);
  document.querySelectorAll("th.sortable").forEach((th) => th.addEventListener("click", () => {
    sort = { key: th.dataset.sort, dir: sort.key === th.dataset.sort ? -sort.dir : 1 };
    render();
  }));

  $("fetch-btn").addEventListener("click", () => withButton($("fetch-btn"), async () => {
    if (!companySel.value) throw new Error("Select a company first.");
    const r = await api("POST", "/api/ledgers/fetch", { company: companySel.value });
    toast(r.message, "success");
    await loadLedgers();
  }));

  $("add-btn").addEventListener("click", () => openForm(null));

  $("ledger-rows").addEventListener("click", async (e) => {
    const editId = e.target.dataset.edit;
    const delId = e.target.dataset.delete;
    if (editId) openForm(ledgers.find((l) => String(l.id) === editId));
    if (delId) {
      const l = ledgers.find((x) => String(x.id) === delId);
      if (!(await confirmDialog(`Delete ledger "${l.name}" from Tally Prime?\nTally refuses if the ledger has vouchers.`))) return;
      withButton(e.target, async () => {
        const r = await api("DELETE", `/api/ledgers/${l.id}`);
        toast(r.message, "success");
        await loadLedgers();
      });
    }
  });

  $("ledger-form").addEventListener("submit", (e) => {
    e.preventDefault();
    withButton($("save-ledger"), async () => {
      const data = {
        company: companySel.value,
        name: $("f-name").value.trim(),
        parent: $("f-parent").value.trim(),
        opening_balance: $("f-opening").value,
      };
      const r = editing
        ? await api("PUT", `/api/ledgers/${editing.id}`, data)
        : await api("POST", "/api/ledgers", data);
      toast(r.message, "success");
      closeModal("ledger-modal");
      await loadLedgers();
    });
  });

  if (companySel.value) loadLedgers().catch((e) => toast(e.message, "error"));
})();
