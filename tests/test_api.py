"""End-to-end API flow against the in-memory FakeTally."""
import json

from tests import fixtures

COMPANY = "Bhrama Enterprises"


def _upload(client, tmp_path, company=COMPANY, bank="HDFC Bank"):
    path = fixtures.make_excel(str(tmp_path / "stmt.xlsx"))
    with open(path, "rb") as f:
        return client.post("/api/imports", data={"company": company, "bank_ledger": bank,
                                                 "file": (f, "stmt.xlsx")},
                           content_type="multipart/form-data")


def test_pages_render(client, fake_tally):
    for url in ("/", "/import", "/ledgers", "/settings"):
        assert client.get(url).status_code == 200


def test_settings_roundtrip(client, fake_tally):
    r = client.post("/api/settings", json={"host": "10.0.0.5", "port": "9001", "format": "xml"})
    assert r.status_code == 200
    assert client.get("/api/settings").get_json() == {"host": "10.0.0.5", "port": 9001, "format": "xml"}
    assert client.post("/api/settings", json={"host": "x", "port": "abc"}).status_code == 400
    assert client.post("/api/settings", json={"host": "x", "port": 1, "format": "yaml"}).status_code == 400
    for fmt in ("json", "xml"):
        r = client.post("/api/tally/test", json={"host": "localhost", "port": 9000, "format": fmt}).get_json()
        assert r["ok"] and r["format"] == fmt and r["companies"] == [COMPANY]


def test_raw_console(client, fake_tally):
    r = client.post("/api/tally/raw", json={"format": "xml", "xml": "<ENVELOPE><HEADER><TALLYREQUEST>Export"
                                            "</TALLYREQUEST></HEADER></ENVELOPE>"}).get_json()
    assert "Unknown Request" in r["raw"]
    r = client.post("/api/tally/raw", json={"format": "json",
                                            "headers": {"tallyrequest": "Export", "type": "Collection",
                                                        "id": "List of Companies"},
                                            "payload": {}}).get_json()
    assert COMPANY in r["raw"]


def test_import_requires_fetched_ledgers(client, fake_tally, tmp_path):
    r = _upload(client, tmp_path)
    assert r.status_code == 409 and r.get_json()["need_fetch"]


def test_import_rejects_company_not_open(client, fake_tally, tmp_path):
    r = _upload(client, tmp_path, company="Other Co")
    assert r.status_code == 400 and "not open in Tally" in r.get_json()["error"]


def test_ledger_crud(client, fake_tally):
    r = client.post("/api/ledgers/fetch", json={"company": COMPANY}).get_json()
    assert r["count"] == 5

    r = client.post("/api/ledgers", json={"company": COMPANY, "name": "Electricity", "parent": "Indirect Expenses",
                                          "opening_balance": 0})
    assert r.status_code == 201
    ledger = r.get_json()["ledger"]
    assert "Electricity" in fake_tally.ledgers

    dup = client.post("/api/ledgers", json={"company": COMPANY, "name": "Electricity", "parent": "Indirect Expenses"})
    assert dup.status_code == 400

    r = client.put("/api/ledgers/%d" % ledger["id"], json={"name": "Power", "parent": "Indirect Expenses",
                                                           "opening_balance": -10})
    assert r.status_code == 200 and "Power" in fake_tally.ledgers and "Electricity" not in fake_tally.ledgers

    assert client.delete("/api/ledgers/%d" % ledger["id"]).status_code == 200
    assert "Power" not in fake_tally.ledgers
    names = [l["name"] for l in client.get("/api/ledgers?company=" + COMPANY).get_json()["ledgers"]]
    assert "Power" not in names


