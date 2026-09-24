import json
import os
import sys
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from services import tally_client  # noqa: E402


class FakeResponse:
    def __init__(self, data, status=200):
        self.status_code = status
        self.content = (json.dumps(data) if not isinstance(data, str) else data).encode("utf-8")


class FakeTally:
    """In-memory stand-in for the Tally Prime JSON and XML APIs (patched over requests.post/get)."""

    def __init__(self):
        self.companies = ["Bhrama Enterprises"]
        self.ledgers = {
            "Cash": {"parent": "Cash-in-Hand", "openingbalance": "-5000.00"},
            "HDFC Bank": {"parent": "Bank Accounts", "openingbalance": "-100000.00"},
            "Office Rent": {"parent": "Indirect Expenses", "openingbalance": "0"},
            "Amazon Web Services": {"parent": "Indirect Expenses", "openingbalance": "0"},
            "Acme Traders": {"parent": "Sundry Debtors", "openingbalance": "-2500.00"},
        }
        self.vouchers = []
        self.requests = []
        self.fail_voucher_ledger = None

    def get(self, url, timeout=None):
        return FakeResponse("<RESPONSE>TallyPrime Server is Running</RESPONSE>")

    def post(self, url, headers=None, data=None, timeout=None):
        if headers.get("content-type", "").startswith("text/xml"):
            return FakeResponse(self._xml(data.decode("utf-8")))
        payload = json.loads(data)
        self.requests.append((headers, payload))
        req = headers["tallyrequest"]
        if req == "Export":
            return FakeResponse(self._export(headers, payload))
        return FakeResponse(self._import(headers, payload))

    def _export(self, headers, payload):
        if headers["id"] == tally_client.COLLECTION_COMPANIES:
            return {"status": "1", "data": {"collection": [{"metadata": {"type": "Company", "name": c}}
                                                           for c in self.companies]}}
        if headers["id"] == tally_client.COLLECTION_LEDGERS:
            return {"status": "1", "data": {"collection": [
                {"metadata": {"type": "Ledger", "name": n}, "parent": v["parent"],
                 "openingbalance": v["openingbalance"], "closingbalance": v["openingbalance"],
                 "guid": "guid-" + n}
                for n, v in self.ledgers.items()]}}
        if headers["id"] == tally_client.COLLECTION_GROUPS:
            return {"status": "1", "data": {"collection": [{"metadata": {"name": g}} for g in
                                                           ["Bank Accounts", "Indirect Expenses", "Sundry Debtors"]]}}
        return {"status": "0", "error": "unknown collection"}

    def _import(self, headers, payload):
        result = {"created": 0, "altered": 0, "deleted": 0, "errors": 0, "exceptions": 0}
        for msg in payload["tallymessage"]:
            meta = msg["metadata"]
            if meta["type"] == "Ledger":
                name, action = meta["name"], meta["action"]
                if action == "Create":
                    if name in self.ledgers:
                        return dict(result, errors=1, lineerror="Duplicate ledger")
                    self.ledgers[name] = {"parent": msg["parent"], "openingbalance": msg.get("openingbalance", "0")}
                    result["created"] += 1
                elif action == "Alter":
                    old = self.ledgers.pop(name)
                    old.update(parent=msg["parent"], openingbalance=msg.get("openingbalance", "0"))
                    self.ledgers[msg["name"]] = old
                    result["altered"] += 1
                else:
                    del self.ledgers[name]
                    result["deleted"] += 1
            else:
                names = [l["ledgername"] for l in msg["allledgerentries"]]
                if self.fail_voucher_ledger in names or any(n not in self.ledgers for n in names):
                    return dict(result, errors=1, lineerror="Ledger does not exist")
                self.vouchers.append(msg)
                result["created"] += 1
        return result

    # ---- XML protocol: translate the envelope, reuse the JSON handlers, answer in XML

    def _xml(self, text):
        env = ET.fromstring(text)
        self.requests.append(("xml", text))
        req = env.findtext("HEADER/TALLYREQUEST")
        if req == "Export":
            col = env.find("BODY/DESC/TDL/TDLMESSAGE/COLLECTION")
            if col is None:
                return "<RESPONSE>Unknown Request, cannot be processed</RESPONSE>"
            kind = col.findtext("TYPE")
            coll_id = {v: k for k, v in tally_client.COLLECTION_TYPES.items()}[kind]
            data = self._export({"id": coll_id}, {})
            out = ["<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER><BODY><DESC></DESC>"
                   "<DATA><COLLECTION>"]
            for obj in data["data"]["collection"]:
                name = obj["metadata"]["name"]
                out.append('<%s NAME="%s" RESERVEDNAME="">' % (kind.upper(), name))
                for k, v in obj.items():
                    if k != "metadata":
                        out.append('<%s TYPE="String">%s</%s>' % (k.upper(), v, k.upper()))
                out.append("</%s>" % kind.upper())
            out.append("</COLLECTION></DATA></BODY></ENVELOPE>")
            return "".join(out)

        messages = []
        for tm in env.findall("BODY/DATA/TALLYMESSAGE"):
            el = tm[0]
            if el.tag == "LEDGER":
                msg = {"metadata": {"type": "Ledger", "name": el.get("NAME"), "action": el.get("ACTION")},
                       "name": el.findtext("NAME.LIST/NAME") or el.get("NAME"),
                       "parent": el.findtext("PARENT"), "openingbalance": el.findtext("OPENINGBALANCE") or "0"}
            else:
                msg = {"metadata": {"type": "Voucher"}, "vouchertypename": el.findtext("VOUCHERTYPENAME"),
                       "allledgerentries": [{"ledgername": l.findtext("LEDGERNAME"), "amount": l.findtext("AMOUNT"),
                                             "isdeemedpositive": l.findtext("ISDEEMEDPOSITIVE")}
                                            for l in el.findall("ALLLEDGERENTRIES.LIST")]}
            messages.append(msg)
        result = self._import({}, {"tallymessage": messages})
        body = "".join("<%s>%s</%s>" % (k.upper(), v, k.upper()) for k, v in result.items())
        return "<RESPONSE>%s</RESPONSE>" % body


@pytest.fixture
def fake_tally(monkeypatch):
    fake = FakeTally()
    monkeypatch.setattr(tally_client.requests, "post", fake.post)
    monkeypatch.setattr(tally_client.requests, "get", fake.get)
    return fake


@pytest.fixture
def app(tmp_path):
    return create_app({"DB_PATH": str(tmp_path / "test.db"), "UPLOAD_DIR": str(tmp_path / "uploads"),
                       "TESTING": True})


@pytest.fixture(params=["json", "xml"])
def client(app, request):
    """Test client; every API test runs once with the JSON format and once with XML."""
    c = app.test_client()
    with app.app_context():
        import db
        db.set_setting("tally_format", request.param)
    return c
