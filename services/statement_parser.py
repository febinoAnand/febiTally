"""Bank statement parsing: PDF / Excel / CSV -> normalised rows.

Output row: {txn_date: 'YYYY-MM-DD', narration, ref_no, debit, credit, balance, ledger}
(`ledger` is the statement's own ledger column, '' when not mapped.)
"""
import os
import re
from datetime import datetime

import pandas as pd

# Column synonyms, matched against normalised header text (lowercase, alphanumerics only).
COLUMN_SYNONYMS = {
    "txn_date": ["txndate", "transactiondate", "trandate", "date", "valuedate", "postingdate", "valuedt", "trndate"],
    "narration": ["narration", "description", "particulars", "details", "remarks", "transactiondetails",
                  "transactionremarks"],
    "ref_no": ["chqrefno", "chequeno", "chqno", "refno", "reference", "chqrefnumber", "refchqno",
               "chequerefno", "utr", "instrumentno"],
    "debit": ["withdrawalamt", "withdrawal", "withdrawals", "debit", "debitamount", "dr", "withdrawalamount",
              "debitamt"],
    "credit": ["depositamt", "deposit", "deposits", "credit", "creditamount", "cr", "depositamount",
               "creditamt"],
    "balance": ["closingbalance", "balance", "runningbalance", "balanceamt", "availablebalance"],
    "amount": ["amount", "txnamount", "transactionamount"],
    "drcr": ["drcr", "type", "crdr", "debitcredit"],
    # optional: a column holding the Tally ledger for each line (common in user-prepared sheets)
    "ledger": ["ledger", "ledgername", "tallyledger", "accounthead", "ledgeraccount", "ledgerhead"],
}
REQUIRED = ("txn_date", "narration")

DATE_FORMATS = ["%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d",
                "%d %b %Y", "%d-%b-%Y", "%d-%b-%y", "%d %b %y", "%d %B %Y", "%b %d, %Y", "%Y/%m/%d"]
DATE_RE = re.compile(r"^\s*(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}[ \-][A-Za-z]{3,9}[ \-]\d{2,4})")
AMOUNT_RE = re.compile(r"-?\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})|-?\d+\.\d{1,2}")


class PasswordRequired(Exception):
    """The statement file is password-protected and no (or a wrong) password was given."""

    def __init__(self, wrong_password=False):
        super().__init__("Incorrect password. Try again." if wrong_password
                         else "This statement is password-protected. Enter its password to open it.")
        self.wrong_password = wrong_password


class ParseError(Exception):
    """Raised when a statement cannot be parsed. `headers`/`preview` help the UI offer column mapping."""

    def __init__(self, message, headers=None, preview=None):
        super().__init__(message)
        self.headers = headers or []
        self.preview = preview or []


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def parse_date(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"\s+\d{1,2}:\d{2}(:\d{2})?\s*(AM|PM)?$", "", text, flags=re.I)  # drop time part
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_amount(value):
    """'1,23,456.00' -> 123456.0; '' / '-' / NaN -> 0.0. Sign and Dr/Cr suffix are ignored (abs value)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    if isinstance(value, (int, float)):
        return abs(float(value))
    text = re.sub(r"(?i)\b(dr|cr|inr|rs\.?)\b|₹", "", str(value))
    text = re.sub(r"[^0-9.]", "", text)
    try:
        return float(text) if text and text != "." else 0.0
    except ValueError:
        return 0.0


def detect_columns(headers):
    """Map our field names to column indexes from a header row. Returns {} if not a header row."""
    normed = [_norm(h) for h in headers]
    mapping = {}
    for field, synonyms in COLUMN_SYNONYMS.items():
        for syn in synonyms:  # synonyms are ordered by preference
            for idx, h in enumerate(normed):
                if idx in mapping.values():
                    continue
                if h == syn or (len(syn) > 3 and h.startswith(syn)):
                    mapping[field] = idx
                    break
            if field in mapping:
                break
    has_amount = "debit" in mapping or "credit" in mapping or "amount" in mapping
    if all(f in mapping for f in REQUIRED) and has_amount:
        return mapping
    return {}


def _clean_table(table):
    return [[("" if c is None else c) for c in row] for row in table if row is not None]


def find_header(table, scan=40):
    """Return (header_row_index, mapping) for the first row that looks like a header, or (None, {})."""
    for i, row in enumerate(table[:scan]):
        mapping = detect_columns(row)
        if mapping:
            return i, mapping
    return None, {}


def mapping_problem(mapping):
    """Why a user-supplied mapping can't be used, or None."""
    if not mapping or "txn_date" not in mapping or "narration" not in mapping:
        return "Map at least the Date and Narration columns."
    if not any(f in mapping for f in ("debit", "credit", "amount")):
        return "Map the Withdrawal/Deposit columns, or a single Amount column."
    return None


