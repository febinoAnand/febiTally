import os

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

import config
import db
from routes import ApiError, api_imports, api_ledgers, api_settings, pages


def create_app(overrides=None):
    app = Flask(__name__)
    app.config.update(
        DB_PATH=config.DB_PATH,
        UPLOAD_DIR=config.UPLOAD_DIR,
        ALLOWED_EXTENSIONS=config.ALLOWED_EXTENSIONS,
        MAX_CONTENT_LENGTH=config.MAX_UPLOAD_MB * 1024 * 1024,
        DEFAULT_TALLY_HOST=config.DEFAULT_TALLY_HOST,
        DEFAULT_TALLY_PORT=config.DEFAULT_TALLY_PORT,
        DEFAULT_TALLY_FORMAT=config.DEFAULT_TALLY_FORMAT,
        TALLY_TIMEOUT=config.TALLY_TIMEOUT,
    )
    app.config.update(overrides or {})

    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
    db.init_db(app.config["DB_PATH"])
    app.teardown_appcontext(db.close_db)

    for module in (pages, api_settings, api_ledgers, api_imports):
        app.register_blueprint(module.bp)

    @app.errorhandler(ApiError)
    def handle_api_error(exc):
        return jsonify(dict(exc.extra, error=str(exc))), exc.status

    @app.errorhandler(HTTPException)
    def handle_http_error(exc):
        if request.path.startswith("/api/"):
            return jsonify({"error": exc.description}), exc.code
        return exc

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=True)
