"""Bank statement imports: upload + parse, review, validate and push to Tally."""
import json
import os
import re
import time
import uuid

from flask import Blueprint, current_app, jsonify, request
from werkzeug.utils import secure_filename

import db
from routes import ApiError, body, exchange, require_company, tally
from services import ledger_matcher
from services.ledger_groups import CASH_BANK_GROUPS
from services.statement_parser import (ParseError, PasswordRequired, inspect_statement, parse_date,
                                      parse_statement)
from services.tally_client import TallyError

bp = Blueprint("api_imports", __name__, url_prefix="/api/imports")

VOUCHER_TYPES = ("Payment", "Receipt", "Contra")
MAPPABLE_FIELDS = ["txn_date", "narration", "ref_no", "debit", "credit", "balance", "amount", "drcr", "ledger"]
PREVIEW_SAMPLE_ROWS = 15
MAX_NARRATION = 1000  # characters; sent to Tally as the voucher narration
EDITABLE = ("txn_date", "narration", "ref_no", "debit", "credit", "ledger", "voucher_type")


# --------------------------------------------------------------------------- helpers

def _get_import(import_id):
    imp = db.query("SELECT * FROM imports WHERE id = ?", (import_id,), one=True)
    if not imp:
        raise ApiError("Import not found.", 404)
    return imp


def _get_entry(import_id, entry_id):
    entry = db.query("SELECT * FROM entries WHERE id = ? AND import_id = ?", (entry_id, import_id), one=True)
    if not entry:
        raise ApiError("Entry not found.", 404)
    return entry


def _ledger_names(company):
    return [r["name"] for r in db.query("SELECT name FROM ledgers WHERE company = ?", (company,))]


def _cash_bank_names(company):
    """Ledgers under Cash-in-Hand / Bank Accounts / Bank OD / Bank OCC (they take Contra vouchers).

    Uses the flag set on Fetch from Tally (which also covers sub-groups), plus any ledger whose own
    group is one of those, so it works even before ledgers are fetched again after an upgrade.
    """
    groups = sorted(CASH_BANK_GROUPS)
    return {r["name"] for r in db.query(
        "SELECT name FROM ledgers WHERE company = ? AND (cash_bank = 1 OR lower(parent) IN (%s))"
        % ",".join("?" * len(groups)), [company] + groups)}


def _sync_voucher_types(imp):
    """Make open entries' voucher types agree with their ledgers: Contra for cash/bank ledgers,
    Payment/Receipt otherwise. Validated entries that change go back to pending. Returns the count."""
    cash_bank = _cash_bank_names(imp["company"])
    changed = 0
    for e in db.query("SELECT id, ledger, voucher_type, debit, status FROM entries WHERE import_id = ? "
                      "AND status IN ('pending', 'validated', 'failed') AND ledger != ''", (imp["id"],)):
        is_cb = e["ledger"] in cash_bank
        if is_cb == (e["voucher_type"] == "Contra"):
            continue
        status = "pending" if e["status"] == "validated" else e["status"]
        db.execute("UPDATE entries SET voucher_type = ?, status = ? WHERE id = ?",
                   (default_voucher_type(e["ledger"], e["debit"], cash_bank), status, e["id"]))
        changed += 1
    if changed:
        _refresh_status(imp["id"])
    return changed


def default_voucher_type(ledger, debit, cash_bank):
    """Contra for cash/bank ledgers, else Payment for withdrawals and Receipt for deposits."""
    if ledger and ledger in cash_bank:
        return "Contra"
    return "Payment" if float(debit or 0) > 0 else "Receipt"


def _rules(company):
    return {r["keyword"]: r["ledger"] for r in
            db.query("SELECT keyword, ledger FROM ledger_rules WHERE company = ?", (company,))}


def _refresh_status(import_id):
    counts = {r["status"]: r["n"] for r in
              db.query("SELECT status, COUNT(*) n FROM entries WHERE import_id = ? GROUP BY status", (import_id,))}
    active = sum(n for s, n in counts.items() if s != "skipped")
    pushed = counts.get("pushed", 0)
    if active and pushed == active:
        status = "pushed"
    elif pushed:
        status = "partial"
    elif active and counts.get("validated", 0) == active:
        status = "validated"
    else:
        status = "draft"
    db.execute("UPDATE imports SET status = ? WHERE id = ?", (status, import_id))
    return status


