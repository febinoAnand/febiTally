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
from services.statement_parser import ParseError, inspect_statement, parse_date, parse_statement
from services.tally_client import TallyError

bp = Blueprint("api_imports", __name__, url_prefix="/api/imports")

VOUCHER_TYPES = ("Payment", "Receipt")
MAPPABLE_FIELDS = ["txn_date", "narration", "ref_no", "debit", "credit", "balance", "amount", "drcr", "ledger"]
PREVIEW_SAMPLE_ROWS = 15
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


def validate_entry(entry, ledger_names, bank_ledger):
    """Return a list of problems with an entry (empty list = valid)."""
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
    expected = "Payment" if debit > 0 else "Receipt"
    if entry.get("voucher_type") not in VOUCHER_TYPES:
        problems.append("voucher type must be Payment or Receipt")
    elif debit or credit:
        if entry["voucher_type"] != expected:
            problems.append("%s voucher does not match a %s" %
                            (entry["voucher_type"], "withdrawal" if debit > 0 else "deposit"))
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


def _inspect(path, mapping, header_row):
    try:
        return inspect_statement(path, mapping, header_row)
    except Exception as exc:  # corrupt or password-protected files
        raise ApiError("Could not read the file: %s" % exc, 422)


@bp.post("/preview")
def preview_import():
    """Upload (or re-inspect) a statement for the column-mapping dialog. Nothing is imported.

    multipart: file  — or form/JSON: token; optional mapping (JSON {field: column}), header_row.
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
    info, rows = _inspect(path, mapping, header_row)
    return jsonify(dict(info, token=token, filename=filename, fields=MAPPABLE_FIELDS,
                        sample=rows[:PREVIEW_SAMPLE_ROWS]))


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
    if request.form.get("token"):
        path, filename = _upload_path(request.form["token"])
        info, rows = _inspect(path, mapping, header_row)
        if not rows:
            raise ApiError(info["problem"] or "No transactions found with this column mapping.", 422)
    else:
        upload = request.files.get("file")
        _token, path = _save_upload(upload)
        filename = upload.filename
        try:
            rows = parse_statement(path, mapping, header_row)
        except ParseError as exc:
            os.remove(path)
            raise ApiError(str(exc), 422, need_mapping=True, headers=exc.headers, preview=exc.preview)
        except Exception as exc:  # corrupt or password-protected files
            os.remove(path)
            raise ApiError("Could not read the file: %s" % exc, 422)

    rules = _rules(company)
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
                 row["balance"], ledger, "Payment" if row["debit"] > 0 else "Receipt",
                 "skipped" if dup else "pending", note))
    message = "Imported %d entries." % len(rows)
    if unmatched:
        message += " %d ledger name(s) from the statement were not found in Tally." % unmatched
    return jsonify({"import_id": import_id, "count": len(rows), "unmatched_ledgers": unmatched,
                    "message": message}), 201


# --------------------------------------------------------------------------- one import

@bp.get("/<int:import_id>")
def get_import(import_id):
    imp = _get_import(import_id)
    entries = db.query("SELECT * FROM entries WHERE import_id = ? ORDER BY txn_date, id", (import_id,))
    return jsonify({"import": imp, "entries": entries})


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
    if "txn_date" in changes:
        changes["txn_date"] = parse_date(changes["txn_date"]) or changes["txn_date"]
    status = data.get("status")
    if status == "skipped":
        changes.update(status="skipped", error=None)
    elif status == "pending" or changes:
        # any edit invalidates a previous validation
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
            if eid in vtypes:
                entry["voucher_type"] = vtypes[eid]
            problems = validate_entry(entry, ledger_names, imp["bank_ledger"])
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
    pushed = failed = 0
    for entry in entries:
        problems = validate_entry(entry, ledger_names, imp["bank_ledger"])
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