def rows_from_table(table, mapping=None, header_row=None):
    """Convert a 2D list (first rows may be bank letterhead) into normalised entries.

    `mapping` may be supplied by the user as {field: column_index}, with `header_row` the index
    of the header row (data starts below it); otherwise the header row is auto-detected.
    """
    table = _clean_table(table)
    if not mapping:
        header_row, mapping = find_header(table)
    if not mapping:
        header_guess = next((r for r in table if sum(1 for c in r if str(c).strip()) >= 3), [])
        raise ParseError("Could not detect the statement columns. Please map them manually.",
                         headers=[str(c) for c in header_guess],
                         preview=[[str(c) for c in r] for r in table[:15]])
    start = header_row + 1 if header_row is not None else 0
    return _normalise(table[start:], mapping)


def _cell(row, mapping, field):
    idx = mapping.get(field)
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def _clean_ref(value):
    """Cheque/reference number; placeholders banks print for "none" ('0', '-', 'NA') become ''."""
    ref = str(value if value is not None else "").strip()
    if isinstance(value, float) and value.is_integer():
        ref = str(int(value))
    return "" if ref.lower() in ("0", "-", "--", "na", "n/a", "nil", "none", "nan") else ref


def _normalise(rows, mapping):
    out = []
    continues = False  # may a date-less row still belong to the previous entry?
    for row in rows:
        date = parse_date(_cell(row, mapping, "txn_date"))
        narration = str(_cell(row, mapping, "narration") or "").strip()
        if not date:
            # Multi-line narration (common in PDFs): a row right below an entry, holding only
            # narration text, continues it. A blank row ends the table, so summary blocks such as
            # "Opening Balance / Debit Count" below the transactions are never glued on.
            others = [str(c).strip() for i, c in enumerate(row) if i != mapping.get("narration")]
            if not narration and not any(others):
                continues = False
            elif out and continues and narration and not any(others):
                out[-1]["narration"] = (out[-1]["narration"] + " " + narration).strip()
            else:
                continues = False
            continue
        continues = True
        debit = parse_amount(_cell(row, mapping, "debit"))
        credit = parse_amount(_cell(row, mapping, "credit"))
        if "amount" in mapping and not debit and not credit:
            # Single amount column: direction from a Dr/Cr column, a Dr/Cr suffix, or the sign.
            raw = str(_cell(row, mapping, "amount") or "").strip()
            amt = parse_amount(raw)
            hint = str(_cell(row, mapping, "drcr") or raw).lower()
            if re.search(r"\bcr\b|credit|\bc\b", hint):
                credit = amt
            elif re.search(r"\bdr\b|debit|\bd\b", hint) or raw.startswith("-"):
                debit = amt
            else:
                credit = amt
        if not debit and not credit:
            continue
        balance_raw = _cell(row, mapping, "balance")
        out.append({
            "txn_date": date,
            "narration": re.sub(r"\s+", " ", narration),
            "ref_no": _clean_ref(_cell(row, mapping, "ref_no")),
            "debit": round(debit, 2),
            "credit": round(credit, 2),
            "balance": round(parse_amount(balance_raw), 2) if balance_raw not in (None, "") else None,
            "ledger": re.sub(r"\s+", " ", str(_cell(row, mapping, "ledger") or "")).strip(),
        })
    return out


# --------------------------------------------------------------------------- readers

