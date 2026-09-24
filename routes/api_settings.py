"""Settings, Tally connectivity and dashboard stats."""
import json

from flask import Blueprint, jsonify, request

import db
from routes import ApiError, body, tally, tally_settings
from services.tally_client import FORMATS, TallyClient, TallyError

bp = Blueprint("api_settings", __name__, url_prefix="/api")


@bp.get("/settings")
def get_settings():
    host, port, fmt = tally_settings()
    return jsonify({"host": host, "port": port, "format": fmt})


def _validate(data):
    host = str(data.get("host") or "").strip()
    try:
        port = int(data.get("port"))
    except (TypeError, ValueError):
        raise ApiError("Port must be a number.")
    if not host:
        raise ApiError("Host is required.")
    if not 1 <= port <= 65535:
        raise ApiError("Port must be between 1 and 65535.")
    fmt = str(data.get("format") or tally_settings()[2]).lower()
    if fmt not in FORMATS:
        raise ApiError("Response format must be JSON or XML.")
    return host, port, fmt


@bp.post("/settings")
def save_settings():
    host, port, fmt = _validate(body(request))
    db.set_setting("tally_host", host)
    db.set_setting("tally_port", port)
    db.set_setting("tally_format", fmt)
    return jsonify({"host": host, "port": port, "format": fmt, "message": "Settings saved."})


@bp.post("/settings/format")
def save_format():
    """Save only the response format, so every page switches format immediately."""
    fmt = str(body(request).get("format") or "").lower()
    if fmt not in FORMATS:
        raise ApiError("Response format must be JSON or XML.")
    db.set_setting("tally_format", fmt)
    return jsonify({"format": fmt, "message": "Tally API format set to %s." % fmt.upper()})


@bp.post("/tally/test")
def test_connection():
    """Test host/port/format (unsaved values from the settings form) and list the open companies."""
    host, port, fmt = _validate(body(request))
    client = TallyClient(host, port, fmt=fmt)
    try:
        message = client.ping()
    except TallyError as exc:
        return jsonify({"ok": False, "error": str(exc)})
    try:
        companies = client.list_companies()
        company_error = None
    except TallyError as exc:
        companies, company_error = [], str(exc)
    return jsonify({"ok": True, "message": message, "format": fmt, "companies": companies,
                    "company_error": company_error})


@bp.get("/tally/status")
def status():
    host, port, fmt = tally_settings()
    try:
        message = tally().ping()
        return jsonify({"ok": True, "host": host, "port": port, "format": fmt, "message": message})
    except TallyError as exc:
        return jsonify({"ok": False, "host": host, "port": port, "format": fmt, "error": str(exc)})


@bp.get("/tally/companies")
def companies():
    try:
        return jsonify({"companies": tally().list_companies()})
    except TallyError as exc:
        raise ApiError(str(exc), 502)


@bp.post("/tally/raw")
def raw_request():
    """API console: send a raw request to Tally and return the raw response text.

    JSON: {"format": "json", "headers": {...}, "payload": {...}}
    XML:  {"format": "xml", "xml": "<ENVELOPE>...</ENVELOPE>"}
    """
    data = body(request)
    fmt = str(data.get("format") or "json").lower()
    if fmt not in FORMATS:
        raise ApiError("Format must be JSON or XML.")
    client = tally(fmt)
    try:
        if fmt == "xml":
            xml = str(data.get("xml") or "").strip()
            if not xml:
                raise ApiError("Enter an XML request.")
            raw, _parsed = client.post_raw(xml)
        else:
            headers = data.get("headers") or {}
            payload = data.get("payload") or {}
            if not isinstance(headers, dict) or not isinstance(payload, dict):
                raise ApiError("Headers and payload must be JSON objects.")
            raw, parsed = client.post_raw(json.dumps(payload), {str(k): str(v) for k, v in headers.items()})
            if parsed is not None:
                raw = json.dumps(parsed, indent=2)
    except TallyError as exc:
        raise ApiError(str(exc), 502)
    return jsonify({"format": fmt, "raw": raw})


@bp.get("/dashboard")
def dashboard():
    counts = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) n FROM entries GROUP BY status")}
    ledgers = db.query("SELECT company, COUNT(*) n, MAX(fetched_at) fetched_at FROM ledgers "
                       "GROUP BY company ORDER BY company")
    recent = db.query(
        "SELECT i.*, COUNT(e.id) total, "
        "SUM(e.status = 'pushed') pushed, SUM(e.status = 'failed') failed, "
        "SUM(e.debit) total_debit, SUM(e.credit) total_credit "
        "FROM imports i LEFT JOIN entries e ON e.import_id = i.id "
        "GROUP BY i.id ORDER BY i.id DESC LIMIT 10")
    imports_count = db.query("SELECT COUNT(*) n FROM imports", one=True)["n"]
    return jsonify({
        "entries": {s: counts.get(s, 0) for s in ("pending", "validated", "pushed", "failed", "skipped")},
        "ledgers": ledgers,
        "imports_count": imports_count,
        "recent_imports": recent,
    })
