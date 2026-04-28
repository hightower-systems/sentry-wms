"""End-to-end tests for the operator-run role-creation scripts in db/.

V-214 #170 shipped because no automated test exercised
db/role-snapshot-keeper.sql via the same invocation path an
operator runs. Whatever check existed during the v1.5.1 pre-merge
gate verified something other than "the role-creation script
actually creates the role." This file is the regression
guardrail: every role-creation script in db/ is tested by
spinning up a scratch DB, running the script via subprocess +
psql (the operator path), and asserting the role was created
with the expected grants and that the script exits non-zero on
internal failure.

Skip conditions:

  * psql binary not on PATH -- the api container does not bundle
    postgresql-client, so locally these tests skip with a clear
    message. CI runs on ubuntu-latest which has psql.
  * Script file not reachable -- protects the locally-skipped
    case from raising a path error.

The test invokes psql via subprocess so the variable-passing
semantics, ON_ERROR_STOP behavior, and \\gexec interpolation are
exercised exactly as an operator's runbook would. Importing the
SQL via psycopg or SQLAlchemy would test something other than
what an operator runs.
"""

import os
import shutil
import subprocess
import sys
import uuid
from urllib.parse import urlparse, urlunparse

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")
os.environ.setdefault("SENTRY_TOKEN_PEPPER", "NEVER_USE_THIS_PEPPER_IN_PRODUCTION")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DB_DIR = os.path.join(REPO_ROOT, "db")
SCHEMA_SQL = os.path.join(DB_DIR, "schema.sql")

PSQL_AVAILABLE = shutil.which("psql") is not None
SCRIPTS_REACHABLE = (
    os.path.exists(SCHEMA_SQL)
    and os.path.exists(os.path.join(DB_DIR, "role-snapshot-keeper.sql"))
    and os.path.exists(os.path.join(DB_DIR, "role-dispatcher.sql"))
)

# Each entry is (script-path, role-name, var-name, expected-grants).
# expected-grants is a set of (table_name, privilege_type) tuples
# the test queries via information_schema.table_privileges.
ROLE_SCRIPTS = [
    (
        "role-snapshot-keeper.sql",
        "sentry_keeper",
        "sentry_keeper_password",
        {
            ("integration_events", "SELECT"),
            ("snapshot_scans", "SELECT"),
            ("snapshot_scans", "UPDATE"),
            ("snapshot_scans", "DELETE"),
        },
    ),
    (
        "role-dispatcher.sql",
        "sentry_dispatcher",
        "sentry_dispatcher_password",
        {
            ("integration_events", "SELECT"),
            ("webhook_subscriptions", "SELECT"),
            ("webhook_subscriptions", "UPDATE"),
            ("webhook_deliveries", "SELECT"),
            ("webhook_deliveries", "INSERT"),
            ("webhook_deliveries", "UPDATE"),
            ("webhook_secrets", "SELECT"),
        },
    ),
]


pytestmark = pytest.mark.skipif(
    not (PSQL_AVAILABLE and SCRIPTS_REACHABLE),
    reason=(
        "requires psql binary on PATH and db/*.sql script files reachable; "
        "locally these tests skip when run inside the api container "
        "(psql not bundled). CI runs them on ubuntu-latest."
    ),
)


def _admin_url():
    return os.environ["DATABASE_URL"]


def _scratch_url(db_name):
    parsed = urlparse(_admin_url())
    return urlunparse(parsed._replace(path=f"/{db_name}"))


