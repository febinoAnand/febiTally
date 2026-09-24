"""Tally Prime HTTP API client (JSON or XML, chosen on the Settings page).

Every call to Tally goes through this module. The request builders (``build_*``) and the
response parsers (``extract_objects``, ``check_import_result``) are plain functions so the
JSON shapes can be adjusted in one place if a Tally Prime build names things differently.

Request convention (see Documentation/API/TaskPullLedgerCashJSON.txt):
    headers: content-type, version, tallyrequest, type, subtype, id
    payload: {"static_variables": [...], "fetch_list": [...]}
In XML mode the same requests are converted to XML envelopes by services/tally_xml.py.
"""
import json
import re

import requests

from services import tally_xml

FORMATS = ("json", "xml")

# Collection names used for exports. Built-in TDL collections; change here if needed.
COLLECTION_COMPANIES = "List of Companies"
COLLECTION_LEDGERS = "List of Ledgers"
COLLECTION_GROUPS = "List of Groups"

# TDL object type of each collection; XML mode sends these as inline TDL collections.
COLLECTION_TYPES = {COLLECTION_COMPANIES: "Company", COLLECTION_LEDGERS: "Ledger", COLLECTION_GROUPS: "Group"}

LEDGER_FIELDS = ["Name", "Parent", "Opening Balance", "Closing Balance", "GUID"]


class TallyError(Exception):
    """Raised when Tally is unreachable or reports an error."""


def _connection_error(url, exc):
    if isinstance(exc, requests.Timeout):
        return TallyError("Tally at %s did not respond in time. Is a company loaded and Tally idle?" % url)
    return TallyError("Cannot reach Tally at %s. Check that Tally Prime is running with the HTTP server "
                      "enabled on this port (F1 > Settings > Connectivity)." % url)


# --------------------------------------------------------------------------- helpers

def _norm(key):
    """Normalise a JSON key: 'Opening Balance', '$OpeningBalance', 'OPENINGBALANCE' -> 'openingbalance'."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _scalar(value):
    """Tally may wrap values as {"value": x}, ["x"] or {"$value": x}; unwrap to a plain value."""
    while True:
        if isinstance(value, list):
            if not value:
                return None
            value = value[0]
        elif isinstance(value, dict):
            for k, v in value.items():
                if _norm(k) in ("value", "text", "name"):
                    value = v
                    break
            else:
                return None
        else:
            return value


def _field(obj, *names):
    """Return a field from a Tally object, matching keys case/space-insensitively.
    Also looks inside a 'metadata' sub-object."""
    wanted = {_norm(n) for n in names}
    for k, v in obj.items():
        if _norm(k) in wanted:
            return _scalar(v)
    for k, v in obj.items():
        if _norm(k) == "metadata" and isinstance(v, dict):
            return _field(v, *names)
    return None


def parse_amount(value):
    """'1,234.50 Dr' -> -1234.5 (Tally convention: debit is negative). None/'' -> 0.0."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    sign = -1 if re.search(r"\bdr\b", text, re.I) else 1
    num = re.sub(r"[^0-9.\-]", "", text)
    try:
        return sign * float(num) if num not in ("", "-", ".") else 0.0
    except ValueError:
        return 0.0


