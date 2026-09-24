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


def _normalise(rows, mapping):
    out = []
    for row in rows:
        date = parse_date(_cell(row, mapping, "txn_date"))
        narration = str(_cell(row, mapping, "narration") or "").strip()
        if not date:
            # Multi-line narration in PDFs: a row without a date continues the previous entry.
            if out and narration and not any(parse_amount(_cell(row, mapping, f))
                                             for f in ("debit", "credit", "amount")):
                out[-1]["narration"] = (out[-1]["narration"] + " " + narration).strip()
            continue
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
            "ref_no": str(_cell(row, mapping, "ref_no") or "").strip(),
            "debit": round(debit, 2),
            "credit": round(credit, 2),
            "balance": round(parse_amount(balance_raw), 2) if balance_raw not in (None, "") else None,
            "ledger": re.sub(r"\s+", " ", str(_cell(row, mapping, "ledger") or "")).strip(),
        })
    return out


# --------------------------------------------------------------------------- readers

def read_table(path):
    """Read a statement file into a 2D list of cells (all sheets / pages concatenated)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return _read_pdf_tables(path)
    if ext == ".csv":
        return _read_csv(path)
    if ext in (".xlsx", ".xls"):
        sheets = pd.read_excel(path, header=None, sheet_name=None, dtype=object)
        table = []
        for df in sheets.values():
            table.extend(df.where(pd.notna(df), "").values.tolist())
        return table
    raise ParseError("Unsupported file type: %s" % ext)


def _read_csv(path):
    """CSV rows as lists. Uses the csv module (not pandas) because bank CSVs often start with
    one-cell title lines, and rows of different lengths must all be kept."""
    import csv

    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(text.splitlines(), dialect)]


def _read_pdf_tables(path):
    import pdfplumber

    table = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for t in page.extract_tables() or []:
                table.extend(t)
    return table


def _read_pdf_lines(path):
    """Fallback for PDFs without ruled tables: parse text lines that start with a date.

    Line shape: <date> <narration...> [ref] <amount> [amount] <balance>. With two amounts we
    compare the balance with the previous one to decide whether the amount is a debit or a credit.
    """
    import pdfplumber

    entries = []
    prev_balance = None
    with pdfplumber.open(path) as pdf:
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


def parse_statement(path, mapping=None, header_row=None):
    """Parse a statement file. Raises ParseError (with headers/preview) when columns can't be found."""
    table = read_table(path)
    if path.lower().endswith(".pdf"):
        try:
            rows = rows_from_table(table, mapping, header_row) if table else []
        except ParseError:
            if mapping:
                raise
            rows = []
        if not rows and not mapping:
            rows = _read_pdf_lines(path)
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


RAW_PREVIEW_ROWS = 80
RAW_CELL_CHARS = 60


def inspect_statement(path, mapping=None, header_row=None):
    """Everything the column-mapping dialog needs, plus the parsed rows.

    With `mapping` None, the detected header row and mapping are used. Returns (info, rows):
      info = {mode: 'table'|'text', detected, header_row, mapping, columns, raw, count, problem}
    mode 'text' means a PDF without tables, read line by line (column mapping does not apply).
    """
    table = _clean_table(read_table(path))
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
        rows = _read_pdf_lines(path)
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
        "count": len(rows),
        "problem": problem,
    }
    return info, rows
