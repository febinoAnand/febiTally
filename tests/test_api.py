"""End-to-end API flow against the in-memory FakeTally."""
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
    assert {v["vouchertypename"] for v in fake_tally.vouchers} == {"Payment", "Receipt"}

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