def test_full_import_validate_push(client, fake_tally, tmp_path):
    client.post("/api/ledgers/fetch", json={"company": COMPANY})
    r = _upload(client, tmp_path)
    assert r.status_code == 201, r.get_json()
    import_id = r.get_json()["import_id"]

    entries = client.get("/api/imports/%d" % import_id).get_json()["entries"]
    assert len(entries) == 4
    by_date = {e["txn_date"]: e for e in entries}
    # ledger names found in narrations are suggested
    assert by_date["2026-04-01"]["ledger"] == "Amazon Web Services"
    assert by_date["2026-04-03"]["ledger"] == "Acme Traders"
    assert by_date["2026-04-05"]["ledger"] == "Office Rent"
    assert by_date["2026-04-03"]["voucher_type"] == "Receipt"

    # ATM withdrawal has no suggestion -> validation fails until a ledger is chosen
    ids = [e["id"] for e in entries]
    r = client.post("/api/imports/%d/validate" % import_id, json={"entry_ids": ids}).get_json()
    assert r["validated"] == 3 and r["invalid"] == 1
    atm = by_date["2026-04-07"]
    r = client.post("/api/imports/%d/validate" % import_id,
                    json={"entry_ids": [atm["id"]], "ledgers": {str(atm["id"]): "Cash"}}).get_json()
    assert r["validated"] == 1

    r = client.post("/api/imports/%d/push" % import_id, json={}).get_json()
    assert r["pushed"] == 4 and r["failed"] == 0 and r["status"] == "pushed"
    assert len(fake_tally.vouchers) == 4
    # ATM withdrawal to the Cash ledger is a cash/bank transfer: Contra
    assert sorted(v["vouchertypename"] for v in fake_tally.vouchers) == ["Contra", "Payment", "Payment", "Receipt"]

    # pushed entries are locked
    patch = client.patch("/api/imports/%d/entries/%d" % (import_id, atm["id"]), json={"ledger": "Office Rent"})
    assert patch.status_code == 400

    # re-importing the same statement marks entries as possible duplicates
    r = _upload(client, tmp_path)
    dup_entries = client.get("/api/imports/%d" % r.get_json()["import_id"]).get_json()["entries"]
    assert all(e["status"] == "skipped" for e in dup_entries)


def test_push_failure_is_recorded_and_retryable(client, fake_tally, tmp_path):
    client.post("/api/ledgers/fetch", json={"company": COMPANY})
    import_id = _upload(client, tmp_path).get_json()["import_id"]
    entries = client.get("/api/imports/%d" % import_id).get_json()["entries"]
    ids = [e["id"] for e in entries]
    client.post("/api/imports/%d/validate" % import_id,
                json={"entry_ids": ids, "ledgers": {str(e["id"]): e["ledger"] or "Cash" for e in entries}})

    fake_tally.fail_voucher_ledger = "Office Rent"
    r = client.post("/api/imports/%d/push" % import_id, json={}).get_json()
    assert r["pushed"] == 3 and r["failed"] == 1 and r["status"] == "partial"
    failed = [e for e in client.get("/api/imports/%d" % import_id).get_json()["entries"] if e["status"] == "failed"]
    assert failed[0]["error"] == "Ledger does not exist"

    fake_tally.fail_voucher_ledger = None
    r = client.post("/api/imports/%d/push" % import_id, json={}).get_json()
    assert r["pushed"] == 1 and r["status"] == "pushed"


def test_learned_rules_suggest_next_time(client, fake_tally, tmp_path):
    client.post("/api/ledgers/fetch", json={"company": COMPANY})
    import_id = _upload(client, tmp_path).get_json()["import_id"]
    atm = [e for e in client.get("/api/imports/%d" % import_id).get_json()["entries"]
           if e["txn_date"] == "2026-04-07"][0]
    assert atm["ledger"] == ""
    client.post("/api/imports/%d/validate" % import_id,
                json={"entry_ids": [atm["id"]], "ledgers": {str(atm["id"]): "Cash"}})
    client.delete("/api/imports/%d" % import_id)

    import_id = _upload(client, tmp_path).get_json()["import_id"]
    atm = [e for e in client.get("/api/imports/%d" % import_id).get_json()["entries"]
           if e["txn_date"] == "2026-04-07"][0]
    assert atm["ledger"] == "Cash"


