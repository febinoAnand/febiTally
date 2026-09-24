(async function () {
  const $ = (id) => document.getElementById(id);
  const FORMAT_HELP = {
    json: "Tally Prime 7.0 and later (native JSON API). Saved immediately; used by the Ledgers and Import pages.",
    xml: "All Tally Prime and Tally.ERP 9 releases. Saved immediately; used by the Ledgers and Import pages.",
  };
  let format = "json";

  function setFormat(fmt) {
    format = fmt === "xml" ? "xml" : "json";
    document.querySelectorAll("#format-choice button").forEach((b) => {
      const on = b.dataset.format === format;
      b.classList.toggle("active", on);
      b.setAttribute("aria-checked", on);
    });
    $("format-help").textContent = FORMAT_HELP[format];
    $("raw-json").classList.toggle("hidden", format !== "json");
    $("raw-xml-wrap").classList.toggle("hidden", format !== "xml");
    $("console-sub").textContent = `Send a raw ${format.toUpperCase()} request to Tally for troubleshooting`;
  }
  $("format-choice").addEventListener("click", async (e) => {
    const fmt = e.target.dataset.format;
    if (!fmt || fmt === format) return;
    const previous = format;
    setFormat(fmt);
    try {
      // saved right away: the Ledgers and Import pages use it for their next Tally request
      toast((await api("POST", "/api/settings/format", { format: fmt })).message, "success");
      refreshTallyStatus();
    } catch (err) {
      setFormat(previous);
      toast(err.message, "error");
    }
  });

  try {
    const s = await api("GET", "/api/settings");
    $("host").value = s.host;
    $("port").value = s.port;
    setFormat(s.format);
  } catch (e) {
    setFormat("json");
    toast(e.message, "error");
  }

  const form = () => ({ host: $("host").value.trim(), port: $("port").value, format });

  $("settings-form").addEventListener("submit", (e) => {
    e.preventDefault();
    withButton($("save-btn"), async () => {
      const r = await api("POST", "/api/settings", form());
      toast(r.message, "success");
      refreshTallyStatus();
    });
  });

  $("test-btn").addEventListener("click", () => withButton($("test-btn"), async () => {
    const r = await api("POST", "/api/tally/test", form());
    const box = $("test-result");
    box.classList.remove("hidden");
    if (!r.ok) {
      box.innerHTML = `<div class="alert alert-error"><b>Connection failed.</b> ${esc(r.error)}</div>`;
      return;
    }
    let html = `<div class="alert alert-success"><b>Connected.</b> ${esc(r.message)}</div>`;
    if (r.company_error) {
      html += `<div class="alert alert-warning">Could not list companies using ${r.format.toUpperCase()}: ${esc(r.company_error)}
        ${r.format === "json" ? "<br>Try the XML format if your Tally Prime is older than 7.0." : ""}</div>`;
    } else {
      html += `<div><b>Open companies (${r.companies.length})</b> <span class="muted small">via ${r.format.toUpperCase()}</span>` +
        (r.companies.length
          ? `<ul class="company-list">${r.companies.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>`
          : `<div class="muted">No company is open in Tally.</div>`) + `</div>`;
    }
    box.innerHTML = html;
  }));

  // API console, pre-filled with the documented "Cash ledger" request in both formats.
  $("raw-headers").value = JSON.stringify({
    tallyrequest: "Export", type: "Object", subtype: "Ledger", id: "Cash",
  }, null, 2);
  $("raw-payload").value = JSON.stringify({
    static_variables: [
      { name: "svExportFormat", value: "jsonEx" },
      { name: "svCurrentCompany", value: "" },
    ],
    fetch_list: ["Opening Balance", "Closing Balance"],
  }, null, 2);
  $("raw-xml").value = `<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Object</TYPE>
    <SUBTYPE>Ledger</SUBTYPE>
    <ID TYPE="Name">Cash</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
        <SVCURRENTCOMPANY></SVCURRENTCOMPANY>
      </STATICVARIABLES>
      <FETCHLIST>
        <FETCH>OpeningBalance</FETCH>
        <FETCH>ClosingBalance</FETCH>
      </FETCHLIST>
    </DESC>
  </BODY>
</ENVELOPE>`;

  $("raw-btn").addEventListener("click", () => withButton($("raw-btn"), async () => {
    let req;
    if (format === "xml") {
      req = { format, xml: $("raw-xml").value };
    } else {
      try {
        req = { format, headers: JSON.parse($("raw-headers").value), payload: JSON.parse($("raw-payload").value) };
      } catch (e) {
        throw new Error("Headers and payload must be valid JSON.");
      }
    }
    const out = $("raw-out");
    out.classList.remove("hidden");
    try {
      out.textContent = (await api("POST", "/api/tally/raw", req)).raw || "(empty response)";
    } catch (e) {
      out.textContent = "Error: " + e.message;
    }
  }));
})();
