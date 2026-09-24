"""Bank statement parsing: PDF / Excel / CSV -> normalised rows.

Output row: {txn_date: 'YYYY-MM-DD', narration, ref_no, debit, credit, balance}
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


def rows_from_table(table, mapping=None):
    """Convert a 2D list (first rows may be bank letterhead) into normalised entries.

    `mapping` may be supplied by the user as {field: column_index}; otherwise the header row
    is auto-detected by scanning the first 40 rows.
    """
    table = [[("" if c is None else c) for c in row] for row in table if row is not None]
    start = 0
    if not mapping:
        for i, row in enumerate(table[:40]):
            mapping = detect_columns(row)
            if mapping:
                start = i + 1
                break
    if not mapping:
        header_guess = next((r for r in table if sum(1 for c in r if str(c).strip()) >= 3), [])
        raise ParseError("Could not detect the statement columns. Please map them manually.",
                         headers=[str(c) for c in header_guess],
                         preview=[[str(c) for c in r] for r in table[:15]])
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
        })
    return out


# --------------------------------------------------------------------------- readers

def read_table(path):
    """Read a statement file into a 2D list of cells (all sheets / pages concatenated)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return _read_pdf_tables(path)
    if ext == ".csv":
        df = pd.read_csv(path, header=None, dtype=object, keep_default_na=False, skip_blank_lines=False,
                         on_bad_lines="skip", encoding_errors="replace")
        return df.values.tolist()
    if ext in (".xlsx", ".xls"):
        sheets = pd.read_excel(path, header=None, sheet_name=None, dtype=object)
        table = []
        for df in sheets.values():
            table.extend(df.where(pd.notna(df), "").values.tolist())
        return table
    raise ParseError("Unsupported file type: %s" % ext)


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


def parse_statement(path, mapping=None):
    """Parse a statement file. Raises ParseError (with headers/preview) when columns can't be found."""
    table = read_table(path)
    if path.lower().endswith(".pdf"):
        try:
            rows = rows_from_table(table, mapping) if table else []
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
    rows = rows_from_table(table, mapping)
    if not rows:
        raise ParseError("The statement has no transactions after the header row.")
    return rows