def test_ledger_and_import_pages_follow_selected_format(client, fake_tally, tmp_path):
    """Switching the format on Settings applies to the very next ledger/import request."""
    for fmt, marker in (("xml", "<"), ("json", "{"), ("xml", "<")):
        assert client.post("/api/settings/format", json={"format": fmt}).status_code == 200
        r = client.post("/api/ledgers/fetch", json={"company": COMPANY}).get_json()
        assert r["format"] == fmt and r["count"] == 5
        assert r["tally_response"].startswith(marker)
        assert fmt.upper() in r["message"]
        sent_xml = fake_tally.requests[-1][0] == "xml"
        assert sent_xml == (fmt == "xml")

    # voucher push stores Tally's raw response per entry, in the selected format
    import_id = _upload(client, tmp_path).get_json()["import_id"]
    entries = client.get("/api/imports/%d" % import_id).get_json()["entries"]
    client.post("/api/imports/%d/validate" % import_id,
                json={"entry_ids": [e["id"] for e in entries],
                      "ledgers": {str(e["id"]): e["ledger"] or "Cash" for e in entries}})
    fake_tally.fail_voucher_ledger = "Office Rent"
    r = client.post("/api/imports/%d/push" % import_id, json={}).get_json()
    assert r["format"] == "xml" and r["tally_response"].startswith("<RESPONSE>")
    entries = client.get("/api/imports/%d" % import_id).get_json()["entries"]
    assert all(e["tally_response"].startswith("<RESPONSE>") for e in entries)
    failed = [e for e in entries if e["status"] == "failed"][0]
    assert "<LINEERROR>Ledger does not exist</LINEERROR>" in failed["tally_response"]

    assert client.post("/api/settings/format", json={"format": "csv"}).status_code == 400


def test_tally_error_carries_raw_response(client, fake_tally):
    client.post("/api/ledgers/fetch", json={"company": COMPANY})
    r = client.post("/api/ledgers", json={"company": COMPANY, "name": "Cash2", "parent": "Cash-in-Hand"})
    assert r.status_code == 201
    fake_tally.ledgers["Clash"] = {"parent": "x", "openingbalance": "0"}  # exists in Tally, not in cache
    r = client.post("/api/ledgers", json={"company": COMPANY, "name": "Clash", "parent": "Cash-in-Hand"})
    body = r.get_json()
    assert r.status_code == 502 and body["error"] == "Duplicate ledger"
    assert "Duplicate ledger" in body["tally_response"] and body["format"] in ("json", "xml")


def _preview(client, path, name):
    with open(path, "rb") as f:
        return client.post("/api/imports/preview", data={"file": (f, name)}, content_type="multipart/form-data")


def test_mapping_dialog_flow(client, fake_tally, tmp_path):
    client.post("/api/ledgers/fetch", json={"company": COMPANY})
    r = _preview(client, fixtures.make_excel(str(tmp_path / "s.xlsx")), "s.xlsx")
    assert r.status_code == 200
    p = r.get_json()
    # letterhead rows 0-2, header on row 3; detected mapping pre-filled for the dialog
    assert p["detected"] and p["mode"] == "table" and p["header_row"] == 3
    assert p["mapping"] == {"txn_date": 0, "narration": 1, "ref_no": 2, "debit": 3, "credit": 4, "balance": 5}
    assert p["count"] == 4 and len(p["sample"]) == 4 and p["raw"][3][0] == "Date"
    assert not client.get("/api/imports").get_json()["imports"]  # nothing imported yet

    # user swaps withdrawal/deposit: preview reflects it
    swapped = dict(p["mapping"], debit=4, credit=3)
    r = client.post("/api/imports/preview", json={"token": p["token"], "mapping": swapped, "header_row": 3}).get_json()
    assert r["count"] == 4 and r["sample"][0]["credit"] == 8500.0

    # clearing every column is reported, not silently auto-detected
    r = client.post("/api/imports/preview", json={"token": p["token"], "mapping": {}, "header_row": 3}).get_json()
    assert r["count"] == 0 and "Date and Narration" in r["problem"]

    # import with the confirmed mapping
    r = client.post("/api/imports", data={"company": COMPANY, "bank_ledger": "HDFC Bank", "token": p["token"],
                                          "mapping": json.dumps(p["mapping"]), "header_row": "3"})
    assert r.status_code == 201 and r.get_json()["count"] == 4
    imp = client.get("/api/imports/%d" % r.get_json()["import_id"]).get_json()
    assert imp["import"]["filename"] == "s.xlsx"
    assert [e["debit"] for e in imp["entries"]] == [8500.0, 0.0, 25000.0, 2000.0]


