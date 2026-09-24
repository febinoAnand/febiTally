import pytest

from services import tally_client as tc


def test_export_matches_documented_request():
    headers, payload = tc.build_export("Bhrama Enterprises", "Object", "Cash", subtype="Ledger",
                                       fetch_list=["Opening Balance", "Closing Balance"])
    assert headers == {"tallyrequest": "Export", "type": "Object", "subtype": "Ledger", "id": "Cash"}
    assert payload == {
        "static_variables": [{"name": "svExportFormat", "value": "jsonEx"},
                             {"name": "svCurrentCompany", "value": "Bhrama Enterprises"}],
        "fetch_list": ["Opening Balance", "Closing Balance"],
    }


def test_extract_objects_handles_key_variants():
    data = {"static_variables": [{"name": "svCurrentCompany", "value": "X"}],
            "data": {"LEDGER": [
                {"NAME": ["Cash"], "PARENT": "Cash-in-Hand", "OPENINGBALANCE": "1,000.00 Dr"},
                {"metadata": {"name": "Bank"}, "Parent": {"value": "Bank Accounts"}, "Closing Balance": 50},
            ]}}
    objs = tc.extract_objects(data)
    ledgers = [tc.TallyClient._ledger_from(o) for o in objs]
    assert [l["name"] for l in ledgers] == ["Cash", "Bank"]
    assert ledgers[0]["opening_balance"] == -1000.0
    assert ledgers[1]["parent"] == "Bank Accounts"
    assert ledgers[1]["closing_balance"] == 50.0


def test_import_result_errors():
    assert tc.check_import_result({"created": "1", "errors": "0"})["created"] == 1
    with pytest.raises(tc.TallyError, match="Ledger does not exist"):
        tc.check_import_result({"created": 0, "errors": 1, "lineerror": "Ledger does not exist"})


def test_payment_and_receipt_vouchers():
    pay = tc.build_voucher("HDFC Bank", {"voucher_type": "Payment", "debit": 500, "credit": 0,
                                         "ledger": "Office Rent", "txn_date": "2026-04-05",
                                         "narration": "Rent", "ref_no": "781"})
    assert pay["date"] == "20260405"
    assert pay["reference"] == "781"
    dr, cr = pay["allledgerentries"]
    assert (dr["ledgername"], dr["isdeemedpositive"], dr["amount"]) == ("Office Rent", "Yes", "-500.00")
    assert (cr["ledgername"], cr["isdeemedpositive"], cr["amount"]) == ("HDFC Bank", "No", "500.00")

    rec = tc.build_voucher("HDFC Bank", {"voucher_type": "Receipt", "debit": 0, "credit": 1200,
                                         "ledger": "Acme Traders", "txn_date": "2026-04-03"})
    dr, cr = rec["allledgerentries"]
    assert (dr["ledgername"], cr["ledgername"]) == ("HDFC Bank", "Acme Traders")


def test_unreachable_tally():
    with pytest.raises(tc.TallyError, match="Cannot reach Tally"):
        tc.TallyClient("127.0.0.1", 1, timeout=1).list_companies()


def test_contra_voucher_direction_follows_statement():
    out = tc.build_voucher("HDFC Bank", {"voucher_type": "Contra", "debit": 2000, "credit": 0,
                                         "ledger": "Cash", "txn_date": "2026-04-07"})
    assert out["metadata"]["vchtype"] == out["vouchertypename"] == "Contra"
    assert [l["ledgername"] for l in out["allledgerentries"]] == ["Cash", "HDFC Bank"]
    inward = tc.build_voucher("HDFC Bank", {"voucher_type": "Contra", "debit": 0, "credit": 5000,
                                            "ledger": "Cash", "txn_date": "2026-04-08"})
    assert [l["ledgername"] for l in inward["allledgerentries"]] == ["HDFC Bank", "Cash"]


def test_cash_bank_groups():
    from services.ledger_groups import is_cash_bank_group
    parents = {"Current Accounts": "Bank Accounts", "Savings": "Current Accounts", "Rent": "Indirect Expenses",
               "Loop A": "Loop B", "Loop B": "Loop A"}
    assert is_cash_bank_group("Cash-in-Hand") and is_cash_bank_group("bank od a/c")
    assert is_cash_bank_group("Savings", parents)
    assert not is_cash_bank_group("Rent", parents) and not is_cash_bank_group("", parents)
    assert not is_cash_bank_group("Loop A", parents)  # malformed hierarchy does not hang
