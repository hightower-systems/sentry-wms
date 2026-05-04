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

    # v1.5.1 V-206 (#147): TRUST_PROXY=true means the api honours
    # X-Forwarded-* from the nearest hop. API_BIND_HOST=0.0.0.0 means
    # the container port is reachable on every host interface. Both
    # together let an attacker who reaches port 5000 directly (cloud
    # misconfig, Security Group error, bastion misroute) spoof
    # X-Forwarded-For and poison every rate-limit bucket, audit-log
    # user_id attribution, and any downstream IP-based allowlist.
    # Refuse boot unless the operator explicitly opts in via
    # SENTRY_ALLOW_OPEN_BIND=1 (documented escape hatch for deployments
    # that do the network-level protection themselves).
    api_bind_host = os.getenv("API_BIND_HOST", "").strip()
    allow_open_bind = os.getenv("SENTRY_ALLOW_OPEN_BIND", "").lower() in (
        "true", "1", "yes"
    )
    if proxy_fix_active and api_bind_host == "0.0.0.0":
        message = (
            "Unsafe deployment: TRUST_PROXY=true combined with "
            "API_BIND_HOST=0.0.0.0 exposes the api to X-Forwarded-For "
            "spoofing from any client that can reach port 5000. Set "
            "API_BIND_HOST=127.0.0.1 and put a reverse proxy in front, "
            "or set TRUST_PROXY=false if the api is directly reachable. "
            "If this combination is intentional (network-level "
            "protection applied elsewhere), set SENTRY_ALLOW_OPEN_BIND=1 "
            "to acknowledge the risk and continue. See V-206 in "
            "docs/deployment.md."
        )
        if not allow_open_bind:
            raise RuntimeError(message)
        logger.critical("ACK via SENTRY_ALLOW_OPEN_BIND=1: %s", message)

    if proxy_fix_active:
        # One proxy hop is the standard nginx / Caddy / Traefik / ALB
        # shape. Deployments that terminate TLS at multiple proxies in
        # front of Sentry (e.g. CDN -> nginx -> Sentry) increase the
        # x_for / x_proto / x_host counts accordingly.
        #
        # v1.5.1 V-219 (umbrella #156): x_prefix=0 is the intentional
        # value and takes precedence over the audit plan's
        # x_prefix=1. Honouring X-Forwarded-Prefix makes sense only
        # for sub-path deploys like `/app` behind a single origin
        # serving many apps, which Sentry does not support today.
        # Turning x_prefix on without that shape gives a remote
        # caller (via a trusted proxy) the ability to steer
        # request.script_root without a functional benefit. The
        # defensible default for our deployment matrix is 0; if a
        # future release adds sub-path support the correct change
        # is to flip to x_prefix=1 AND gate behind an explicit
        # config flag so operators on the old shape do not
        # inherit the broader trust.
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
    #
    # v1.5.1 V-201 (#142): the guard now rejects weak values too
    # (short, whitespace-only, placeholder). Centralised in
    # middleware.auth_middleware.validate_pepper_config so the boot
    # check, the request-time hasher, and the admin issuance hasher
    # all apply the same rule.
    from middleware.auth_middleware import validate_pepper_config
    validate_pepper_config(os.getenv("SENTRY_TOKEN_PEPPER"))

    # #238: pre-#238, validate_or_die() ran ONLY in the dispatcher
    # container (WebhookDispatcher.run). The api container reads
    # the same dispatcher env vars (DISPATCHER_MAX_PENDING_HARD_CAP,
    # DISPATCHER_MAX_DLQ_HARD_CAP, DISPATCHER_REPLAY_BATCH_HARD_CAP,
    # SENTRY_PUBSUB_HMAC_KEY, etc.) for admin-endpoint enforcement
    # and for the cross-worker pubsub publisher, but a typo'd or
    # out-of-range value never tripped a boot guard here. The two
    # containers could disagree silently on the cap; the api would
    # boot and fall back to defaults while the dispatcher refused
    # to start. Run the same validator on the api boot path so
    # both containers fail loudly with the same range messages.
    # Idempotent: each helper re-reads from os.environ, so running
    # it twice (api + dispatcher) is safe.
    from services.webhook_dispatcher import env_validator as _dispatcher_env
    _dispatcher_env.validate_or_die()

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
    from services.rate_limit import init_limiter, _resolve_storage_uri
    init_limiter(app)

    # v1.5.1 V-205 (#146): wire up Redis pubsub for cross-worker
    # token-cache invalidation. Same broker shape rate_limit resolves,
    # different Redis DB (0, where celery lives). A deployment without
    # Redis falls back to the 60s TTL revocation path documented at
    # token_cache module top. Idempotent; safe on reloads.
    from services import token_cache
    _broker_url = os.getenv("CELERY_BROKER_URL", "")
    if _broker_url.startswith(("redis://", "rediss://")):
        token_cache.start_invalidation_subscriber(_broker_url)
    else:
        token_cache.start_invalidation_subscriber(None)

    # Security response headers
    # V-110: fonts are now self-hosted under admin/public/fonts and
    # served by the admin nginx container. Neither style-src nor
    # font-src carry a Google origin, so the admin panel has no
    # third-party asset dependency and a successful XSS cannot load
    # an attacker-controlled stylesheet or font from any origin.
    # v1.5.1 V-109 (#54): report-uri points browsers at the
    # /api/csp-report endpoint below so CSP violations leave a trail
    # operators can actually find. Without it a successful XSS
    # probe was silently blocked AND silently unnoticed; the server
    # only saw a normal request. report-uri is the widely-compatible
    # directive (deprecated but universal); report-to is the modern
    # API and is deferred until we plumb the Reporting-Endpoints
    # header too.
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
        "object-src 'none'; "
        "report-uri /api/csp-report"
    )

    @app.before_request
    def _mint_source_txn_id():
        # v1.5.0 plan section 1.5: every emit_event call within a single
        # HTTP request reuses one source_txn_id so a retried request
        # collapses to one row via the integration_events idempotency
        # key. Prefer an inbound X-Request-ID header when it parses as a
        # UUID (supports distributed tracing across services); otherwise
        # mint a fresh one.
        #
        # v1.5.1 V-211 (#155) -- contract: source_txn_id is a
        # Sentry-internal idempotency key exposed on the wire for
        # tracing, NOT a safe consumer dedupe key. Authenticated
        # callers can set it to an arbitrary UUID via X-Request-ID;
        # consumers MUST dedupe on event_id (server-side BIGSERIAL)
        # instead. Documented in docs/events/README.md. The
        # passthrough is retained for the tracing use case; the
        # attacker-controlled surface is accepted as low-impact so
        # long as the consumer contract is followed.
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
        # v1.5.1 V-215 (umbrella #156): /api/health is reachable
        # unauthenticated (Docker healthcheck, upstream monitors) so
        # the response MUST NOT include state that helps an attacker
        # shape their approach. proxy_fix_active moved to the
        # authenticated /api/admin/system-info endpoint below.
        return {"status": "ok", "service": "sentry-wms"}

    # v1.5.1 V-215: admin-only surface for operator-useful state
    # that was previously on /api/health. Registered on the app
    # directly (not the admin blueprint) because register_blueprint
    # has already run by this point in create_app; adding a route
    # to admin_bp after registration does not propagate to the
    # app's URL map. Auth is enforced via the standard decorators
    # so the wiring matches every other admin-only endpoint.
    from middleware.auth_middleware import require_auth as _require_auth
    from middleware.auth_middleware import require_role as _require_role
    from flask import jsonify as _jsonify

    @app.route("/api/admin/system-info", methods=["GET"])
    @_require_auth
    @_require_role("ADMIN")
    def system_info():
        return _jsonify(
            {
                "proxy_fix_active": app.config.get(
                    "PROXY_FIX_ACTIVE", False
                ),
            }
        )

    # v1.5.1 V-109 (#54): CSP violation sink. Browsers POST a report
    # here when a CSP directive blocks a resource; logging them at
    # WARNING level gives operators a signal on XSS probes or
    # third-party-asset regressions that would otherwise be silent.
    # Unauthenticated by design (the report comes from the victim's
    # browser, not an authenticated session) but rate-limited so a
    # hostile page cannot flood structured logs.
    from services.rate_limit import limiter as _limiter

    @app.route("/api/csp-report", methods=["POST"])
    @_limiter.limit("60 per minute")
    def csp_report():
        # Browsers use application/csp-report (legacy report-uri) or
        # application/reports+json (modern report-to). Accept both
        # via request.get_data so we don't depend on content-type
        # routing.
        import json as _json
        raw = request.get_data(as_text=True) or ""
        try:
            parsed = _json.loads(raw) if raw else {}
        except ValueError:
            parsed = {"raw_body_truncated": raw[:500]}
        logger.warning(
            "csp_violation remote=%s ua=%s report=%s",
            request.remote_addr or "unknown",
            request.headers.get("User-Agent", "")[:200],
            _json.dumps(parsed)[:2000],
        )
        return ("", 204)

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.getenv("FLASK_PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