def read_table(path, password=None):
    """Read a statement file into a 2D list of cells (all sheets / pages concatenated).

    Spreadsheet files are recognised by their content, not their extension: banks often send a
    real .xlsx named .xls, an HTML table or Excel 2003 XML saved as .xls, or tab-separated text.
    Raises PasswordRequired for an encrypted PDF / Excel file when `password` is missing or wrong.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return _read_pdf_tables(path, password)
    if ext not in (".xlsx", ".xls", ".csv"):
        raise ParseError("Unsupported file type: %s" % ext)
    with open(path, "rb") as f:
        data = f.read()
    kind = sniff_format(data)
    if kind == "ole":
        data = _decrypt_office(data, password)  # no-op for an unencrypted .xls
        kind = sniff_format(data)
    if kind == "pdf":
        return _read_pdf_tables(path, password)
    if kind in ("xlsx", "ole"):
        return _read_workbook(data, "openpyxl" if kind == "xlsx" else "xlrd")
    text = _decode_text(data)
    if kind == "html":
        return _read_html_tables(text)
    if kind == "xml2003":
        return _read_spreadsheet_xml(text)
    return _read_csv_text(text)


def sniff_format(data):
    """'xlsx' (zip), 'ole' (old .xls or an encrypted workbook), 'pdf', 'html', 'xml2003' or 'text'."""
    if data[:4] == b"PK\x03\x04":
        return "xlsx"
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "ole"
    if data[:5] == b"%PDF-":
        return "pdf"
    head = _decode_text(data[:8192]).lstrip().lower()
    if head.startswith("<"):
        if "urn:schemas-microsoft-com:office:spreadsheet" in head or "<workbook" in head:
            return "xml2003"
        return "html"
    return "text"


def _decode_text(data):
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _read_workbook(data, engine):
    import io

    sheets = pd.read_excel(io.BytesIO(data), header=None, sheet_name=None, dtype=object, engine=engine)
    table = []
    for df in sheets.values():
        table.extend(df.where(pd.notna(df), "").values.tolist())
        table.append([])  # keep sheets apart
    return table


def _decrypt_office(data, password):
    """Decrypt an encrypted workbook in memory (never on disk); unencrypted data is returned as is.

    Excel's built-in default password (used for "read-only recommended" files) is tried first,
    so those open without asking.
    """
    import io

    import msoffcrypto
    from msoffcrypto.exceptions import DecryptionError, FileFormatError, InvalidKeyError

    try:
        encrypted = msoffcrypto.OfficeFile(io.BytesIO(data)).is_encrypted()
    except (FileFormatError, OSError, ValueError, KeyError, AssertionError):
        return data  # an ordinary .xls that msoffcrypto does not need to handle
    if not encrypted:
        return data
    for candidate in ("VelvetSweatshop", password):
        if not candidate:
            continue
        try:
            office = msoffcrypto.OfficeFile(io.BytesIO(data))
            office.load_key(password=candidate)
            out = io.BytesIO()
            office.decrypt(out)
            return out.getvalue()
        except (InvalidKeyError, DecryptionError):
            continue
    raise PasswordRequired(wrong_password=bool(password))


def _read_html_tables(text):
    """Rows of every <table> in an HTML page (what many banks save as .xls)."""
    from html.parser import HTMLParser

    class Tables(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.rows, self.row, self.cell, self.skip = [], None, None, 0

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.skip += 1
            elif tag == "tr":
                self._end_row()
                self.row = []
            elif tag in ("td", "th"):
                self._end_cell()
                if self.row is None:
                    self.row = []
                self.cell = []
                span = str(dict(attrs).get("colspan") or "1")
                self.colspan = int(span) if span.isdigit() else 1
            elif tag == "br" and self.cell is not None:
                self.cell.append(" ")

        def handle_endtag(self, tag):
            if tag in ("script", "style"):
                self.skip = max(self.skip - 1, 0)
            elif tag in ("td", "th"):
                self._end_cell()
            elif tag in ("tr", "table"):
                self._end_row()

        def handle_data(self, data):
            if self.cell is not None and not self.skip:
                self.cell.append(data)

        def _end_cell(self):
            if self.cell is not None and self.row is not None:
                self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
                self.row.extend([""] * (self.colspan - 1))
            self.cell = None

        def _end_row(self):
            self._end_cell()
            if self.row is not None:
                self.rows.append(self.row)
            self.row = None

    parser = Tables()
    parser.feed(text)
    parser.close()
    parser._end_row()
    return parser.rows


def _read_spreadsheet_xml(text):
    """Rows of an Excel 2003 XML ("XML Spreadsheet") workbook."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", text.strip()))
    ns = "{urn:schemas-microsoft-com:office:spreadsheet}"
    rows = []
    for row in root.iter(ns + "Row"):
        cells = []
        for cell in row.findall(ns + "Cell"):
            index = cell.get(ns + "Index")
            if index and index.isdigit():
                cells.extend([""] * (int(index) - 1 - len(cells)))
            data = cell.find(ns + "Data")
            cells.append("".join(data.itertext()).strip() if data is not None else "")
        rows.append(cells)
    return rows


