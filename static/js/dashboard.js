(async function () {
  const $ = (id) => document.getElementById(id);

  tallyStatus.then((s) => {
    $("st-tally").textContent = s.ok ? "Connected" : "Offline";
    $("st-tally").style.color = s.ok ? "var(--success)" : "var(--danger)";
    $("st-tally-hint").innerHTML = s.ok
      ? esc(`${s.host}:${s.port}`)
      : `${esc(s.error || "")} · <a href="/settings">Settings</a>`;
  });

  try {
    const d = await api("GET", "/api/dashboard");
    $("st-imports").textContent = d.imports_count;
    $("st-pending").textContent = d.entries.pending;
    $("st-validated").textContent = d.entries.validated;
    $("st-pushed").textContent = d.entries.pushed;
    $("st-failed-hint").textContent = d.entries.failed ? `${d.entries.failed} failed — retry from the import` : "vouchers created";
    if (d.entries.failed) $("st-failed-hint").style.color = "var(--danger)";

    $("recent").innerHTML = d.recent_imports.length ? d.recent_imports.map((i) => `
      <tr>
        <td><a href="/import?id=${i.id}">#${i.id}</a></td>
        <td>${esc(i.filename)}</td>
        <td>${esc(i.company)}<div class="muted small">${esc(i.bank_ledger)}</div></td>
        <td>${fmtDate(i.created_at)}</td>
        <td class="num">${i.total}</td>
        <td class="num">${money(i.total_debit)}</td>
        <td class="num">${money(i.total_credit)}</td>
        <td>${badge(i.status)}</td>
      </tr>`).join("")
      : `<tr><td colspan="8" class="empty">No imports yet. <a href="/import">Import a bank statement</a>.</td></tr>`;

    $("ledger-counts").innerHTML = d.ledgers.length ? d.ledgers.map((l) => `
      <tr><td>${esc(l.company)}</td><td class="num">${l.n}</td><td class="small muted">${esc(l.fetched_at || "")}</td></tr>`).join("")
      : `<tr><td colspan="3" class="empty">No ledgers fetched yet. <a href="/ledgers">Fetch from Tally</a>.</td></tr>`;
  } catch (e) {
    toast(e.message, "error");
  }
})();
