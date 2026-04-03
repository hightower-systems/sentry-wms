"""
Sentry WMS - Flask API Entry Point
"""

import os
from flask import Flask
from flask_cors import CORS


def create_app():
    app = Flask(__name__)

    # Config
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
        "DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry"
    )
    app.config["JWT_SECRET"] = os.getenv("JWT_SECRET", "change-this-to-a-random-string")

    # CORS - allow mobile app and admin panel
    CORS(app)

    # Register blueprints (uncomment as built)
    # from routes.auth import auth_bp
    # from routes.lookup import lookup_bp
    # from routes.receiving import receiving_bp
    # from routes.putaway import putaway_bp
    # from routes.picking import picking_bp
    # from routes.packing import packing_bp
    # from routes.shipping import shipping_bp
    # from routes.inventory import inventory_bp
    # from routes.transfers import transfers_bp
    # from routes.admin import admin_bp

    # app.register_blueprint(auth_bp, url_prefix="/api/auth")
    # app.register_blueprint(lookup_bp, url_prefix="/api/lookup")
    # app.register_blueprint(receiving_bp, url_prefix="/api/receiving")
    # app.register_blueprint(putaway_bp, url_prefix="/api/putaway")
    # app.register_blueprint(picking_bp, url_prefix="/api/picking")
    # app.register_blueprint(packing_bp, url_prefix="/api/packing")
    # app.register_blueprint(shipping_bp, url_prefix="/api/shipping")
    # app.register_blueprint(inventory_bp, url_prefix="/api/inventory")
    # app.register_blueprint(transfers_bp, url_prefix="/api/transfers")
    # app.register_blueprint(admin_bp, url_prefix="/api/admin")

    @app.route("/api/health")
    def health():
        return {"status": "ok", "service": "sentry-wms"}

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("FLASK_PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