def _open_pdf(path, password):
    """Open a PDF with pdfplumber; PasswordRequired if it is encrypted and the password is missing/wrong."""
    import pdfplumber

    try:
        pdf = pdfplumber.open(path, password=password or "")
        pdf.pages  # noqa: B018 - forces the document (and its encryption) to be read
        return pdf
    except Exception as exc:
        if "PDFPasswordIncorrect" in repr(exc) or type(exc).__name__ == "PDFPasswordIncorrect":
            raise PasswordRequired(wrong_password=bool(password))
        raise


def _read_csv_text(text):
    """Delimited text rows. Uses the csv module (not pandas) because bank CSVs often start with
    one-cell title lines, and rows of different lengths must all be kept."""
    import csv

    lines = text.splitlines()
    sample = lines[:200]

    # The delimiter that splits the most lines into 3+ fields wins. (csv.Sniffer is misled by
    # amounts such as 1,191.12 in tab-separated files.) Ties go to tab, then ; | and ,.
    def score(delim):
        return sum(1 for r in csv.reader(sample, delimiter=delim) if len(r) >= 3)

    delimiter = max(["\t", ";", "|", ","], key=lambda d: (score(d), -["\t", ";", "|", ","].index(d)))
    return [row for row in csv.reader(lines, delimiter=delimiter)]


def _read_pdf_tables(path, password=None):
    table = []
    with _open_pdf(path, password) as pdf:
        for page in pdf.pages:
            for t in page.extract_tables() or []:
                table.extend(t)
    return table


def _read_pdf_lines(path, password=None):
    """Fallback for PDFs without ruled tables: parse text lines that start with a date.

    Line shape: <date> <narration...> [ref] <amount> [amount] <balance>. With two amounts we
    compare the balance with the previous one to decide whether the amount is a debit or a credit.
    """
    entries = []
    prev_balance = None
    with _open_pdf(path, password) as pdf:
        for page in pdf.pages:
            for line in (page.extract_text() or "").splitlines():
                m = DATE_RE.match(line)
                if not m:
                    if entries and line.strip() and not AMOUNT_RE.search(line):
                        entries[-1]["narration"] += " " + line.strip()
                    continue
                date = parse_date(m.group(1))
                rest = line[m.end():].strip()
                amounts = AMOUNT_RE.findall(rest)
                if not date or not amounts:
                    continue
                first_amt = rest.find(amounts[0]) if len(amounts) < 2 else rest.find(amounts[-2])
                narration = rest[:first_amt].strip()
                # drop a leading value date that some banks print after the txn date
                vd = DATE_RE.match(narration)
                if vd:
                    narration = narration[vd.end():].strip()
                balance = parse_amount(amounts[-1]) if len(amounts) >= 2 else None
                amount = parse_amount(amounts[-2] if len(amounts) >= 2 else amounts[0])
                is_credit = bool(re.search(r"\bcr\b", rest[first_amt:], re.I)) and len(amounts) < 2
                if balance is not None and prev_balance is not None:
                    is_credit = balance > prev_balance
                prev_balance = balance if balance is not None else prev_balance
                entries.append({
                    "txn_date": date,
                    "narration": narration,
                    "ref_no": "",
                    "debit": 0.0 if is_credit else round(amount, 2),
                    "credit": round(amount, 2) if is_credit else 0.0,
                    "balance": balance,
                })
    return entries


