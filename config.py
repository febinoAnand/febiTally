import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_PATH = os.environ.get("FEBITALLY_DB", os.path.join(BASE_DIR, "data", "febitally.db"))
UPLOAD_DIR = os.environ.get("FEBITALLY_UPLOADS", os.path.join(BASE_DIR, "uploads"))
MAX_UPLOAD_MB = 20
ALLOWED_EXTENSIONS = {".pdf", ".xlsx", ".xls", ".csv"}

DEFAULT_TALLY_HOST = "localhost"
DEFAULT_TALLY_PORT = 9000
DEFAULT_TALLY_FORMAT = "json"  # "json" (Tally Prime 7+) or "xml" (all releases)
TALLY_TIMEOUT = 15
