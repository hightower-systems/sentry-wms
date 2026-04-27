"""CI guardrail: every INSERT INTO a UUID-retrofitted table must list external_id.

Migration 025 (issue #108) drops the DEFAULT on external_id across the ten
aggregate/actor tables. A callsite that forgets to supply external_id now
fails at runtime with a NOT NULL violation. This test walks the `api/`
tree and fails during CI if any INSERT INTO a retrofitted table omits
`external_id` from its column list, so the regression surfaces on the
pull request instead of in production.

The scan is source-level (regex over file text); multi-line INSERT
column lists are supported by reading up to the closing paren after
`INSERT INTO <table>`. SQL fragments built at runtime (string
concatenation, dynamic table names) are out of scope: the project does
not do that for these ten tables today, and the integration tests would
catch a runtime NULL column error anyway.

v1.5.1 V-216 (umbrella #156): the scan now also covers ``db/**/*.sql``
(seed scripts, migrations, operator-driven helpers). Pre-v1.5.1 the
guardrail only walked ``api/``, so a seed file that omitted
external_id would pass CI and fail at migration time with a NOT NULL
violation. The scan continues to skip runtime-assembled SQL; the .sql
coverage is strictly about static INSERT statements in the db/ tree.
"""

import os
import re

RETROFITTED_TABLES = (
    "users",
    "items",
    "bins",
    "sales_orders",
    "purchase_orders",
    "item_receipts",
    "inventory_adjustments",
    "bin_transfers",
    "cycle_counts",
    "item_fulfillments",
)

_REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_API_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_DB_DIR = os.path.abspath(os.path.join(_REPO_DIR, "db"))

# Capture the opening `INSERT INTO <table> (` and everything up to the
# first closing `)`. DOTALL lets the column list span multiple lines.
_INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+(" + "|".join(RETROFITTED_TABLES) + r")\s*\(([^)]*)\)",
    re.IGNORECASE | re.DOTALL,
)


def _source_files():
    """Yield every .py file under api/ and every .sql file under db/.

    v1.5.1 V-216 (umbrella #156): the .sql walk is the new piece.
    Fresh installs run db/schema.sql; upgrades apply db/migrations/*.sql
    in order; operators run db/role-snapshot-keeper.sql (V-214) on
    demand. Any of those paths can introduce an INSERT INTO a
    retrofitted table that omits external_id, so they belong in the
    guardrail surface too.
    """
    for root, _dirs, files in os.walk(_API_DIR):
        if "__pycache__" in root or ".pytest_cache" in root:
            continue
        for fname in files:
            if fname.endswith(".py"):
                yield os.path.join(root, fname)
    if os.path.isdir(_DB_DIR):
        for root, _dirs, files in os.walk(_DB_DIR):
            for fname in files:
                if fname.endswith(".sql"):
                    yield os.path.join(root, fname)


def _violations():
    bad = []
    for path in _source_files():
        # Exempt this file: the RETROFITTED_TABLES constant contains the
        # table names as string literals, which the regex would otherwise
        # match when they appear next to an INSERT INTO in a doctest or
        # comment.
        if os.path.abspath(path) == os.path.abspath(__file__):
            continue
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        for m in _INSERT_RE.finditer(src):
            table = m.group(1).lower()
            cols = m.group(2)
            if "external_id" not in cols.lower():
                line_no = src.count("\n", 0, m.start()) + 1
                rel = os.path.relpath(path, _REPO_DIR)
                bad.append((rel, line_no, table, cols.strip()))
    return bad


def test_every_insert_to_retrofitted_table_lists_external_id():
    """Scan api/ (.py) + db/ (.sql) for INSERT INTO one of the ten
    tables; assert external_id is in the column list."""
    bad = _violations()
    assert not bad, (
        "INSERT statements below target a UUID-retrofitted table but do not "
        "list external_id in the column list. After migration 025, these "
        "inserts fail with a NOT NULL violation. Supply `uuid.uuid4()` "
        "(Python) or `gen_random_uuid()` (inline SQL):\n  " +
        "\n  ".join(f"{path}:{line} -> {table}({cols})" for path, line, table, cols in bad)
    )


def test_guardrail_catches_a_known_bad_fragment():
    """Sanity check the regex actually catches a known-bad INSERT."""
    bad_src = "cur.execute(\"INSERT INTO users (username, password_hash) VALUES ('x', 'y')\")"
    matches = list(_INSERT_RE.finditer(bad_src))
    assert len(matches) == 1
    assert "external_id" not in matches[0].group(2).lower()


def test_guardrail_walks_db_sql_tree():
    """v1.5.1 V-216 (umbrella #156): the walk must include .sql files
    under db/. Synthesise a bad fragment on the fly to prove the
    scan surface covers the db/ tree, without editing a real
    migration to be broken."""
    # The _source_files() generator yields both .py and .sql paths;
    # a non-empty overlap with db/*.sql confirms the walk reached
    # that tree.
    sources = list(_source_files())
    sql_sources = [p for p in sources if p.endswith(".sql")]
    assert any(p.startswith(_DB_DIR + os.sep) for p in sql_sources), (
        "walk must include db/*.sql so seed scripts and migrations "
        "are covered by the external_id guardrail"
    )