def extract_objects(data, required=("name",)):
    """Walk a Tally JSON response and return every dict that has the required fields."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            if {_norm(k) for k in node} == {"name", "value"}:
                return  # echoed static variable, not a Tally object
            if all(_field(node, r) not in (None, "") for r in required):
                found.append(node)
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def find_line_error(data):
    """Return the first LINEERROR message in a response, or None."""
    if isinstance(data, dict):
        for k, v in data.items():
            if _norm(k) == "lineerror" and _scalar(v):
                return str(_scalar(v))
            found = find_line_error(v)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = find_line_error(item)
            if found:
                return found
    return None


def check_import_result(data):
    """Parse an Import response. Returns counts; raises TallyError if Tally reported errors."""
    counts = {"created": 0, "altered": 0, "deleted": 0, "errors": 0, "exceptions": 0}
    messages = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                nk = _norm(k)
                if nk in counts:
                    try:
                        counts[nk] += int(float(_scalar(v) or 0))
                    except (TypeError, ValueError):
                        pass
                elif nk in ("lineerror", "error", "errormessage"):
                    msg = _scalar(v)
                    if msg:
                        messages.append(str(msg))
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    if messages or counts["errors"] or counts["exceptions"]:
        raise TallyError("; ".join(messages) or "Tally reported %d error(s), %d exception(s)"
                         % (counts["errors"], counts["exceptions"]))
    return counts


def _static_vars(company=None, export=False):
    sv = []
    if export:
        sv.append({"name": "svExportFormat", "value": "jsonEx"})
    if company:
        sv.append({"name": "svCurrentCompany", "value": company})
    return sv


def _fmt_amount(value):
    return "%.2f" % float(value)


# --------------------------------------------------------------------------- builders

def build_export(company, req_type, req_id, subtype=None, fetch_list=None):
    headers = {"tallyrequest": "Export", "type": req_type, "id": req_id}
    if subtype:
        headers["subtype"] = subtype
    payload = {"static_variables": _static_vars(company, export=True)}
    if fetch_list:
        payload["fetch_list"] = list(fetch_list)
    return headers, payload


def build_ledger_import(company, action, name, parent=None, opening_balance=None, new_name=None):
    obj = {"metadata": {"type": "Ledger", "name": name, "action": action}}
    if action != "Delete":
        obj["name"] = new_name or name
        if parent:
            obj["parent"] = parent
        if opening_balance is not None:
            obj["openingbalance"] = _fmt_amount(opening_balance)
    headers = {"tallyrequest": "Import", "type": "Data", "id": "All Masters"}
    payload = {"static_variables": _static_vars(company), "tallymessage": [obj]}
    return headers, payload


def build_voucher(bank_ledger, entry):
    """Build one Payment / Receipt / Contra voucher from a statement entry.

    Tally sign convention: debit lines carry isdeemedpositive=Yes and a negative amount.
    The direction follows the statement:
    money out of the bank (withdrawal): Dr the row's ledger, Cr bank   (Payment, or Contra to cash/other bank)
    money into the bank (deposit):      Dr bank, Cr the row's ledger   (Receipt, or Contra from cash/other bank)
    """
    vtype = entry["voucher_type"]
    withdrawal = float(entry["debit"] or 0) > 0
    amount = float(entry["debit"] or 0) or float(entry["credit"] or 0)
    if withdrawal:
        dr_ledger, cr_ledger = entry["ledger"], bank_ledger
    else:
        dr_ledger, cr_ledger = bank_ledger, entry["ledger"]
    voucher = {
        "metadata": {"type": "Voucher", "action": "Create", "vchtype": vtype},
        "date": entry["txn_date"].replace("-", ""),
        "vouchertypename": vtype,
        "narration": entry.get("narration") or "",
        "allledgerentries": [
            {"ledgername": dr_ledger, "isdeemedpositive": "Yes", "amount": _fmt_amount(-amount)},
            {"ledgername": cr_ledger, "isdeemedpositive": "No", "amount": _fmt_amount(amount)},
        ],
    }
    if entry.get("ref_no"):
        voucher["reference"] = entry["ref_no"]
    return voucher


def build_voucher_import(company, vouchers):
    headers = {"tallyrequest": "Import", "type": "Data", "id": "Vouchers"}
    payload = {"static_variables": _static_vars(company), "tallymessage": list(vouchers)}
    return headers, payload


# --------------------------------------------------------------------------- client

class TallyClient:
    """Talks to Tally Prime in JSON (Tally Prime 7+) or XML (all releases).

    Requests are always built in the JSON-API shape; in XML mode they are converted by
    tally_xml and the XML response is converted back to dicts, so callers see one format.
    """

    def __init__(self, host, port, timeout=15, fmt="json"):
        self.url = "http://%s:%s" % (host, port)
        self.timeout = timeout
        self.fmt = fmt if fmt in FORMATS else "json"
        # Raw text of the last request/response, in the configured format (shown in the UI).
        self.last_request = None
        self.last_raw = None

    def _decode(self, resp):
        raw = resp.content
        for enc in ("utf-8-sig", "utf-16"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("latin-1")

    def _send(self, body, headers):
        """POST a raw body to Tally and return the decoded response text."""
        self.last_request, self.last_raw = body, None
        try:
            resp = requests.post(self.url, headers=headers, data=body.encode("utf-8"), timeout=self.timeout)
        except requests.RequestException as exc:
            raise _connection_error(self.url, exc)
        text = self._decode(resp).strip()
        self.last_raw = text
        if resp.status_code != 200:
            raise TallyError("Tally returned HTTP %s: %s" % (resp.status_code, text[:300]))
        return text

    def _parse_json(self, text):
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            raise TallyError("Tally returned a non-JSON response: %s" % text[:300])
        if isinstance(data, dict):
            status = data.get("status")
            if str(status) == "0":
                raise TallyError(str(data.get("error") or data.get("data") or "Tally request failed"))
        return data

    def _parse_xml(self, text):
        try:
            return tally_xml.parse(text)
        except tally_xml.XmlError as exc:
            raise TallyError(str(exc))

    def post(self, headers, payload):
        """Send a request (JSON-API shape) in the configured format and return the parsed response."""
        if self.fmt == "xml":
            body = tally_xml.to_xml(headers, payload, COLLECTION_TYPES)
            data = self._parse_xml(self._send(body, {"content-type": "text/xml;charset=utf-8"}))
        else:
            all_headers = {"content-type": "application/json", "version": "1"}
            all_headers.update(headers)
            data = self._parse_json(self._send(json.dumps(payload), all_headers))
        if headers.get("tallyrequest") == "Export":
            error = find_line_error(data)
            if error:
                raise TallyError(error)
        return data

    def post_raw(self, body, headers=None):
        """API console: send a raw body as-is. Returns (raw_text, parsed or None)."""
        if self.fmt == "xml":
            text = self._send(body, {"content-type": "text/xml;charset=utf-8"})
            try:
                return text, tally_xml.parse(text)
            except tally_xml.XmlError:
                return text, None
        all_headers = {"content-type": "application/json", "version": "1"}
        all_headers.update(headers or {})
        text = self._send(body, all_headers)
        try:
            return text, json.loads(text)
        except ValueError:
            return text, None

    # ---- connectivity

    def ping(self):
        try:
            resp = requests.get(self.url, timeout=5)
        except requests.RequestException as exc:
            raise _connection_error(self.url, exc)
        return self._decode(resp).strip() or "Tally is running"

    # ---- exports

    def list_companies(self):
        data = self.post(*build_export(None, "Collection", COLLECTION_COMPANIES, fetch_list=["Name"]))
        names = []
        for obj in extract_objects(data):
            name = str(_field(obj, "name")).strip()
            if name and name not in names:
                names.append(name)
        return names

    def list_ledgers(self, company):
        data = self.post(*build_export(company, "Collection", COLLECTION_LEDGERS, fetch_list=LEDGER_FIELDS))
        return [self._ledger_from(obj) for obj in extract_objects(data)]

    def get_ledger(self, company, name):
        data = self.post(*build_export(company, "Object", name, subtype="Ledger",
                                       fetch_list=LEDGER_FIELDS))
        objs = extract_objects(data)
        return self._ledger_from(objs[0]) if objs else None

    def group_parents(self, company):
        """{group name: parent group name} for every group of the company ('' for primary groups)."""
        data = self.post(*build_export(company, "Collection", COLLECTION_GROUPS, fetch_list=["Name", "Parent"]))
        parents = {}
        for obj in extract_objects(data):
            name = str(_field(obj, "name")).strip()
            parent = re.sub(r"^[\x00-\x1f\s]*primary$", "", str(_field(obj, "parent") or "").strip(), flags=re.I)
            parents[name] = parent.strip()
        return parents

    def list_groups(self, company):
        return sorted(self.group_parents(company))

    @staticmethod
    def _ledger_from(obj):
        return {
            "name": str(_field(obj, "name")).strip(),
            "parent": str(_field(obj, "parent") or "").strip(),
            "opening_balance": parse_amount(_field(obj, "openingbalance")),
            "closing_balance": parse_amount(_field(obj, "closingbalance")),
            "guid": _field(obj, "guid") or "",
        }

    # ---- master imports

    def create_ledger(self, company, name, parent, opening_balance=0):
        return check_import_result(self.post(*build_ledger_import(
            company, "Create", name, parent, opening_balance)))

    def alter_ledger(self, company, name, parent, opening_balance=0, new_name=None):
        return check_import_result(self.post(*build_ledger_import(
            company, "Alter", name, parent, opening_balance, new_name)))

    def delete_ledger(self, company, name):
        return check_import_result(self.post(*build_ledger_import(company, "Delete", name)))

    # ---- vouchers

    def post_voucher(self, company, bank_ledger, entry):
        """Create one voucher. Returns (counts, raw_response); raises TallyError on failure."""
        data = self.post(*build_voucher_import(company, [build_voucher(bank_ledger, entry)]))
        counts = check_import_result(data)
        if not counts["created"] and not counts["altered"]:
            raise TallyError("Tally did not create the voucher: %s" % (self.last_raw or "")[:300])
        return counts, data
