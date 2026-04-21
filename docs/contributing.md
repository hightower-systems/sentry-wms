# Contributing

Thanks for your interest in contributing to Sentry WMS.

## Dev Environment Setup

```bash
git clone https://github.com/hightower-systems/sentry-wms.git
cd sentry-wms
cp .env.example .env
docker compose up -d
```

This starts PostgreSQL, the Flask API, and the React admin panel. The API runs on port 5000, admin on port 8080 (nginx-served production build). Add the `docker-compose.dev.yml` overlay to get the Vite dev-server on port 3000 with hot reload.

For mobile development:

```bash
cd mobile
npm install
npx expo start --clear
```

## Running Tests

```bash
docker compose exec api python -m pytest tests/ -x -q
```

307 tests using transaction rollback isolation (savepoint per test, rollback after). Zero regressions required before merging.

## Workflow

1. Fork the repository
2. Clone your fork: `git clone https://github.com/YOUR-USERNAME/sentry-wms.git`
3. Create a feature branch: `git checkout -b feature/your-feature-name`
4. Make your changes
5. Run the full test suite
6. Commit with a clear message
7. Push to your fork: `git push origin feature/your-feature-name`
8. Open a Pull Request against `main`

## Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/) with lowercase, present tense:

| Prefix | Use |
|--------|-----|
| `feat:` | New feature or capability |
| `fix:` | Bug fix |
| `security:` | Security fix |
| `refactor:` | Code change that doesn't fix a bug or add a feature |
| `test:` | Adding or updating tests |
| `docs:` | Documentation only |
| `chore:` | Build, CI, deps, formatting |
| `perf:` | Performance improvement |

Examples:

- `feat: add netsuite connector scaffold`
- `fix: scan input losing focus on pack screen`
- `security: add warehouse access check to shipping endpoint`

## Code Style

- **Python (API):** PEP 8. Type hints where practical. All SQL must use parameterized bindings -- no f-strings or string concatenation in queries.
- **JavaScript (Mobile/Admin):** Prettier defaults.
- **SQL:** Lowercase keywords, snake_case table and column names.
- **Status values:** Use constants from `api/constants.py` -- never hardcode status strings like `'OPEN'` or `'PICKED'`.
- **No em dashes.** Use hyphens (`-`) or double hyphens (`--`) only.

## Architecture Patterns

- **Service layer** - business logic lives in `api/services/` (inventory_service, picking_service, audit_service, auth_service)
- **`@with_db` decorator** - provides `g.db` session for the duration of a request; routes call `g.db.commit()` explicitly for writes
- **`@require_auth`** - JWT validation with live database check on every request (role, warehouse access, active status)
- **`check_warehouse_access()`** - call after loading a resource to verify the user is authorized for that resource's warehouse

## Pull Request Requirements

- All existing tests must pass
- New features should include tests
- One feature per PR -- keep them focused
- Update relevant documentation

## Reporting Issues

Open an [issue on GitHub](https://github.com/hightower-systems/sentry-wms/issues) with:

- What you expected to happen
- What actually happened
- Steps to reproduce
- Device/environment info (if mobile-related)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
