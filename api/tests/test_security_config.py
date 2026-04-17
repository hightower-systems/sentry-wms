"""Infrastructure-level security regression tests.

These tests assert properties of static config files (Dockerfile, nginx.conf,
docker-compose.yml, .env.example, SECURITY.md, seed SQL) so that fixes to
CRITICAL security findings cannot silently regress.

Each test references the V-id from the Phase 6 audit report.

Run these from a host checkout. The api container only mounts ``./api``
(or nothing at all in production), so the repo-root files these tests
inspect are not reachable from inside the container. The module-level
``pytestmark`` below skips the whole file when that is the case.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / "docker-compose.yml").exists(),
    reason="repo-root config files not available (running inside container?)",
)


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text()


# ---------------------------------------------------------------------------
# V-001 -- Fernet master key must not ship as a default in docker-compose.yml
# ---------------------------------------------------------------------------


class TestV001_FernetKeyNotHardcoded:
    def test_no_default_fernet_value(self):
        compose = _read("docker-compose.yml")
        # The specific committed value must be gone from HEAD.
        assert "CrFAoVpcrJdjJoxrC4vv8RNL0r965VZ4TKkMcD2Zy4k=" not in compose
        # Strict-fail form must be used.
        assert "${SENTRY_ENCRYPTION_KEY:?" in compose

    def test_prod_compose_also_strict(self):
        prod = _read("docker-compose.prod.yml")
        assert "${SENTRY_ENCRYPTION_KEY:?" in prod

    def test_env_example_documents_key(self):
        example = _read(".env.example")
        assert "SENTRY_ENCRYPTION_KEY=" in example


# ---------------------------------------------------------------------------
# V-040 -- API and admin ports bound to loopback by default
# ---------------------------------------------------------------------------


class TestV040_LoopbackOnlyByDefault:
    def test_api_port_binds_loopback(self):
        compose = _read("docker-compose.yml")
        # Must not expose 5000 on all interfaces. The loopback-bound form
        # is "127.0.0.1:5000:5000"; the exposed form is "5000:5000".
        assert '"5000:5000"' not in compose, (
            "docker-compose.yml binds the API port on all interfaces; "
            "use 127.0.0.1:5000:5000 (V-040)"
        )
        assert '"127.0.0.1:5000:5000"' in compose

    def test_admin_port_binds_loopback(self):
        compose = _read("docker-compose.yml")
        assert '"8080:8080"' not in compose, (
            "docker-compose.yml binds the admin port on all interfaces; "
            "use 127.0.0.1:8080:8080 (V-040)"
        )
        assert '"127.0.0.1:8080:8080"' in compose

    def test_db_port_still_loopback(self):
        # The database port was already loopback-bound pre-V-040. Regression guard.
        compose = _read("docker-compose.yml")
        assert '"127.0.0.1:5432:5432"' in compose


# ---------------------------------------------------------------------------
# V-042 -- pip-audit and npm audit run in CI
# ---------------------------------------------------------------------------


class TestV042_DependencyAuditInCI:
    def test_audit_workflow_exists(self):
        workflow = _read(".github/workflows/audit.yml")
        assert "pip-audit" in workflow
        assert "npm audit" in workflow

    def test_pip_audit_is_strict(self):
        # --strict makes pip-audit exit non-zero on advisories.
        workflow = _read(".github/workflows/audit.yml")
        assert "--strict" in workflow

    def test_npm_audit_fails_on_high(self):
        workflow = _read(".github/workflows/audit.yml")
        assert "--audit-level=high" in workflow

    def test_covers_api_admin_mobile(self):
        workflow = _read(".github/workflows/audit.yml")
        assert "api/requirements.txt" in workflow
        assert "working-directory: admin" in workflow
        assert "working-directory: mobile" in workflow

    def test_runs_on_push_and_schedule(self):
        workflow = _read(".github/workflows/audit.yml")
        assert "push:" in workflow
        assert "schedule:" in workflow


# ---------------------------------------------------------------------------
# V-002 -- JWT_SECRET must be required via strict-fail form everywhere
# ---------------------------------------------------------------------------


class TestV002_JwtSecretStrict:
    def test_docker_compose_strict(self):
        compose = _read("docker-compose.yml")
        # Historical defaults must not reappear.
        assert "dev-secret-change-in-production" not in compose
        assert "dev-jwt-secret-do-not-use-in-production" not in compose
        # Strict-fail form must be used.
        assert "${JWT_SECRET:?" in compose

    def test_prod_compose_strict(self):
        prod = _read("docker-compose.prod.yml")
        assert "${JWT_SECRET:?" in prod


# ---------------------------------------------------------------------------
# V-003 -- Admin Dockerfile must be a production nginx build, not Vite dev
# ---------------------------------------------------------------------------


class TestV003_AdminDockerfileProduction:
    def test_dockerfile_is_multistage_nginx(self):
        dockerfile = _read("admin/Dockerfile")
        assert "FROM nginx:" in dockerfile, "admin runtime must be nginx"
        assert "USER nginx" in dockerfile, "admin must not run as root"
        assert "npm run dev" not in dockerfile, "dev-server must not ship to prod"
        assert "vite" not in dockerfile.lower() or "npm run build" in dockerfile

    def test_dockerfile_copies_built_dist(self):
        dockerfile = _read("admin/Dockerfile")
        assert "COPY --from=builder /app/dist" in dockerfile

    def test_nginx_config_blocks_vite_dev_paths(self):
        # Any legacy scanner probing Vite's /@fs/, /@id/, /@vite/, or HMR
        # endpoints should get 404 from nginx.
        nginx_conf = _read("admin/nginx.conf")
        for pattern in ("@fs", "@id", "@vite", "__vite_hmr"):
            assert pattern in nginx_conf, f"nginx.conf must reference {pattern}"

    def test_nginx_spa_fallback_present(self):
        nginx_conf = _read("admin/nginx.conf")
        assert "try_files $uri" in nginx_conf
        assert "/index.html" in nginx_conf

    def test_compose_admin_listens_on_8080(self):
        # V-040 rebound to 127.0.0.1:8080:8080 by default. A deployment that
        # layers a reverse proxy may override via docker-compose.override.yml
        # to re-open the binding. The test accepts either form.
        compose = _read("docker-compose.yml")
        assert re.search(r'"(127\.0\.0\.1:)?8080:8080"', compose), (
            "compose must publish admin on port 8080"
        )

    def test_compose_admin_no_bind_mount(self):
        # The prod compose must not bind-mount ./admin into the container;
        # that would reintroduce the live-reload surface.
        compose = _read("docker-compose.yml")
        # The admin service block starts at "admin:" and continues until
        # the next top-level service or end of file. We just assert the
        # source mount pattern does not appear alongside admin anywhere.
        assert "./admin:/app" not in compose, "source bind-mount belongs in docker-compose.dev.yml only"

    def test_dockerignore_excludes_noise(self):
        ignored = _read("admin/.dockerignore")
        for token in ("node_modules", "dist", ".git", ".env"):
            assert token in ignored, f".dockerignore missing {token}"

    def test_dev_overlay_exists_for_local_hot_reload(self):
        # A separate dev compose must exist so devs can still run Vite
        # locally without touching the production compose.
        assert (REPO_ROOT / "docker-compose.dev.yml").exists()


# ---------------------------------------------------------------------------
# V-004 -- Redis broker must require a password
# ---------------------------------------------------------------------------


class TestV004_RedisRequirePass:
    def test_compose_requires_password(self):
        compose = _read("docker-compose.yml")
        assert "--requirepass" in compose, "redis must start with --requirepass"
        assert "${REDIS_PASSWORD:?" in compose, "REDIS_PASSWORD must be required"

    def test_compose_broker_url_has_auth(self):
        compose = _read("docker-compose.yml")
        # The URL form redis://:<pass>@redis:6379/0 must be used; bare
        # redis://redis:6379/0 leaves the broker unauthenticated.
        assert "redis://:${REDIS_PASSWORD" in compose
        assert "CELERY_BROKER_URL: redis://redis:6379" not in compose
        assert "CELERY_RESULT_BACKEND: redis://redis:6379" not in compose

    def test_compose_healthcheck_uses_auth(self):
        compose = _read("docker-compose.yml")
        # The ping healthcheck must pass -a so it actually authenticates;
        # otherwise Redis could reject pings but the healthcheck would
        # still pass on the unauthenticated error response.
        assert "redis-cli -a" in compose

    def test_prod_compose_requires_password(self):
        prod = _read("docker-compose.prod.yml")
        assert "--requirepass" in prod
        assert "redis://:${REDIS_PASSWORD" in prod

    def test_env_example_documents_redis_password(self):
        example = _read(".env.example")
        assert "REDIS_PASSWORD=" in example

    def test_redis_port_not_exposed_to_host(self):
        # Defense in depth: even authenticated Redis should not be
        # reachable from the host network.
        compose = _read("docker-compose.yml")
        # There should be no "6379:" line under the redis service block.
        redis_block = compose[compose.find("  redis:"):compose.find("  celery-worker:")]
        assert "6379:" not in redis_block


# ---------------------------------------------------------------------------
# V-005 -- No key material in application logs
# ---------------------------------------------------------------------------


class TestV069_NoSeedAdminHash:
    def test_seed_sql_has_no_bcrypt_hash(self):
        # The specific published hash must be gone from every SQL file.
        for sql_path in (REPO_ROOT / "db").glob("*.sql"):
            body = sql_path.read_text()
            assert "$2b$12$zDGRKFLmc6v/A4mVhxOzb.7uoW1ulnXn0AisK5uJ5iWk33vC2EpSK" not in body, (
                f"{sql_path.name} still contains the known bcrypt hash of 'admin'"
            )
            # No generic bcrypt hash literal (prefix $2a$/$2b$/$2y$) should
            # appear in the public SQL. Hashes belong to runtime scripts only.
            for prefix in ("$2a$", "$2b$", "$2y$"):
                assert prefix not in body, (
                    f"{sql_path.name} contains a bcrypt hash literal ({prefix})"
                )

    def test_seed_sql_uses_placeholder(self):
        seed = _read("db/seed-apartment-lab.sql")
        assert "SEED_SCRIPT_WILL_REPLACE_THIS" in seed, (
            "seed-apartment-lab.sql must insert the placeholder the setup script overwrites"
        )

    def test_seed_script_rewrites_admin_password(self):
        # seed.sh is responsible for replacing the placeholder with a real
        # bcrypt hash at setup time. Verify the rewrite step is present.
        script = _read("db/seed.sh")
        assert "UPDATE users SET password_hash" in script
        assert "crypt(" in script


class TestV005_NoKeyMaterialInLogs:
    def test_credential_vault_does_not_log_key(self):
        vault = _read("api/services/credential_vault.py")
        # The auto-generate + log path is gone; no logger.* call should
        # reference the key variable.
        assert "auto-generated" not in vault
        assert "os.environ[\"SENTRY_ENCRYPTION_KEY\"] =" not in vault
        assert "SENTRY_ENCRYPTION_KEY=%s" not in vault