def _make_scratch_db():
    """Create a scratch DB on the running Postgres for the test
    and return its name. Caller is responsible for tearing it
    down via _drop_scratch_db.

    Schema is loaded so the role-script's GRANT targets exist;
    without it the GRANT lines fail with 'relation does not
    exist' and the test conflates that signal with the actual
    role-creation failure mode it is trying to catch.
    """
    name = f"sentry_role_test_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(_admin_url())
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute(f'CREATE DATABASE "{name}"')
    admin.close()

    # Load schema.sql via psql so any client-side meta-commands
    # in schema.sql are honored. -q suppresses "successful" banner;
    # ON_ERROR_STOP makes a load failure visible.
    result = subprocess.run(
        [
            "psql",
            _scratch_url(name),
            "-q",
            "-v", "ON_ERROR_STOP=1",
            "-f", SCHEMA_SQL,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        _drop_scratch_db(name, force=True)
        raise RuntimeError(f"failed to load schema.sql into scratch DB: {result.stderr}")
    return name


def _drop_scratch_db(name, force=False):
    """Drop the scratch DB. Force-disconnects any sessions held
    by the role-under-test before the DROP."""
    admin = psycopg2.connect(_admin_url())
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
        (name,),
    )
    try:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
    except psycopg2.errors.ObjectInUse:
        if not force:
            raise
    admin.close()


def _drop_role(role_name):
    """Drop the role plus any privileges it holds. Must be called
    AFTER every scratch DB the role had grants in is dropped --
    otherwise PostgreSQL refuses with DependentObjectsStillExist.

    Belt-and-suspenders: iterate through every sentry_role_test_*
    database that still exists and DROP OWNED BY the role inside
    each, so a half-dropped state from a prior failed run does
    not pin a role that subsequent tests then trip over."""
    admin = psycopg2.connect(_admin_url())
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,)
    )
    if cur.fetchone() is None:
        admin.close()
        return

    # Find any leftover scratch DBs the role might still hold
    # grants in (e.g. from a previously-aborted test run).
    cur.execute(
        "SELECT datname FROM pg_database WHERE datname LIKE 'sentry_role_test_%'"
    )
    leftovers = [r[0] for r in cur.fetchall()]
    admin.close()

    for db in leftovers:
        try:
            scratch_admin = psycopg2.connect(_scratch_url(db))
            scratch_admin.autocommit = True
            scratch_cur = scratch_admin.cursor()
            try:
                scratch_cur.execute(f'DROP OWNED BY "{role_name}" CASCADE')
            except psycopg2.Error:
                pass  # role had no grants in this DB
            scratch_admin.close()
        except psycopg2.Error:
            # Could not connect (DB already dropped or in use).
            # Continue; the next iteration / final DROP ROLE will
            # surface any real issue.
            continue

    admin = psycopg2.connect(_admin_url())
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute(f'DROP ROLE IF EXISTS "{role_name}"')
    admin.close()


def _run_role_script(script_filename, scratch_db, var_value=None):
    """Invoke the role-creation script via the operator path
    (psql -f). Returns the CompletedProcess so the caller can
    assert returncode + stderr."""
    cmd = ["psql", _scratch_url(scratch_db), "-f", os.path.join(DB_DIR, script_filename)]
    if var_value is not None:
        # Match the operator-runbook invocation: pass the password
        # as a bare value; the script wraps it with format(%L) at
        # the top level.
        var_name = script_filename.replace("role-", "").replace(".sql", "").replace("-", "_") + "_password"
        # The keeper uses sentry_keeper_password, the dispatcher uses
        # sentry_dispatcher_password. Derive from filename for parity.
        if script_filename == "role-snapshot-keeper.sql":
            var_name = "sentry_keeper_password"
        elif script_filename == "role-dispatcher.sql":
            var_name = "sentry_dispatcher_password"
        cmd.extend(["-v", f"{var_name}={var_value}"])
    return subprocess.run(cmd, capture_output=True, text=True)


