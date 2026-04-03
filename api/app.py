"""
Sentry WMS - Flask API Entry Point
"""

import os

from dotenv import load_dotenv
from flask import Flask
from flask_cors import CORS

load_dotenv()


def create_app():
    app = Flask(__name__)

    # Config
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
        "DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry"
    )
    app.config["JWT_SECRET"] = os.getenv("JWT_SECRET", "change-this-to-a-random-string")

    # CORS - allow mobile app and admin panel
    CORS(app)

    # Register blueprints
    from routes.auth import auth_bp
    from routes.lookup import lookup_bp

    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(lookup_bp, url_prefix="/api/lookup")

    @app.route("/api/health")
    def health():
        return {"status": "ok", "service": "sentry-wms"}

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("FLASK_PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
