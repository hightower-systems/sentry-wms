# Contributing to Sentry WMS

Thanks for your interest in contributing to Sentry WMS! Here's how to get started.

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/YOUR-USERNAME/sentry-wms.git`
3. Create a feature branch: `git checkout -b feature/your-feature-name`
4. Make your changes
5. Run tests: `cd api && python -m pytest`
6. Commit with a clear message: `git commit -m "add: receiving API endpoint"`
7. Push to your fork: `git push origin feature/your-feature-name`
8. Open a Pull Request against `main`

## Commit Message Format

Use lowercase, present tense:
- `add: receiving API endpoint`
- `fix: barcode scan validation`
- `update: pick path sorting logic`
- `docs: API reference for receiving`
- `test: cycle count variance detection`

## Code Style

- **Python (API):** Follow PEP 8. Use type hints where practical.
- **JavaScript (Mobile/Admin):** Use Prettier defaults.
- **SQL:** Lowercase keywords, snake_case table/column names.

## Pull Request Requirements

- All existing tests must pass
- New features should include tests
- Update relevant documentation
- One feature per PR - keep them focused

## Reporting Issues

Open an issue on GitHub with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Device/environment info (if mobile-related)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
