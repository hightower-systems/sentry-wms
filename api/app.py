"""
Sentry WMS - Flask API Entry Point
"""

import logging
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, g, request
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()

logger = logging.getLogger(__name__)


def check_build_version(build_file_path="/app/BUILD_VERSION"):
    """v1.4.2 #73: detect upgrade-without-rebuild.

    The Dockerfile writes the source `__version__` into /app/BUILD_VERSION
    at image build time. If a later `git pull` bumps the code version but
    the operator skips `docker compose build`, the container runs the old
    image (with old dependencies) against the new code. Fail fast with a
    clear message rather than letting a ModuleNotFoundError crash a worker.
    """
    from version import __version__ as code_version

    build_file = Path(build_file_path)
    if not build_file.exists():
        logger.warning(
            "No %s found. Skipping version check. "
            "Expected in development, may indicate stale image in production.",
            build_file_path,
        )
        return

    build_version = build_file.read_text().strip()
    if build_version != code_version:
        logger.critical(
            "Docker image version (%s) does not match code version (%s). "
            "This means you upgraded the code without rebuilding the Docker image. "
            "Run: docker compose down && docker compose build && docker compose up -d",
            build_version,
            code_version,
        )
        sys.exit(2)


def create_app():
    check_build_version()

    app = Flask(__name__)

    # #107: honour X-Forwarded-* headers from a trusted reverse proxy so
    # request.scheme / request.host / request.is_secure reflect what the
    # browser sees, not the Flask <- proxy hop. Without this, behind an
    # HTTPS-terminating nginx / Caddy / Traefik / ALB, cookies get scoped
    # to the internal 127.0.0.1 host instead of the public hostname, the
    # browser never resubmits them, and every CSRF-protected request 403s.
    #
    # Gated behind TRUST_PROXY because honouring these headers when NOT
    # behind a trusted proxy lets any client forge its own scheme / host /
    # IP (a well-known ProxyFix footgun). Opt-in only; operator confirms
    # via docs/deployment.md that the app sits on a network the proxy
    # controls before setting TRUST_PROXY=true.
    proxy_fix_active = os.getenv("TRUST_PROXY", "").lower() in ("true", "1", "yes")
    if proxy_fix_active:
        # One proxy hop is the standard nginx / Caddy / Traefik / ALB
        # shape. Deployments that terminate TLS at multiple proxies in
        # front of Sentry (e.g. CDN -> nginx -> Sentry) increase the
        # x_for / x_proto / x_host counts accordingly.
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=1,
            x_proto=1,
            x_host=1,
            x_prefix=0,
        )
    # #136: emit the ProxyFix state at startup so operators can verify
    # activation via `docker compose logs api | grep ProxyFix` without
    # having to inspect app internals. The line fires for both states --
    # "active" confirms the wiring reached the container, "inactive"
    # confirms the default-off posture. Logged via the module logger at
    # WARNING level to match the check_build_version() pattern: this is
    # load-bearing security state and needs to clear the default gunicorn
    # stderr threshold, not be filtered at INFO.
    if proxy_fix_active:
        logger.warning(
            "ProxyFix active: trusting X-Forwarded-* headers (TRUST_PROXY=true)"
        )
    else:
        logger.warning(
            "ProxyFix inactive: not trusting proxy headers (TRUST_PROXY not set)"
        )
    app.config["PROXY_FIX_ACTIVE"] = proxy_fix_active

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

    # v1.5.0 #128: SENTRY_TOKEN_PEPPER is concatenated with every
    # inbound X-WMS-Token plaintext before the SHA-256 hash step
    # (Decision Q). Boot fails without it rather than silently falling
    # back to an empty pepper; a token hash computed with an empty
    # pepper differs from every hash stored by a correctly-configured
    # deployment, which would look like a blanket auth failure rather
    # than a config problem.
    if not os.getenv("SENTRY_TOKEN_PEPPER"):
        raise RuntimeError(
            "SENTRY_TOKEN_PEPPER environment variable is required for "
            "X-WMS-Token auth (v1.5.0). See .env.example for details."
        )

    # CORS - restrict to known origins, configurable via env var
    cors_origins = os.getenv(
        "CORS_ORIGINS",
        "http://localhost:3000,http://localhost:5000,http://localhost:8081",
    ).split(",")
    resolved_origins = [o.strip() for o in cors_origins]
    # V-045: credentials must cross CORS for the admin SPA's HttpOnly cookie.
    # Origins stay restricted (no wildcard), which is required for cookie auth.
    CORS(app, origins=resolved_origins, supports_credentials=True)

    # V-041: rate limiting. Default 300/min per authenticated user (or per IP
    # if unauthenticated); sensitive routes override via @limiter.limit(...).
    from services.rate_limit import init_limiter
    init_limiter(app)

    # Security response headers
    # V-110: fonts are now self-hosted under admin/public/fonts and
    # served by the admin nginx container. Neither style-src nor
    # font-src carry a Google origin, so the admin panel has no
    # third-party asset dependency and a successful XSS cannot load
    # an attacker-controlled stylesheet or font from any origin.
    csp_policy = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "object-src 'none'"
    )

    @app.before_request
    def _mint_source_txn_id():
        # v1.5.0 plan section 1.5: every emit_event call within a single
        # HTTP request reuses one source_txn_id so a retried request
        # collapses to one row via the integration_events idempotency
        # key. Prefer an inbound X-Request-ID header when it parses as a
        # UUID (supports distributed tracing across services); otherwise
        # mint a fresh one.
        inbound = request.headers.get("X-Request-ID", "").strip()
        if inbound:
            try:
                g.source_txn_id = uuid.UUID(inbound)
            except (ValueError, TypeError):
                g.source_txn_id = uuid.uuid4()
        else:
            g.source_txn_id = uuid.uuid4()

    @app.after_request
    def set_security_headers(response):
        from flask import request as _request
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "0"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = csp_policy
        # V-051: HSTS only when the request was HTTPS-terminated. Setting it
        # on plain HTTP would force browsers to refuse future HTTP connections
        # to this host, which breaks warehouse-LAN deployments that run over
        # HTTP (see V-048 accepted risk).
        is_https = _request.is_secure or _request.headers.get("X-Forwarded-Proto") == "https"
        if is_https:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
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
    from routes.polling import polling_bp
    from routes.snapshot import snapshot_bp

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
    # v1.5.0 #122: first /api/v1/* surface. Gated by @require_wms_token
    # per route; cookie-auth users do not see this surface.
    app.register_blueprint(polling_bp, url_prefix="/api/v1/events")
    # v1.5.0 #133: bulk snapshot paging. Shares the same
    # @require_wms_token surface as polling, distinct 2/min rate limit.
    app.register_blueprint(snapshot_bp, url_prefix="/api/v1/snapshot")

    # Import connector modules so they auto-register with the registry
    import connectors.example  # noqa: F401

    # v1.5.0: load the v1.5.0 event-schema registry eagerly so a malformed
    # api/schemas_v1/events/*/*.json file or a catalog entry without a
    # matching schema fails boot loudly, not lazily on the first emit.
    import services.events_schema_registry  # noqa: F401

    @app.route("/api/health")
    def health():
        # #136: expose ProxyFix state so operators can verify the
        # TRUST_PROXY wiring reached the container, without reading logs
        # or execing into the container. Curl-friendly from the proxy.
        return {
            "status": "ok",
            "service": "sentry-wms",
            "proxy_fix_active": app.config.get("PROXY_FIX_ACTIVE", False),
        }

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("FLASK_PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
