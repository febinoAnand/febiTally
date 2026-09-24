import xml.etree.ElementTree as ET

import pytest

from services import tally_client as tc
from services import tally_xml


def _xml(headers_payload):
    return ET.fromstring(tally_xml.to_xml(*headers_payload, collection_types=tc.COLLECTION_TYPES))


def test_object_export_matches_documented_request():
    env = _xml(tc.build_export("Bhrama Enterprises", "Object", "Cash", subtype="Ledger",
                               fetch_list=["Opening Balance", "Closing Balance"]))
    assert env.findtext("HEADER/TALLYREQUEST") == "Export"
    assert env.findtext("HEADER/SUBTYPE") == "Ledger"
    assert env.find("HEADER/ID").get("TYPE") == "Name" and env.findtext("HEADER/ID") == "Cash"
    sv = env.find("BODY/DESC/STATICVARIABLES")
    assert sv.findtext("SVEXPORTFORMAT") == "$$SysName:XML"
    assert sv.findtext("SVCURRENTCOMPANY") == "Bhrama Enterprises"
    assert [f.text for f in env.findall("BODY/DESC/FETCHLIST/FETCH")] == ["OpeningBalance", "ClosingBalance"]


def test_collection_export_uses_inline_tdl():
    env = _xml(tc.build_export("Co", "Collection", tc.COLLECTION_LEDGERS, fetch_list=tc.LEDGER_FIELDS))
    col = env.find("BODY/DESC/TDL/TDLMESSAGE/COLLECTION")
    assert env.findtext("HEADER/ID") == col.get("NAME") == "FebiLedgerCollection"
    assert col.findtext("TYPE") == "Ledger"
    assert col.findtext("FETCH") == "Name, Parent, OpeningBalance, ClosingBalance, GUID"


def test_ledger_alter_and_voucher_envelopes():
    env = _xml(tc.build_ledger_import("Co", "Alter", "Old & Co", "Sundry Creditors", 0, new_name="New"))
    led = env.find("BODY/DATA/TALLYMESSAGE/LEDGER")
    assert (led.get("NAME"), led.get("ACTION")) == ("Old & Co", "Alter")
    assert led.findtext("NAME.LIST/NAME") == "New"
    assert led.findtext("PARENT") == "Sundry Creditors"

    voucher = tc.build_voucher("HDFC Bank", {"voucher_type": "Payment", "debit": 500, "credit": 0,
                                             "ledger": "Rent", "txn_date": "2026-04-05", "narration": "<rent>"})
    vch = _xml(tc.build_voucher_import("Co", [voucher])).find("BODY/DATA/TALLYMESSAGE/VOUCHER")
    assert (vch.get("VCHTYPE"), vch.get("ACTION")) == ("Payment", "Create")
    assert vch.findtext("DATE") == "20260405" and vch.findtext("NARRATION") == "<rent>"
    lines = vch.findall("ALLLEDGERENTRIES.LIST")
    assert [(l.findtext("LEDGERNAME"), l.findtext("ISDEEMEDPOSITIVE"), l.findtext("AMOUNT")) for l in lines] == \
        [("Rent", "Yes", "-500.00"), ("HDFC Bank", "No", "500.00")]


def test_parse_collection_response_with_control_chars():
    text = ('<?xml version="1.0" encoding="utf-8"?><ENVELOPE><BODY><DATA><COLLECTION>'
            '<LEDGER NAME="Cash&#4;"><PARENT TYPE="String">Cash-in-Hand</PARENT>'
            '<OPENINGBALANCE TYPE="Amount">-5000.00</OPENINGBALANCE></LEDGER>'
            '<LEDGER NAME="HDFC Bank"><PARENT TYPE="String">Bank Accounts</PARENT></LEDGER>'
            '</COLLECTION></DATA></BODY></ENVELOPE>')
    ledgers = [tc.TallyClient._ledger_from(o) for o in tc.extract_objects(tally_xml.parse(text))]
    assert [(l["name"], l["parent"], l["opening_balance"]) for l in ledgers] == \
        [("Cash", "Cash-in-Hand", -5000.0), ("HDFC Bank", "Bank Accounts", 0.0)]


def test_parse_import_response_and_errors():
    ok = tally_xml.parse("<RESPONSE><CREATED>1</CREATED><ERRORS>0</ERRORS></RESPONSE>")
    assert tc.check_import_result(ok)["created"] == 1
    bad = tally_xml.parse("<RESPONSE><CREATED>0</CREATED><ERRORS>1</ERRORS>"
                          "<LINEERROR>Ledger 'X' does not exist!</LINEERROR></RESPONSE>")
    with pytest.raises(tc.TallyError, match="does not exist"):
        tc.check_import_result(bad)
    with pytest.raises(tally_xml.XmlError, match="Unknown Request"):
        tally_xml.parse("<RESPONSE>Unknown Request, cannot be processed</RESPONSE>")
    with pytest.raises(tally_xml.XmlError, match="non-XML"):
        tally_xml.parse("{not xml}")
