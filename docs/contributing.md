# Contributing

Contributions are welcome. This document covers the development workflow, code conventions,
and pull request process.

---

## Getting started

```bash
git clone https://github.com/nunokisc/nanio-orchestrator.git
cd nanio-orchestrator

# Set up dev environment
uv sync                     # with uv (recommended)
# or:
pip install -e ".[dev]"     # with pip

source .venv/bin/activate

# Start the dev server
make run
```

The dev server runs at `http://localhost:8080` with API key `dev`.

---

## Running tests

```bash
make test           # pytest -v (177 tests, ~2 s)
make test-cov       # with coverage report
```

Tests use pytest-asyncio (`asyncio_mode = auto`) and httpx for API testing.
No external services are required — nginx and S3 are mocked.

**Tests must pass before every commit.** CI enforces this.

---

## Code style

```bash
make lint           # ruff check (errors only)
make fmt            # ruff format (auto-fix)
```

Rules: `ruff` with `E`, `F`, `I`, `W` (standard + import sorting). Line length 120.
Target: Python 3.9+.

---

## Commit conventions

Plain English, imperative mood:
```
fix: nil check in _enrich_vhost when ip_rule_ips_json is absent
feat: add per-pool bucket status endpoint
chore: bump ruff to 0.5
```

Prefixes: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`.

---

## Adding a new feature checklist

- [ ] Add/update Pydantic models in `models.py`
- [ ] Add DB column with `ALTER TABLE` migration in `db.py` (both `_run_migrations_async` and `init_db_sync`)
- [ ] Implement API endpoint in `api/`
- [ ] Update `sidecar.py` if the new field must survive DB loss (add to `write_vhost_sidecar` or `write_pool_sidecar`)
- [ ] Update `rebuild.py` to restore the field from sidecar on rebuild
- [ ] Update nginx template in `nginx/templates/` if the field affects config generation
- [ ] Update `nginx/generator.py` to pass new template variables
- [ ] Add tests in `tests/`
- [ ] Update `docs/` and `CLAUDE.md` if needed

---

## Pull request process

1. Fork the repo and create a branch: `git checkout -b feat/my-feature`
2. Make changes following the checklist above
3. Run `make test` and `make lint` — both must pass
4. Open a PR against `main`
5. CI will run tests on Python 3.9, 3.11, and 3.12
6. At least one review required before merge

---

## Project structure quick reference

See [CLAUDE.md](../CLAUDE.md) for the full module-by-module description and golden rules.

---

## Releasing

Releases are published to PyPI automatically via GitHub Actions when a tag is pushed:

```bash
# Bump version in pyproject.toml and nanio_orchestrator/__init__.py
# Then:
git tag v0.2.0
git push origin v0.2.0
```

The CI publish workflow builds the wheel and sdist, then uploads to PyPI using a
trusted publisher (OIDC — no stored API token needed).

See [`.github/workflows/publish.yml`](../.github/workflows/publish.yml).