def validate_entry(entry, ledger_names, bank_ledger, cash_bank=frozenset()):
    """Return a list of problems with an entry (empty list = valid).

    cash_bank: names of cash/bank ledgers. Those need a Contra voucher, and Contra needs one of them.
    """
    problems = []
    if not parse_date(entry.get("txn_date")):
        problems.append("invalid date")
    debit, credit = float(entry.get("debit") or 0), float(entry.get("credit") or 0)
    if (debit > 0) == (credit > 0):
        problems.append("exactly one of withdrawal/deposit must be greater than zero")
    ledger = entry.get("ledger") or ""
    if not ledger:
        problems.append("ledger not selected")
    elif ledger not in ledger_names:
        problems.append("ledger '%s' not found in Tally ledgers (fetch ledgers again?)" % ledger)
    elif ledger == bank_ledger:
        problems.append("ledger cannot be the bank ledger itself")
    vtype = entry.get("voucher_type")
    if vtype not in VOUCHER_TYPES:
        problems.append("voucher type must be Payment, Receipt or Contra")
    elif ledger in ledger_names and ledger != bank_ledger:
        if vtype == "Contra" and ledger not in cash_bank:
            problems.append("Contra is only for transfers with cash or bank ledgers; '%s' is not one" % ledger)
        elif vtype != "Contra" and ledger in cash_bank:
            problems.append("'%s' is a cash/bank ledger: use a Contra voucher" % ledger)
    if vtype in ("Payment", "Receipt") and (debit or credit):
        expected = "Payment" if debit > 0 else "Receipt"
        if vtype != expected:
            problems.append("%s voucher does not match a %s" % (vtype, "withdrawal" if debit > 0 else "deposit"))
    return problems


# --------------------------------------------------------------------------- list / create

@bp.get("")
def list_imports():
    return jsonify({"imports": db.query(
        "SELECT i.*, COUNT(e.id) total, SUM(e.status = 'pending') pending, "
        "SUM(e.status = 'validated') validated, SUM(e.status = 'pushed') pushed, "
        "SUM(e.status = 'failed') failed FROM imports i LEFT JOIN entries e ON e.import_id = i.id "
        "GROUP BY i.id ORDER BY i.id DESC")})


UPLOAD_TTL_SECONDS = 2 * 24 * 3600
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def _cleanup_uploads():
    """Remove uploaded statements older than two days (previews that were never imported)."""
    folder = current_app.config["UPLOAD_DIR"]
    now = time.time()
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if "__" in name and os.path.isfile(path) and now - os.path.getmtime(path) > UPLOAD_TTL_SECONDS:
            try:
                os.remove(path)
            except OSError:
                pass


def _save_upload(upload):
    """Save an uploaded statement; returns (token, path)."""
    if not upload or not upload.filename:
        raise ApiError("Choose a statement file to upload.")
    ext = os.path.splitext(upload.filename)[1].lower()
    if ext not in current_app.config["ALLOWED_EXTENSIONS"]:
        raise ApiError("Unsupported file type %s. Use PDF, XLSX, XLS or CSV." % ext)
    token = uuid.uuid4().hex
    name = secure_filename(upload.filename) or "statement" + ext
    path = os.path.join(current_app.config["UPLOAD_DIR"], "%s__%s" % (token, name))
    upload.save(path)
    return token, path


def _upload_path(token):
    """Find a previously uploaded statement by its token; returns (path, original_name)."""
    if not TOKEN_RE.match(token or ""):
        raise ApiError("Invalid upload reference. Upload the file again.")
    folder = current_app.config["UPLOAD_DIR"]
    for name in os.listdir(folder):
        if name.startswith(token + "__"):
            return os.path.join(folder, name), name[len(token) + 2:]
    raise ApiError("The uploaded file has expired. Upload it again.", 410)