@pytest.mark.parametrize(
    "script,role,var_name,expected_grants",
    ROLE_SCRIPTS,
    ids=[r[1] for r in ROLE_SCRIPTS],
)
class TestRoleCreationScripts:
    @pytest.fixture(autouse=True)
    def _scratch(self, script, role, var_name, expected_grants):
        # Clean up any leftover state from a prior aborted run
        # before creating fresh scratch state. PostgreSQL will
        # refuse to drop a role that holds grants in a database
        # we cannot reach, so leftover scratch DBs accumulate
        # and pin the role across runs.
        _drop_role(role)
        self.scratch = _make_scratch_db()
        try:
            yield
        finally:
            # Drop the scratch DB FIRST so the role's grants in
            # it disappear with the database; then drop the role.
            _drop_scratch_db(self.scratch)
            _drop_role(role)

    def test_valid_var_creates_role_and_exits_zero(
        self, script, role, var_name, expected_grants
    ):
        result = _run_role_script(script, self.scratch, var_value="test-pw-1234")
        assert result.returncode == 0, (
            f"script must exit zero with valid var; "
            f"got {result.returncode}\nstderr: {result.stderr}"
        )

        admin = psycopg2.connect(_scratch_url(self.scratch))
        try:
            cur = admin.cursor()
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            assert cur.fetchone() is not None, (
                f"role {role} must exist after the script runs; "
                f"this is the V-214 #170 regression check"
            )
        finally:
            admin.close()

    def test_expected_grants_land_per_information_schema(
        self, script, role, var_name, expected_grants
    ):
        result = _run_role_script(script, self.scratch, var_value="test-pw-grants")
        assert result.returncode == 0, result.stderr

        admin = psycopg2.connect(_scratch_url(self.scratch))
        try:
            cur = admin.cursor()
            cur.execute(
                """
                SELECT table_name, privilege_type
                  FROM information_schema.table_privileges
                 WHERE grantee = %s
                """,
                (role,),
            )
            actual = {(r[0], r[1]) for r in cur.fetchall()}
        finally:
            admin.close()

        missing = expected_grants - actual
        assert not missing, (
            f"role {role} is missing expected grants: {missing}\n"
            f"actual grants: {sorted(actual)}"
        )

    def test_missing_var_exits_nonzero(
        self, script, role, var_name, expected_grants
    ):
        """Proves \\set ON_ERROR_STOP on is in effect: omitting the
        password variable triggers a syntax error inside the
        :'var' interpolation, which without ON_ERROR_STOP would
        log to stderr and exit zero (the V-214 #170 silent-failure
        mode)."""
        result = _run_role_script(script, self.scratch, var_value=None)
        assert result.returncode != 0, (
            f"script must exit non-zero when password var is omitted; "
            f"a zero exit means \\set ON_ERROR_STOP on is not in effect "
            f"and silent failures will ship undetected. stderr: {result.stderr}"
        )

        admin = psycopg2.connect(_scratch_url(self.scratch))
        try:
            cur = admin.cursor()
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            assert cur.fetchone() is None, (
                f"role {role} must NOT exist after a failed run; "
                f"the script bailed before CREATE ROLE could fire"
            )
        finally:
            admin.close()

    def test_idempotent_rerun_with_rotated_password(
        self, script, role, var_name, expected_grants
    ):
        """Two consecutive runs with different passwords must
        both succeed; the second only fires the ALTER branch.
        Idempotence is the property the operator runbook relies
        on -- a re-run after a password rotation must not error,
        and the GRANTs must remain intact."""
        first = _run_role_script(script, self.scratch, var_value="first-pw")
        assert first.returncode == 0, first.stderr

        second = _run_role_script(script, self.scratch, var_value="second-pw")
        assert second.returncode == 0, second.stderr

        admin = psycopg2.connect(_scratch_url(self.scratch))
        try:
            cur = admin.cursor()
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            assert cur.fetchone() is not None, (
                f"role {role} must still exist after a rerun"
            )
            cur.execute(
                """
                SELECT COUNT(*) FROM information_schema.table_privileges
                 WHERE grantee = %s
                """,
                (role,),
            )
            grant_count = cur.fetchone()[0]
            assert grant_count >= len(expected_grants), (
                f"grants must remain intact after rerun; got {grant_count}, "
                f"expected at least {len(expected_grants)}"
            )
        finally:
            admin.close()
