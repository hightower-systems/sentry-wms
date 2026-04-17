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
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB request body limit
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is required")
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url

    jwt_secret = os.getenv("JWT_SECRET")
    if not jwt_secret:
        raise RuntimeError("JWT_SECRET environment variable is required")
    app.config["JWT_SECRET"] = jwt_secret

    # CORS - restrict to known origins, configurable via env var
    cors_origins = os.getenv(
        "CORS_ORIGINS",
        "http://localhost:3000,http://localhost:5000,http://localhost:8081",
    ).split(",")
    resolved_origins = [o.strip() for o in cors_origins]
    CORS(app, origins=resolved_origins)

    # Security response headers
    csp_policy = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "object-src 'none'"
    )

    @app.after_request
    def set_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "0"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = csp_policy
        return response

    # Prevent stack trace leakage in production
    @app.errorhandler(500)
    def internal_error(e):
        return {"error": "Internal server error"}, 500

    # Register blueprints
    from routes.auth import auth_bp
    from routes.lookup import lookup_bp
    from routes.receiving import receiving_bp
    from routes.putaway import putaway_bp
    from routes.picking import picking_bp
    from routes.packing import packing_bp
    from routes.shipping import shipping_bp
    from routes.inventory import inventory_bp
    from routes.transfers import transfers_bp
    from routes.admin import admin_bp
    from routes.warehouses import warehouses_bp

    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(lookup_bp, url_prefix="/api/lookup")
    app.register_blueprint(receiving_bp, url_prefix="/api/receiving")
    app.register_blueprint(putaway_bp, url_prefix="/api/putaway")
    app.register_blueprint(picking_bp, url_prefix="/api/picking")
    app.register_blueprint(packing_bp, url_prefix="/api/packing")
    app.register_blueprint(shipping_bp, url_prefix="/api/shipping")
    app.register_blueprint(inventory_bp, url_prefix="/api/inventory")
    app.register_blueprint(transfers_bp, url_prefix="/api/transfers")
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
    app.register_blueprint(warehouses_bp, url_prefix="/api/warehouses")

    # Import connector modules so they auto-register with the registry
    import connectors.example  # noqa: F401

    @app.route("/api/health")
    def health():
        return {"status": "ok", "service": "sentry-wms"}

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("FLASK_PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