def _mapping_arg(raw, header_row_raw):
    """Parse mapping ({field: column}) and header row from form/JSON values."""
    mapping = None  # None = auto-detect; {} = user cleared every column
    if raw not in (None, ""):
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            mapping = {str(k): int(v) for k, v in data.items() if v not in ("", None)}
        except (ValueError, TypeError, AttributeError):
            raise ApiError("Invalid column mapping.")
        unknown = set(mapping) - set(MAPPABLE_FIELDS)
        if unknown:
            raise ApiError("Unknown column field(s): %s" % ", ".join(sorted(unknown)))
    header_row = None
    if header_row_raw not in (None, ""):
        try:
            header_row = int(header_row_raw)
        except (TypeError, ValueError):
            raise ApiError("Invalid header row.")
    return mapping, header_row


def _password_needed(exc, token, filename):
    """422 telling the browser to ask for the statement's password (the upload is kept by token)."""
    return ApiError(str(exc), 422, need_password=True, wrong_password=exc.wrong_password,
                    token=token, filename=filename)


def _inspect(path, mapping, header_row, password, token, filename):
    try:
        return inspect_statement(path, mapping, header_row, password)
    except PasswordRequired as exc:
        raise _password_needed(exc, token, filename)
    except Exception as exc:  # corrupt or unsupported files
        raise ApiError("Could not read the file: %s" % exc, 422)


# Words in bank / ledger names that say nothing about which bank it is.
BANK_STOPWORDS = {"bank", "banks", "ltd", "limited", "the", "of", "and", "account", "accounts", "statement",
                  "current", "savings", "saving", "casa", "od", "occ", "cc", "branch", "customer", "name", "pvt",
                  "india", "indian", "number", "from", "to", "xls", "xlsx", "csv", "pdf", "salary"}


def _bank_account_key(company, account):
    return "bank_account:%s:%s" % (company, account)


def _bank_words(text):
    return {w for w in re.split(r"[^a-z]+", (text or "").lower()) if len(w) >= 3 and w not in BANK_STOPWORDS}


def _suggest_bank(company, account, filename, header):
    """Most likely bank ledger for an uploaded statement: {ledger, reason}, or None.

    1. the ledger chosen last time for this account number,
    2. a bank ledger whose name contains the account's last 4+ digits ("HDFC Bank - 5678"),
    3. a bank ledger whose name shares a word (4+ letters in common) with the file name or
       statement header ("Dhanbank_….xls" -> "Dhanlaxmi Bank").
    """
    banks = [r for r in db.query(
        "SELECT name, parent, cash_bank FROM ledgers WHERE company = ? ORDER BY name COLLATE NOCASE", (company,))
        if (r["cash_bank"] or (r["parent"] or "").lower() in CASH_BANK_GROUPS)
        and (r["parent"] or "").lower() != "cash-in-hand"]
    names = {b["name"] for b in banks}
    tail = account[-4:] if account else ""
    if account:
        saved = db.get_setting(_bank_account_key(company, account))
        if saved in names:
            return {"ledger": saved, "reason": "Used for account …%s last time" % tail}
        for b in banks:
            digits = re.sub(r"\D", "", b["name"])
            if len(digits) >= 4 and (account.endswith(digits) or digits.endswith(tail)):
                return {"ledger": b["name"], "reason": "Ledger name matches account …%s" % tail}
    hints = _bank_words(re.sub(r"[_\-.]", " ", os.path.splitext(filename or "")[0])) | _bank_words(header)
    best, best_len = None, 0
    for b in banks:
        for word in _bank_words(b["name"]):
            for hint in hints:
                common = len(os.path.commonprefix([word, hint]))
                if common >= 4 and common > best_len:
                    best, best_len = b["name"], common
    if best:
        return {"ledger": best, "reason": "Ledger name matches the bank named in the statement"}
    return None