def test_mapping_dialog_undetected_columns(client, fake_tally, tmp_path):
    client.post("/api/ledgers/fetch", json={"company": COMPANY})
    path = tmp_path / "odd.csv"
    path.write_text("Bank of Nowhere\nWhen,What,How much\n01/04/2026,Something,100\n02/04/2026,Other,50\n")
    p = _preview(client, str(path), "odd.csv").get_json()
    assert not p["detected"] and p["count"] == 0 and "header row" in p["problem"]

    mapping = {"txn_date": 0, "narration": 1, "debit": 2}
    r = client.post("/api/imports/preview", json={"token": p["token"], "mapping": mapping, "header_row": 1}).get_json()
    assert r["count"] == 2 and r["problem"] is None

    bad = client.post("/api/imports", data={"company": COMPANY, "bank_ledger": "HDFC Bank", "token": p["token"],
                                            "mapping": json.dumps({"narration": 1})})
    assert bad.status_code == 422 and "Date and Narration" in bad.get_json()["error"]
    r = client.post("/api/imports", data={"company": COMPANY, "bank_ledger": "HDFC Bank", "token": p["token"],
                                          "mapping": json.dumps(mapping), "header_row": "1"})
    assert r.status_code == 201 and r.get_json()["count"] == 2


def test_mapping_dialog_rejects_bad_tokens(client, fake_tally):
    assert client.post("/api/imports/preview", json={"token": "../../etc/passwd"}).status_code == 400
    assert client.post("/api/imports/preview", json={"token": "0" * 32}).status_code == 410


def test_ledger_column_mapping(client, fake_tally, tmp_path):
    client.post("/api/ledgers/fetch", json={"company": COMPANY})
    p = _preview(client, fixtures.make_excel_with_ledger(str(tmp_path / "l.xlsx")), "l.xlsx").get_json()
    assert p["mapping"]["ledger"] == 5 and "ledger" in p["fields"]
    assert [r["ledger"] for r in p["sample"]] == ["Amazon Web Services", "acme traders", "Rent Account", ""]

    r = client.post("/api/imports", data={"company": COMPANY, "bank_ledger": "HDFC Bank", "token": p["token"],
                                          "mapping": json.dumps(p["mapping"]), "header_row": "0"}).get_json()
    assert r["unmatched_ledgers"] == 1 and "not found in Tally" in r["message"]
    entries = {e["txn_date"]: e for e in client.get("/api/imports/%d" % r["import_id"]).get_json()["entries"]}
    assert entries["2026-04-01"]["ledger"] == "Amazon Web Services"
    assert entries["2026-04-03"]["ledger"] == "Acme Traders"          # matched ignoring case
    assert entries["2026-04-05"]["ledger"] == "Office Rent"           # unknown -> suggestion from narration
    assert "Rent Account" in entries["2026-04-05"]["error"]
    assert entries["2026-04-07"]["ledger"] == "" and entries["2026-04-07"]["error"] is None

    # without mapping the ledger column, the statement's ledger names are ignored
    mapping = {k: v for k, v in p["mapping"].items() if k != "ledger"}
    r = client.post("/api/imports", data={"company": COMPANY, "bank_ledger": "HDFC Bank", "token": p["token"],
                                          "mapping": json.dumps(mapping), "header_row": "0"}).get_json()
    entries = {e["txn_date"]: e for e in client.get("/api/imports/%d" % r["import_id"]).get_json()["entries"]}
    assert entries["2026-04-03"]["ledger"] == "Acme Traders"  # from narration suggestion
    assert r["unmatched_ledgers"] == 0


