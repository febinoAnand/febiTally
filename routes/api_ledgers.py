"""Ledger cache and ledger CRUD. Every write goes to Tally first; the cache is updated only on success."""
from flask import Blueprint, jsonify, request

import db
from routes import ApiError, body, call_tally, exchange, require_company, tally

bp = Blueprint("api_ledgers", __name__, url_prefix="/api/ledgers")


def _get_ledger(ledger_id):
    row = db.query("SELECT * FROM ledgers WHERE id = ?", (ledger_id,), one=True)
    if not row:
        raise ApiError("Ledger not found.", 404)
    return row


def _parse_form(data):
    name = str(data.get("name") or "").strip()
    parent = str(data.get("parent") or "").strip()
    if not name:
        raise ApiError("Ledger name is required.")
    if not parent:
        raise ApiError("Parent group is required.")
    try:
        opening = float(data.get("opening_balance") or 0)
    except (TypeError, ValueError):
        raise ApiError("Opening balance must be a number.")
    return name, parent, opening


@bp.get("")
def list_ledgers():
    company = request.args.get("company", "")
    rows = db.query("SELECT * FROM ledgers WHERE company = ? ORDER BY name COLLATE NOCASE", (company,))
    return jsonify({"company": company, "ledgers": rows})


@bp.get("/companies")
def cached_companies():
    return jsonify({"companies": db.query(
        "SELECT company, COUNT(*) n, MAX(fetched_at) fetched_at FROM ledgers GROUP BY company ORDER BY company")})


@bp.get("/groups")
def groups():
    company = require_company(request.args.get("company"))
    client = tally()
    return jsonify({"groups": call_tally(client, client.list_groups, company)})


@bp.post("/fetch")
def fetch():
    """Pull every ledger of the company from Tally and replace the cache with it."""
    company = require_company(body(request).get("company"))
    client = tally()
    ledgers = call_tally(client, client.list_ledgers, company)
    conn = db.get_db()
    with conn:
        conn.execute("DELETE FROM ledgers WHERE company = ?", (company,))
        conn.executemany(
            "INSERT OR REPLACE INTO ledgers (company, name, parent, opening_balance, closing_balance, guid, "
            "fetched_at) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            [(company, l["name"], l["parent"], l["opening_balance"], l["closing_balance"], l["guid"])
             for l in ledgers if l["name"]])
    return jsonify(dict(exchange(client), company=company, count=len(ledgers),
                        message="Fetched %d ledgers from Tally (%s)." % (len(ledgers), client.fmt.upper())))


@bp.post("")
def create():
    data = body(request)
    company = require_company(data.get("company"))
    name, parent, opening = _parse_form(data)
    if db.query("SELECT 1 FROM ledgers WHERE company = ? AND name = ?", (company, name), one=True):
        raise ApiError("Ledger '%s' already exists." % name)
    client = tally()
    call_tally(client, client.create_ledger, company, name, parent, opening)
    ledger_id = db.execute(
        "INSERT INTO ledgers (company, name, parent, opening_balance, closing_balance) VALUES (?, ?, ?, ?, ?)",
        (company, name, parent, opening, opening))
    return jsonify(dict(exchange(client), ledger=_get_ledger(ledger_id),
                        message="Ledger created in Tally (%s)." % client.fmt.upper())), 201


@bp.put("/<int:ledger_id>")
def update(ledger_id):
    current = _get_ledger(ledger_id)
    company = require_company(current["company"])
    name, parent, opening = _parse_form(body(request))
    if name != current["name"] and db.query(
            "SELECT 1 FROM ledgers WHERE company = ? AND name = ?", (company, name), one=True):
        raise ApiError("Ledger '%s' already exists." % name)
    client = tally()
    call_tally(client, client.alter_ledger, company, current["name"], parent, opening,
               new_name=name if name != current["name"] else None)
    db.execute("UPDATE ledgers SET name = ?, parent = ?, opening_balance = ? WHERE id = ?",
               (name, parent, opening, ledger_id))
    if name != current["name"]:
        # keep pending statement entries and learned rules pointing at the renamed ledger
        db.execute("UPDATE entries SET ledger = ? WHERE ledger = ? AND status != 'pushed' AND import_id IN "
                   "(SELECT id FROM imports WHERE company = ?)", (name, current["name"], company))
        db.execute("UPDATE ledger_rules SET ledger = ? WHERE ledger = ? AND company = ?",
                   (name, current["name"], company))
    return jsonify(dict(exchange(client), ledger=_get_ledger(ledger_id),
                        message="Ledger updated in Tally (%s)." % client.fmt.upper()))


@bp.delete("/<int:ledger_id>")
def delete(ledger_id):
    current = _get_ledger(ledger_id)
    company = require_company(current["company"])
    client = tally()
    call_tally(client, client.delete_ledger, company, current["name"])
    db.execute("DELETE FROM ledgers WHERE id = ?", (ledger_id,))
    db.execute("DELETE FROM ledger_rules WHERE company = ? AND ledger = ?", (company, current["name"]))
    return jsonify(dict(exchange(client),
                        message="Ledger '%s' deleted from Tally (%s)." % (current["name"], client.fmt.upper())))