@bp.post("/preview")
def preview_import():
    """Upload (or re-inspect) a statement for the column-mapping dialog. Nothing is imported.

    multipart: file  — or form/JSON: token; optional company (to suggest the bank ledger),
    mapping (JSON {field: column}), header_row,
    password (for an encrypted PDF/Excel file). A protected file without the right password gets
    422 {need_password, wrong_password, token}: ask for the password and send it with the token.
    """
    data = request.form if request.files or request.form else body(request)
    if request.files.get("file"):
        _cleanup_uploads()
        token, path = _save_upload(request.files["file"])
        filename = request.files["file"].filename
    else:
        token = data.get("token")
        path, filename = _upload_path(token)
    mapping, header_row = _mapping_arg(data.get("mapping"), data.get("header_row"))
    password = data.get("password") or None  # only used to open the file; never stored or logged
    info, rows = _inspect(path, mapping, header_row, password, token, filename)
    company = (data.get("company") or "").strip()
    suggestion = _suggest_bank(company, info["account_number"], filename, info["header_text"]) if company else None
    return jsonify(dict(info, token=token, filename=filename, fields=MAPPABLE_FIELDS,
                        protected=bool(password), bank_suggestion=suggestion, sample=rows[:PREVIEW_SAMPLE_ROWS]))


@bp.post("")
def create_import():
    """Create an import from a statement.

    multipart/form-data: company, bank_ledger, and either
      token (from /preview) + mapping + header_row  — the mapping confirmed in the dialog, or
      file [+ mapping]                              — direct upload with auto-detection.
    """
    company = require_company(request.form.get("company"))
    bank_ledger = (request.form.get("bank_ledger") or "").strip()

    ledger_names = _ledger_names(company)
    if not ledger_names:
        raise ApiError("Ledgers for '%s' have not been fetched from Tally yet. Fetch ledgers first." % company,
                       409, need_fetch=True)
    if bank_ledger not in ledger_names:
        raise ApiError("Select the bank ledger this statement belongs to.")

    mapping, header_row = _mapping_arg(request.form.get("mapping"), request.form.get("header_row"))
    password = request.form.get("password") or None
    if request.form.get("token"):
        path, filename = _upload_path(request.form["token"])
        info, rows = _inspect(path, mapping, header_row, password, request.form["token"], filename)
        if not rows:
            raise ApiError(info["problem"] or "No transactions found with this column mapping.", 422)
    else:
        upload = request.files.get("file")
        token, path = _save_upload(upload)
        filename = upload.filename
        try:
            rows = parse_statement(path, mapping, header_row, password)
        except PasswordRequired as exc:
            raise _password_needed(exc, token, filename)  # keep the file: retry with token + password
        except ParseError as exc:
            os.remove(path)
            raise ApiError(str(exc), 422, need_mapping=True, headers=exc.headers, preview=exc.preview)
        except Exception as exc:  # corrupt or password-protected files
            os.remove(path)
            raise ApiError("Could not read the file: %s" % exc, 422)

    rules = _rules(company)
    cash_bank = _cash_bank_names(company)
    by_lower = {n.lower(): n for n in ledger_names}
    unmatched = 0
    conn = db.get_db()
    with conn:
        cur = conn.execute("INSERT INTO imports (company, bank_ledger, filename) VALUES (?, ?, ?)",
                           (company, bank_ledger, filename))
        import_id = cur.lastrowid
        for row in rows:
            dup = conn.execute(
                "SELECT e.import_id FROM entries e JOIN imports i ON i.id = e.import_id "
                "WHERE i.company = ? AND i.bank_ledger = ? AND e.import_id != ? AND e.txn_date = ? "
                "AND e.debit = ? AND e.credit = ? AND e.narration = ? AND e.status != 'skipped' LIMIT 1",
                (company, bank_ledger, import_id, row["txn_date"], row["debit"], row["credit"],
                 row["narration"])).fetchone()
            # ledger from the statement's own column when it names a Tally ledger, else a suggestion
            given = row.get("ledger") or ""
            ledger = by_lower.get(given.lower(), "")
            note = None
            if given and not ledger:
                unmatched += 1
                note = "Ledger '%s' from the statement is not a ledger in Tally" % given
            if not ledger:
                ledger = ledger_matcher.suggest(row["narration"], ledger_names, rules)
            if dup:
                note = "Possible duplicate of an entry in import #%d" % dup[0]
            conn.execute(
                "INSERT INTO entries (import_id, txn_date, narration, ref_no, debit, credit, balance, ledger, "
                "voucher_type, status, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (import_id, row["txn_date"], row["narration"], row["ref_no"], row["debit"], row["credit"],
                 row["balance"], ledger, default_voucher_type(ledger, row["debit"], cash_bank),
                 "skipped" if dup else "pending", note))
    if request.form.get("token") and info.get("account_number"):
        # remember which bank ledger this account belongs to, for the next statement
        db.set_setting(_bank_account_key(company, info["account_number"]), bank_ledger)
    message = "Imported %d entries." % len(rows)
    if unmatched:
        message += " %d ledger name(s) from the statement were not found in Tally." % unmatched
    return jsonify({"import_id": import_id, "count": len(rows), "unmatched_ledgers": unmatched,
                    "message": message}), 201


