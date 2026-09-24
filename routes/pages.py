"""HTML pages. Each page is a static template; data is loaded by its script from the JSON API."""
from flask import Blueprint, render_template

bp = Blueprint("pages", __name__)


@bp.get("/")
def dashboard():
    return render_template("dashboard.html", active="dashboard")


@bp.get("/import")
def import_page():
    return render_template("import.html", active="import")


@bp.get("/ledgers")
def ledgers():
    return render_template("ledgers.html", active="ledgers")


@bp.get("/settings")
def settings():
    return render_template("settings.html", active="settings")
