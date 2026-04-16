"""Infrastructure-level security regression tests.

These tests assert properties of static config files (Dockerfile, nginx.conf,
docker-compose.yml, .env.example, SECURITY.md, seed SQL) so that fixes to
CRITICAL security findings cannot silently regress.

Each test references the V-id from the Phase 6 audit report.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


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
        compose = _read("docker-compose.yml")
        assert '"8080:8080"' in compose

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
# V-005 -- No key material in application logs
# ---------------------------------------------------------------------------


class TestV005_NoKeyMaterialInLogs:
    def test_credential_vault_does_not_log_key(self):
        vault = _read("api/services/credential_vault.py")
        # The auto-generate + log path is gone; no logger.* call should
        # reference the key variable.
        assert "auto-generated" not in vault
        assert "os.environ[\"SENTRY_ENCRYPTION_KEY\"] =" not in vault
        assert "SENTRY_ENCRYPTION_KEY=%s" not in vault