# --------------------------------------------------------------------------- one import

@bp.get("/<int:import_id>")
def get_import(import_id):
    imp = _get_import(import_id)
    corrected = _sync_voucher_types(imp)
    if corrected:
        imp = _get_import(import_id)
    entries = db.query("SELECT * FROM entries WHERE import_id = ? ORDER BY txn_date, id", (import_id,))
    return jsonify({"import": imp, "entries": entries, "voucher_types_corrected": corrected})


@bp.delete("/<int:import_id>")
def delete_import(import_id):
    _get_import(import_id)
    db.execute("DELETE FROM imports WHERE id = ?", (import_id,))
    return jsonify({"message": "Import deleted."})


@bp.patch("/<int:import_id>/entries/<int:entry_id>")
def update_entry(import_id, entry_id):
    entry = _get_entry(import_id, entry_id)
    if entry["status"] == "pushed":
        raise ApiError("This entry is already in Tally and can no longer be edited.")
    data = body(request)
    changes = {k: data[k] for k in EDITABLE if k in data}
    for k in ("debit", "credit"):
        if k in changes:
            try:
                changes[k] = round(float(changes[k] or 0), 2)
            except (TypeError, ValueError):
                raise ApiError("%s must be a number." % k.title())
    if "ledger" in changes and "voucher_type" not in changes:
        # ledger changed without an explicit type: Contra for cash/bank ledgers, else Payment/Receipt
        imp = _get_import(import_id)
        debit = changes.get("debit", entry["debit"])
        changes["voucher_type"] = default_voucher_type(changes["ledger"], debit, _cash_bank_names(imp["company"]))
    if "txn_date" in changes:
        changes["txn_date"] = parse_date(changes["txn_date"]) or changes["txn_date"]
    if "narration" in changes:
        narration = re.sub(r"[ \t]+", " ", str(changes["narration"] or "")).strip()
        if len(narration) > MAX_NARRATION:
            raise ApiError("Narration is too long (%d characters, at most %d)." % (len(narration), MAX_NARRATION))
        changes["narration"] = narration
    status = data.get("status")
    if status == "skipped":
        changes.update(status="skipped", error=None)
    elif status == "pending" or (changes and set(changes) != {"narration"}):
        # an edit invalidates a previous validation; the narration alone does not affect it
        changes.update(status="pending", error=None)
    if changes:
        cols = ", ".join("%s = ?" % k for k in changes)
        db.execute("UPDATE entries SET %s WHERE id = ?" % cols, tuple(changes.values()) + (entry_id,))
    _refresh_status(import_id)
    return jsonify({"entry": _get_entry(import_id, entry_id)})


@bp.delete("/<int:import_id>/entries/<int:entry_id>")
def delete_entry(import_id, entry_id):
    entry = _get_entry(import_id, entry_id)
    if entry["status"] == "pushed":
        raise ApiError("This entry is already in Tally and cannot be deleted here.")
    db.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    _refresh_status(import_id)
    return jsonify({"message": "Entry deleted."})


