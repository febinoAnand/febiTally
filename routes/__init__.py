"""Shared helpers for the API blueprints."""
from flask import current_app

import db
from services.tally_client import TallyClient, TallyError


class ApiError(Exception):
    """An error returned to the browser as {"error": message, ...extra} with an HTTP status."""

    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def tally_settings():
    host = db.get_setting("tally_host", current_app.config["DEFAULT_TALLY_HOST"])
    port = db.get_setting("tally_port", current_app.config["DEFAULT_TALLY_PORT"])
    fmt = db.get_setting("tally_format", current_app.config["DEFAULT_TALLY_FORMAT"])
    return host, int(port), fmt


def tally(fmt=None):
    host, port, saved_fmt = tally_settings()
    return TallyClient(host, port, timeout=current_app.config["TALLY_TIMEOUT"], fmt=fmt or saved_fmt)


MAX_RESPONSE_CHARS = 200000


def exchange(client):
    """Format + raw Tally response of the client's last call, for display in the UI."""
    raw = client.last_raw or ""
    if len(raw) > MAX_RESPONSE_CHARS:
        raw = raw[:MAX_RESPONSE_CHARS] + "\n… (truncated, %d characters in total)" % len(client.last_raw)
    return {"format": client.fmt, "tally_response": raw}


def call_tally(client, fn, *args, **kwargs):
    """Run a TallyClient method; on failure raise an ApiError that carries Tally's raw response."""
    try:
        return fn(*args, **kwargs)
    except TallyError as exc:
        raise ApiError(str(exc), 502, **exchange(client))


def require_company(company):
    """Check that the company is open in Tally. Returns the exact name as Tally reports it."""
    company = (company or "").strip()
    if not company:
        raise ApiError("Select a company.")
    client = tally()
    companies = call_tally(client, client.list_companies)
    for name in companies:
        if name.lower() == company.lower():
            return name
    raise ApiError("Company '%s' is not open in Tally Prime. Open companies: %s"
                   % (company, ", ".join(companies) or "none"), 400)


def body(request):
    return request.get_json(silent=True) or {}