def test_contra_for_cash_and_bank_ledgers(client, fake_tally, tmp_path):
    # a bank ledger two levels down: Bank Accounts > Current Accounts > ICICI Current
    fake_tally.groups["Current Accounts"] = "Bank Accounts"
    fake_tally.ledgers["ICICI Current"] = {"parent": "Current Accounts", "openingbalance": "0"}
    r = client.post("/api/ledgers/fetch", json={"company": COMPANY}).get_json()
    assert r["cash_bank"] == 3
    flags = {l["name"]: l["cash_bank"] for l in client.get("/api/ledgers?company=" + COMPANY).get_json()["ledgers"]}
    assert flags == {"Cash": 1, "HDFC Bank": 1, "ICICI Current": 1, "Office Rent": 0,
                     "Amazon Web Services": 0, "Acme Traders": 0}

    import_id = _upload(client, tmp_path).get_json()["import_id"]
    entries = {e["txn_date"]: e for e in client.get("/api/imports/%d" % import_id).get_json()["entries"]}
    atm, deposit = entries["2026-04-07"], entries["2026-04-03"]
    url = "/api/imports/%d/entries/%%d" % import_id

    # choosing a cash/bank ledger switches the row to Contra; choosing another switches back
    assert client.patch(url % atm["id"], json={"ledger": "Cash"}).get_json()["entry"]["voucher_type"] == "Contra"
    assert client.patch(url % atm["id"], json={"ledger": "Office Rent"}).get_json()["entry"]["voucher_type"] == "Payment"
    assert client.patch(url % deposit["id"], json={"ledger": "ICICI Current"}).get_json()["entry"]["voucher_type"] == "Contra"

    # validation: Contra needs a cash/bank ledger, and cash/bank ledgers need Contra
    v = "/api/imports/%d/validate" % import_id
    r = client.post(v, json={"entry_ids": [atm["id"]], "ledgers": {str(atm["id"]): "Office Rent"},
                             "voucher_types": {str(atm["id"]): "Contra"}}).get_json()
    assert r["invalid"] == 1
    err = [e for e in client.get("/api/imports/%d" % import_id).get_json()["entries"] if e["id"] == atm["id"]][0]["error"]
    assert "Contra is only for transfers with cash or bank ledgers" in err
    r = client.post(v, json={"entry_ids": [atm["id"]], "ledgers": {str(atm["id"]): "Cash"},
                             "voucher_types": {str(atm["id"]): "Payment"}}).get_json()
    assert r["invalid"] == 1
    r = client.post(v, json={"entry_ids": [atm["id"], deposit["id"]],
                             "ledgers": {str(atm["id"]): "Cash", str(deposit["id"]): "ICICI Current"}}).get_json()
    assert r["validated"] == 2

    r = client.post("/api/imports/%d/push" % import_id, json={"entry_ids": [atm["id"], deposit["id"]]}).get_json()
    assert r["pushed"] == 2
    lines = {v["vouchertypename"] + ":" + v["allledgerentries"][0]["ledgername"]: v["allledgerentries"]
             for v in fake_tally.vouchers}
    # cash withdrawal from HDFC: Dr Cash, Cr HDFC Bank
    w = lines["Contra:Cash"]
    assert [(l["ledgername"], l["isdeemedpositive"], l["amount"]) for l in w] == \
        [("Cash", "Yes", "-2000.00"), ("HDFC Bank", "No", "2000.00")]
    # transfer in from ICICI: Dr HDFC Bank, Cr ICICI Current
    d = lines["Contra:HDFC Bank"]
    assert [(l["ledgername"], l["isdeemedpositive"], l["amount"]) for l in d] == \
        [("HDFC Bank", "Yes", "-12000.00"), ("ICICI Current", "No", "12000.00")]