@bp.post("/<int:import_id>/validate")
def validate(import_id):
    """Body: {entry_ids: [...], ledgers: {entry_id: ledger}, voucher_types: {entry_id: type}}.
    Saves the chosen ledgers, then marks each entry validated or records why it is not."""
    imp = _get_import(import_id)
    data = body(request)
    ids = [int(i) for i in data.get("entry_ids") or []]
    if not ids:
        raise ApiError("Select at least one entry to validate.")
    ledgers = {int(k): v for k, v in (data.get("ledgers") or {}).items()}
    vtypes = {int(k): v for k, v in (data.get("voucher_types") or {}).items()}
    ledger_names = set(_ledger_names(imp["company"]))
    cash_bank = _cash_bank_names(imp["company"])

    results = {"validated": 0, "invalid": 0}
    conn = db.get_db()
    with conn:
        for eid in ids:
            row = conn.execute("SELECT * FROM entries WHERE id = ? AND import_id = ?", (eid, import_id)).fetchone()
            if not row or row["status"] in ("pushed", "skipped"):
                continue
            entry = dict(row)
            if eid in ledgers:
                entry["ledger"] = (ledgers[eid] or "").strip()
                if eid not in vtypes:  # ledger changed, type not given: pick the matching type
                    entry["voucher_type"] = default_voucher_type(entry["ledger"], entry["debit"], cash_bank)
            if eid in vtypes:
                entry["voucher_type"] = vtypes[eid]
            problems = validate_entry(entry, ledger_names, imp["bank_ledger"], cash_bank)
            status = "invalid" if problems else "validated"
            conn.execute("UPDATE entries SET ledger = ?, voucher_type = ?, status = ?, error = ? WHERE id = ?",
                         (entry["ledger"], entry["voucher_type"],
                          "pending" if problems else "validated",
                          "; ".join(problems) if problems else None, eid))
            results[status] += 1
            if not problems:
                for kw, ledger in ledger_matcher.learn(entry["narration"], entry["ledger"]):
                    conn.execute("INSERT OR REPLACE INTO ledger_rules (company, keyword, ledger) VALUES (?, ?, ?)",
                                 (imp["company"], kw, ledger))
    _refresh_status(import_id)
    results["message"] = "%d validated, %d need attention." % (results["validated"], results["invalid"])
    return jsonify(results)


@bp.post("/<int:import_id>/push")
def push(import_id):
    """Push validated (and previously failed) entries to Tally as vouchers, one request per entry."""
    imp = _get_import(import_id)
    company = require_company(imp["company"])
    ids = [int(i) for i in body(request).get("entry_ids") or []]
    sql = "SELECT * FROM entries WHERE import_id = ? AND status IN ('validated', 'failed')"
    args = [import_id]
    if ids:
        sql += " AND id IN (%s)" % ",".join("?" * len(ids))
        args += ids
    entries = db.query(sql, args)
    if not entries:
        raise ApiError("No validated entries to push. Validate entries first.")

    client = tally()
    ledger_names = set(_ledger_names(company))
    cash_bank = _cash_bank_names(company)
    pushed = failed = 0
    for entry in entries:
        problems = validate_entry(entry, ledger_names, imp["bank_ledger"], cash_bank)
        if problems:
            db.execute("UPDATE entries SET status = 'pending', error = ? WHERE id = ?",
                       ("; ".join(problems), entry["id"]))
            failed += 1
            continue
        try:
            client.post_voucher(company, imp["bank_ledger"], entry)
            db.execute("UPDATE entries SET status = 'pushed', error = NULL, tally_response = ? WHERE id = ?",
                       (exchange(client)["tally_response"], entry["id"]))
            pushed += 1
        except TallyError as exc:
            db.execute("UPDATE entries SET status = 'failed', error = ?, tally_response = ? WHERE id = ?",
                       (str(exc), exchange(client)["tally_response"], entry["id"]))
            failed += 1
            if "Cannot reach Tally" in str(exc):
                break  # Tally went away; don't hammer it with the remaining entries
    status = _refresh_status(import_id)
    return jsonify(dict(exchange(client), pushed=pushed, failed=failed, status=status,
                        message="%d pushed to Tally (%s), %d failed." % (pushed, client.fmt.upper(), failed)))
