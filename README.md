# FebiTally

Flask dashboard that imports bank statements (PDF / Excel / CSV) into Tally Prime through Tally's HTTP API, in either JSON or XML format.

The workflow is: fetch ledgers from Tally → upload a statement → assign a ledger to each line → validate → push the lines to Tally as Payment/Receipt vouchers.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py          # http://127.0.0.1:5000
```

In Tally Prime, enable the HTTP server: **F1 → Settings → Connectivity → Client/Server configuration**, set it to *Both* or *Server*, and choose a port (default 9000). Enter that host and port on the **Settings** page and click **Test connection**.

On the same page, set the **Response format**:
- **JSON** works with Tally Prime 7.0 and later (the native JSON API).
- **XML** works with every Tally Prime and Tally.ERP 9 release. Switch to XML if JSON requests fail, or if the JSON responses change after a Tally upgrade.

Clicking JSON or XML saves the format immediately. The Ledgers and Import pages use it for their next request to Tally. Both pages also:
- show the active format as a chip in the page header,
- have a **Last Tally response** button that shows Tally's raw reply (XML or JSON) to the most recent request.

On the Import page, each pushed or failed entry also has a **Response** button that shows Tally's raw reply to that voucher.

Test connection uses the format currently selected on the form.

## Pages

| Page | What it does |
|---|---|
| Dashboard | Tally connection status, entry counts by status, recent imports, ledgers cached per company |
| Import Statement | 1. Pick the company (it must be open in Tally) and the bank ledger. 2. Upload the file. 3. Review the entries: pick a ledger per row, validate, then push to Tally. You can edit, skip or delete rows. Failed pushes show Tally's error and can be retried. |
| Ledgers | Fetch all ledgers of a company from Tally. Create, edit and delete ledgers; every change is written to Tally first. |
| Settings | Tally host, port and response format (JSON/XML), a connection test, and an API console for sending raw JSON or XML requests to Tally |

An import is blocked until the company's ledgers have been fetched. Ledgers are suggested automatically in two ways:
- when a ledger name appears in the narration,
- from keywords learned from earlier validated entries.

A statement line that matches an entry already imported for the same bank is marked *skipped* as a possible duplicate. You can restore it if it is not a duplicate.

## Layout

- `services/tally_client.py`: all Tally requests. The request builders and the response parsers are separate functions so the request shapes can be adjusted in one place.
- `services/tally_xml.py`: XML mode. It converts the same requests into Tally XML envelopes, and converts XML responses back into the structure the parsers expect. Collection exports (companies, ledgers, groups) are sent as inline TDL collections, which every release supports.
- `services/statement_parser.py`:
  - detects columns (Date, Narration, Withdrawal/Deposit or Amount + Dr/Cr, Balance, Ref),
  - reads PDF tables, falling back to text lines for PDFs without tables,
  - shows a manual column-mapping dialog when auto-detection fails.
- `services/ledger_matcher.py`: ledger suggestions and learned keyword rules.
- `routes/`: page routes and the JSON API (`/api/settings`, `/api/tally/*`, `/api/ledgers`, `/api/imports`).
- `templates/`, `static/`: plain HTML, CSS and vanilla JS.
- `data/febitally.db`: the SQLite database, created on first run.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python tests/fixtures.py   # writes sample statements to tests/samples/
```

The API tests run against an in-memory fake Tally (`tests/conftest.py`). Each test runs twice, once in JSON mode and once in XML mode.

## Verifying against a real Tally Prime

Tally Prime's JSON API is new (7.x), and the exact response field names can differ between builds. The parsers accept the common variations: `NAME`, `name`, `metadata.name`, and values wrapped in lists or `{value: …}`.

The first time you connect to a real Tally:

1. **Settings → API console.** It is pre-filled with the documented Cash ledger request, in JSON and in XML. Set `svCurrentCompany` and send it to confirm the response format.
2. **Ledgers → Fetch** and check that the count matches Tally.
3. **Import a small statement** and push it. Check Day Book for correct Dr/Cr. If Tally rejects something, the row shows Tally's error text. The request shapes are in `build_ledger_import`, `build_voucher` and the `COLLECTION_*` constants.
