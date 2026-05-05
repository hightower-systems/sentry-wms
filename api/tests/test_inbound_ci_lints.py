"""v1.7.0 pre-merge gate item 11: CI lints for the Pipe B inbound surface.

These run inside the regular pytest suite so a CI green = lints green.
The lints encode the load-bearing rules from the plan that aren't
captured by behavioural tests:

- mapping_loader.py contains no `eval` / `exec` / `__import__`. The
  derived-expression sandbox is built on a function whitelist; an
  accidental `eval()` import in the loader source would punch through
  the whitelist regardless of how strict the grammar gets. Static
  rejection at the source-text layer is the regression net.
- Every `/api/v1/inbound/*` route registered on the Flask app is
  gated by @require_wms_token. A new POST route landing without the
  decorator would be a wire-level auth bypass; the lint catches it
  at CI time rather than at audit time.
- Every mapping doc on disk under db/mappings/ declares
  `version_compare`. The loader fails on missing `version_compare`
  at boot; this lint catches the same shape statically so a mapping
  doc shipped without the field never reaches a running deployment.
"""

import ast
import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry")
os.environ.setdefault("JWT_SECRET", "NEVER_USE_THIS_IN_PRODUCTION_32!")
os.environ.setdefault("SENTRY_ENCRYPTION_KEY", "t5hPIEVn_O41qfiMqAiPEnwzQh68o3Es46YfSOBvEK8=")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import yaml


# Path resolution handles both layouts:
# - CI / host: pytest cwd is `<repo>/api/`, __file__ is <repo>/api/tests/...
#   so parent.parent.parent = <repo>; api is <repo>/api, mappings at <repo>/db/mappings.
# - Docker container: api mounted at /app, db mounted at /db.
_TESTS_DIR = Path(__file__).resolve().parent
_API_ROOT_CANDIDATES = [_TESTS_DIR.parent, Path("/app")]
_DB_ROOT_CANDIDATES = [_TESTS_DIR.parent.parent / "db", Path("/db")]


def _resolve_first(candidates: list[Path]) -> Path | None:
    for c in candidates:
        if c.is_dir():
            return c
    return None


_API_ROOT = _resolve_first(_API_ROOT_CANDIDATES)
_DB_ROOT = _resolve_first(_DB_ROOT_CANDIDATES)
_MAPPING_LOADER = (_API_ROOT / "services" / "mapping_loader.py") if _API_ROOT else None
_MAPPINGS_DIR = (_DB_ROOT / "mappings") if _DB_ROOT else None


# ----------------------------------------------------------------------
# Source-text lint: no eval / exec / __import__ in mapping_loader.py
# ----------------------------------------------------------------------


# Builtins that, if called from mapping_loader.py, break the
# derived-expression sandbox. AST-walked rather than regex'd so
# docstring text ("no __import__") doesn't trip the lint.
_FORBIDDEN_BUILTIN_NAMES = frozenset(
    {"eval", "exec", "compile", "__import__"}
)


def _read_loader_source() -> str:
    if _MAPPING_LOADER is None or not _MAPPING_LOADER.is_file():
        raise FileNotFoundError(
            "mapping_loader.py not located. Tried "
            f"{[str(c / 'services' / 'mapping_loader.py') for c in _API_ROOT_CANDIDATES]}"
        )
    return _MAPPING_LOADER.read_text(encoding="utf-8")


class TestMappingLoaderForbiddenSymbols:
    def test_no_forbidden_builtin_calls(self):
        """Walk the loader's AST, flag any Call whose func is a bare
        Name in {eval, exec, compile, __import__}. Method calls
        (`evaluator.eval(...)`) are Attribute access on the func and
        do NOT trip; class names with overlap (e.g.,
        `EvalWithCompoundTypes`) are different identifiers and do NOT
        trip either."""
        source = _read_loader_source()
        tree = ast.parse(source)
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in _FORBIDDEN_BUILTIN_NAMES:
                    violations.append(
                        (node.func.id, getattr(node, "lineno", -1))
                    )
            # __import__ also shows up as a bare Name reference (e.g.,
            # passed to a higher-order function); flag those too.
            if isinstance(node, ast.Name) and node.id == "__import__":
                violations.append(("__import__ (referenced)", node.lineno))
        assert not violations, (
            f"mapping_loader.py contains forbidden builtin calls (R9 "
            f"sandbox violation): {violations}"
        )