def parse_statement(path, mapping=None, header_row=None, password=None):
    """Parse a statement file. Raises ParseError (with headers/preview) when columns can't be found."""
    table = read_table(path, password)
    if path.lower().endswith(".pdf"):
        try:
            rows = rows_from_table(table, mapping, header_row) if table else []
        except ParseError:
            if mapping:
                raise
            rows = []
        if not rows and not mapping:
            rows = _read_pdf_lines(path, password)
        if not rows:
            raise ParseError("No transactions found in the PDF. If it is a scanned image, export the "
                             "statement as Excel or a text PDF instead.",
                             headers=[str(c) for c in (table[0] if table else [])],
                             preview=[[str(c) for c in r] for r in table[:15]])
        return rows
    rows = rows_from_table(table, mapping, header_row)
    if not rows:
        raise ParseError("The statement has no transactions after the header row.")
    return rows


ACCOUNT_LABEL_RE = re.compile(r"\b(a/?c|acc(oun)?t)\.?\s*(no|num(ber)?|#)\b", re.I)


def extract_account_number(table, scan=40):
    """The account number printed in the statement header ('Account Number | 0125…', 'A/c No: 5010…').

    Returns the digits, or '' when none is found."""
    for row in table[:scan]:
        cells = [str(c).strip() for c in row]
        for i, cell in enumerate(cells):
            if not ACCOUNT_LABEL_RE.search(cell):
                continue
            after_colon = cell.split(":", 1)[1] if ":" in cell else ""
            for candidate in [after_colon] + cells[i + 1:i + 3]:
                digits = re.sub(r"\D", "", candidate)
                if 6 <= len(digits) <= 20 and not re.search(r"[A-Za-z]{3,}", candidate):
                    return digits
    return ""


def header_text(table, rows=8):
    """Text of the statement's first rows (bank name, statement title) for matching the bank ledger."""
    return " ".join(str(c) for r in table[:rows] for c in r if str(c).strip())


RAW_PREVIEW_ROWS = 80
RAW_CELL_CHARS = 60


def inspect_statement(path, mapping=None, header_row=None, password=None):
    """Everything the column-mapping dialog needs, plus the parsed rows.

    With `mapping` None, the detected header row and mapping are used. Returns (info, rows):
      info = {mode: 'table'|'text', detected, header_row, mapping, columns, raw, count, problem}
    mode 'text' means a PDF without tables, read line by line (column mapping does not apply).
    """
    table = _clean_table(read_table(path, password))
    detected_row, detected_map = find_header(table)
    user_mapping = mapping is not None  # {} from the dialog means "nothing mapped", not "auto-detect"
    if not user_mapping:
        mapping, header_row = detected_map, detected_row

    rows, mode = [], "table"
    problem = mapping_problem(mapping) if user_mapping or mapping else None
    if mapping and not problem:
        start = header_row + 1 if header_row is not None else 0
        rows = _normalise(table[start:], mapping)
    if not rows and not user_mapping and path.lower().endswith(".pdf"):
        rows = _read_pdf_lines(path, password)
        if rows:
            mode, problem = "text", None
    if not mapping and mode == "table" and not user_mapping:
        problem = ("Could not detect the statement columns. Pick the header row and map the columns."
                   if table else "No table found in the file. If it is a scanned PDF, export the "
                                 "statement as Excel or a text PDF instead.")
    elif mode == "table" and not problem and not rows:
        problem = "No transactions found below the header row with this mapping."

    raw = [[str(c)[:RAW_CELL_CHARS] for c in r] for r in table[:RAW_PREVIEW_ROWS]]
    info = {
        "mode": mode,
        "detected": bool(detected_map),
        "header_row": header_row,
        "mapping": mapping or {},
        "columns": max((len(r) for r in raw), default=0),
        "raw": raw,
        "total_rows": len(table),
        "account_number": extract_account_number(table),
        "header_text": header_text(table)[:500],
        "count": len(rows),
        "problem": problem,
    }
    return info, rows