def test_mapped_cash_ledger_imports_as_contra(client, fake_tally, tmp_path):
    client.post("/api/ledgers/fetch", json={"company": COMPANY})
    import openpyxl
    path = str(tmp_path / "c.xlsx")
    wb = openpyxl.Workbook()
    wb.active.append(["Date", "Narration", "Withdrawal", "Deposit", "Ledger"])
    wb.active.append(["07/04/2026", "ATM WDL", 2000, None, "cash"])
    wb.active.append(["08/04/2026", "CASH DEPOSIT", None, 5000, "Cash"])
    wb.save(path)
    p = _preview(client, path, "c.xlsx").get_json()
    r = client.post("/api/imports", data={"company": COMPANY, "bank_ledger": "HDFC Bank", "token": p["token"],
                                          "mapping": json.dumps(p["mapping"]), "header_row": "0"}).get_json()
    entries = client.get("/api/imports/%d" % r["import_id"]).get_json()["entries"]
    assert [(e["ledger"], e["voucher_type"]) for e in entries] == [("Cash", "Contra"), ("Cash", "Contra")]


def test_old_database_is_migrated(tmp_path):
    import sqlite3
    import db as dbmod
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ledgers (id INTEGER PRIMARY KEY, company TEXT, name TEXT, parent TEXT, "
                 "opening_balance REAL, closing_balance REAL, guid TEXT, fetched_at TEXT, UNIQUE (company, name))")
    conn.execute("INSERT INTO ledgers (company, name, parent) VALUES ('X', 'Cash', 'Cash-in-Hand'), "
                 "('X', 'Rent', 'Indirect Expenses')")
    conn.commit()
    conn.close()
    dbmod.init_db(path)
    dbmod.init_db(path)  # running again is harmless
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT name, cash_bank FROM ledgers ORDER BY name").fetchall() == [("Cash", 1), ("Rent", 0)]


def test_opening_import_switches_cash_bank_rows_to_contra(app, client, fake_tally, tmp_path):
    """Rows saved before Contra existed (or before ledgers were re-fetched) are corrected on open."""
    import db as dbmod
    client.post("/api/ledgers/fetch", json={"company": COMPANY})
    import_id = _upload(client, tmp_path).get_json()["import_id"]
    entries = {e["txn_date"]: e for e in client.get("/api/imports/%d" % import_id).get_json()["entries"]}
    atm, rent = entries["2026-04-07"], entries["2026-04-05"]
    with app.app_context():
        # old data: ATM row on Cash saved as a validated Payment; rent row wrongly Contra;
        # and the cash/bank flag missing (as after an upgrade without re-fetching)
        dbmod.execute("UPDATE entries SET ledger = 'Cash', voucher_type = 'Payment', status = 'validated' "
                      "WHERE id = ?", (atm["id"],))
        dbmod.execute("UPDATE entries SET voucher_type = 'Contra' WHERE id = ?", (rent["id"],))
        dbmod.execute("UPDATE ledgers SET cash_bank = 0")

    r = client.get("/api/imports/%d" % import_id).get_json()
    assert r["voucher_types_corrected"] == 2
    got = {e["id"]: (e["voucher_type"], e["status"]) for e in r["entries"]}
    assert got[atm["id"]] == ("Contra", "pending")      # re-validate after the change
    assert got[rent["id"]] == ("Payment", "pending")
    assert client.get("/api/imports/%d" % import_id).get_json()["voucher_types_corrected"] == 0

    # pushed rows are never touched
    client.post("/api/imports/%d/validate" % import_id, json={"entry_ids": [atm["id"]]})
    client.post("/api/imports/%d/push" % import_id, json={"entry_ids": [atm["id"]]})
    with app.app_context():
        dbmod.execute("UPDATE entries SET voucher_type = 'Payment' WHERE id = ?", (atm["id"],))
    assert client.get("/api/imports/%d" % import_id).get_json()["voucher_types_corrected"] == 0