# ----------------------------------------------------------------------
# Route lint: every /api/v1/inbound/* route is wms-token-gated
# ----------------------------------------------------------------------


def _is_wms_token_protected(view_func) -> bool:
    """Walk the wrapper chain looking for the __wms_token_protected__
    marker the require_wms_token decorator stamps on its wrapper."""
    func = view_func
    seen = set()
    while func is not None and id(func) not in seen:
        seen.add(id(func))
        if getattr(func, "__wms_token_protected__", False):
            return True
        if hasattr(func, "__wrapped__"):
            func = func.__wrapped__
        else:
            break
    return False


class TestInboundRoutesAreTokenGated:
    def test_every_inbound_post_is_wms_token_protected(self, app):
        """Every Flask rule under /api/v1/inbound/* with POST in its
        method set must walk into a require_wms_token wrapper. The
        documentation-aid mapping-schema GET is exempt -- it's
        intentionally unauthenticated."""
        problems = []
        for rule in app.url_map.iter_rules():
            if not rule.rule.startswith("/api/v1/inbound/"):
                continue
            if "POST" not in rule.methods:
                continue
            view = app.view_functions.get(rule.endpoint)
            if view is None:
                continue
            if not _is_wms_token_protected(view):
                problems.append((rule.rule, rule.endpoint))
        assert not problems, (
            "Inbound POST route(s) missing @require_wms_token. Each "
            "/api/v1/inbound/<resource> endpoint must walk through the "
            "decorator chain set up in routes.inbound.register_inbound_resource. "
            f"Offenders: {problems}"
        )

    def test_mapping_schema_is_exempt_from_token_gate(self, app):
        """Documentation aid: GET /api/v1/inbound/mapping-schema must
        stay reachable without X-WMS-Token. Pinning this so a future
        well-meaning lint rule doesn't accidentally gate it."""
        view = app.view_functions.get("inbound.mapping_schema")
        assert view is not None
        assert not _is_wms_token_protected(view), (
            "mapping-schema is intentionally unauthenticated; "
            "do not wrap it with @require_wms_token."
        )


# ----------------------------------------------------------------------
# Mapping-doc lint: every <source_system>.yaml has version_compare set
# ----------------------------------------------------------------------


_VALID_VERSION_COMPARE = {"iso_timestamp", "integer", "lexicographic"}


def _mapping_yaml_files() -> list[Path]:
    if _MAPPINGS_DIR is None or not _MAPPINGS_DIR.is_dir():
        return []
    return sorted(
        p for p in _MAPPINGS_DIR.iterdir()
        if p.suffix.lower() in (".yaml", ".yml", ".json")
        and not p.name.startswith(".")
    )


class TestMappingDocsHaveVersionCompare:
    def test_every_committed_mapping_doc_declares_version_compare(self):
        """Required by services.mapping_loader.MappingDocument; the
        loader fails at boot when the field is missing. The static
        lint catches the same shape so a mapping doc shipped without
        the field never reaches a running deployment."""
        problems = []
        for path in _mapping_yaml_files():
            try:
                doc = yaml.safe_load(path.read_text())
            except Exception as exc:
                problems.append((str(path), f"parse error: {exc}"))
                continue
            if not isinstance(doc, dict):
                problems.append((str(path), "top-level is not a mapping"))
                continue
            vc = doc.get("version_compare")
            if vc is None:
                problems.append((str(path), "missing version_compare"))
            elif vc not in _VALID_VERSION_COMPARE:
                problems.append((
                    str(path),
                    f"version_compare={vc!r} not in {sorted(_VALID_VERSION_COMPARE)}",
                ))
        assert not problems, (
            f"Mapping doc(s) violate the version_compare lint: {problems}"
        )

    def test_mappings_dir_exists(self):
        """If the directory itself is missing, the loader skips
        loading. Catch the typo at CI time so a renamed / moved
        mappings dir doesn't silently disable inbound on the next
        deploy."""
        assert _MAPPINGS_DIR is not None and _MAPPINGS_DIR.is_dir(), (
            f"db/mappings/ directory not located. Tried "
            f"{[str(c) for c in _DB_ROOT_CANDIDATES]}; the inbound "
            f"loader skips loading entirely when the dir isn't present."
        )
